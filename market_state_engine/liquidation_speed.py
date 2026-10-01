"""Past-only liquidation speed scoring, with reusable research indicators."""
from bisect import bisect_left
from datetime import date, datetime, time, timedelta, timezone
import math
import statistics


KST = timezone(timedelta(hours=9))


def _time(value, kst=False):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST if kst else timezone.utc)
    return parsed.timestamp()


def _percentile(value, reference):
    return 100 * sum(x <= value for x in reference) / len(reference) if reference else None


def _score(percentile, value):
    if percentile is None or value <= 0:
        return 0
    for boundary, score in ((30, 0), (40, 1), (50, 2), (60, 3), (70, 4), (80, 5)):
        if percentile < boundary:
            return score
    return 6


class _Series:
    def __init__(self, rows):
        rows = sorted(rows)
        self.times = [t for t, _ in rows]
        self.values = [v for _, v in rows]
        self.prefix = [0.0]
        for value in self.values:
            self.prefix.append(self.prefix[-1] + value)

    def amount(self, start, end):
        return max(0.0, self.prefix[bisect_left(self.times, end)] - self.prefix[bisect_left(self.times, start)])

    def window(self, now, minutes, history_start, config):
        seconds = minutes * 60
        # Fixed completed UTC bins, all ending before the live window starts.
        first_end = math.ceil(history_start / seconds) * seconds + seconds
        last_end = math.floor((now - seconds) / seconds) * seconds
        first_end = max(first_end, last_end - config.liquidation_speed_reference_hours * 3600 + seconds)
        ends = list(range(int(first_end), int(last_end) + 1, seconds))
        history = [self.amount(t - seconds, t) / minutes for t in ends]
        changes = [b - a for a, b in zip(history, history[1:])]
        amount = self.amount(now - seconds, now)
        speed = amount / minutes
        previous = self.amount(now - seconds * 2, now) - amount
        previous = max(0.0, previous) / minutes
        mean = statistics.mean(history) if history else None
        sd = statistics.stdev(history) if len(history) > 1 else None
        change_mean = statistics.mean(changes) if changes else None
        change_sd = statistics.stdev(changes) if len(changes) > 1 else None
        ready = len(history) * minutes >= config.liquidation_speed_min_reference_hours * 60
        z = (speed - mean) / sd if ready and sd and sd > 1e-9 else None
        acceleration = speed - previous
        accel_z = (acceleration - change_mean) / change_sd if ready and change_sd and change_sd > 1e-9 else None
        a, b = bisect_left(self.times, now - seconds), bisect_left(self.times, now)
        percentile = _percentile(speed, history) if ready else None
        indicators = {
            "amount_usd": amount, "speed_usd_per_min": speed,
            "previous_speed_usd_per_min": previous, "speed_change_usd_per_min": acceleration,
            "speed_z": z, "speed_change_z": accel_z,
            "reference_mean_speed": mean, "reference_std_speed": sd,
            "reference_count": len(history), "reference_start": ends[0] - seconds if ends else None,
            "reference_end": ends[-1] if ends else None, "reference_ready": ready,
            "speed_percentile": percentile, "raw_score": _score(percentile, speed),
            "increasing": speed > previous, "above_mean": mean is not None and speed > mean,
            "event_count": b - a,
            "active_minutes": len({int((t - (now - seconds)) // 60) for t in self.times[a:b]}),
            "largest_event_share": max(self.values[a:b], default=0) / amount if amount > 0 else None,
        }
        return indicators, history


def calculate_liquidation_speed(data, current_time, config):
    if not (0 < config.liquidation_speed_min_reference_hours <= config.liquidation_speed_reference_hours):
        raise ValueError("Liquidation reference hours must satisfy 0 < minimum <= reference")
    now = math.floor(current_time.timestamp())
    data = data or {}
    symbol = data.get("symbol")
    events = {"LONG": [], "SHORT": []}
    invalid = 0
    for row in data.get("raw_events", []):
        if symbol and row.get("symbol") != symbol:
            continue
        try:
            t = _time(row.get("event_time_kst", row.get("timestamp")), "event_time_kst" in row)
            value = float(row["usd_size"])
            side = row.get("side")
            kind = row.get("liq_type")
            if side is None:
                side = {"short_liquidation": "BUY", "long_liquidation": "SELL"}.get(kind)
            if side not in ("BUY", "SELL") or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid liquidation event")
            if kind and kind != ("short_liquidation" if side == "BUY" else "long_liquidation"):
                raise ValueError("Conflicting liquidation direction")
            if t < now:
                events["LONG" if side == "BUY" else "SHORT"].append((t, value))
        except (ValueError, TypeError, KeyError, AttributeError):
            invalid += 1
    all_times = [t for rows in events.values() for t, _ in rows]
    history_start = min(all_times) if all_times else now
    coverage = "event_extent_assumed"
    missing_today = False
    if "raw_file_dates" in data:
        try:
            days = {date.fromisoformat(s) for s in data["raw_file_dates"]}
            today = datetime.fromtimestamp(now, KST).date()
            missing_today = today not in days
            first = today
            while first - timedelta(days=1) in days:
                first -= timedelta(days=1)
            # A daily filename alone does not prove collection started at midnight.
            history_start = max(history_start, datetime.combine(first, time.min, KST).timestamp())
            coverage = "contiguous_files_assumed_not_heartbeat"
        except (ValueError, TypeError):
            missing_today = True
    series = {side: _Series(rows) for side, rows in events.items()}
    windows = {}
    histories = {}
    for minutes in (1, 5, 10):
        key = str(minutes)
        windows[key] = {}
        for side in ("LONG", "SHORT"):
            indicators, reference = series[side].window(now, minutes, history_start, config)
            windows[key][side] = indicators
            if minutes == 5:
                histories[side] = reference
    short_hour = series["LONG"].amount(now - 3600, now)
    long_hour = series["SHORT"].amount(now - 3600, now)
    total_hour = short_hour + long_hour
    a, b = windows["5"]["LONG"], windows["5"]["SHORT"]
    total = a["amount_usd"] + b["amount_usd"]
    imbalance = (a["amount_usd"] - b["amount_usd"]) / total if total else 0.0
    ready = a["reference_ready"] and b["reference_ready"] and not missing_today and not invalid
    direction = "LONG" if imbalance > 0 else "SHORT" if imbalance < 0 else None
    total_reference = [x + y for x, y in zip(histories["LONG"], histories["SHORT"])]
    activity = _score(_percentile(total / 5, total_reference), total) if ready else 0
    selected = windows["5"].get(direction, {})
    if not ready:
        gate = "missing_raw_file" if missing_today else "invalid_rows" if invalid else "insufficient_history"
    elif not total:
        gate = "no_recent_events"
    elif abs(imbalance) < config.liquidation_min_imbalance_ratio:
        gate = "balanced"
    elif not selected["increasing"]:
        gate = "not_increasing"
    elif not selected["above_mean"]:
        gate = "not_above_mean"
    else:
        gate = "passed"
    score = selected["raw_score"] if gate == "passed" else 0
    bonus = config.liquidation_activity_bonus_score if (
        gate == "passed" and score > 0 and activity >= config.liquidation_activity_bonus_min_score
        and abs(imbalance) >= config.liquidation_activity_bonus_min_imbalance_ratio
    ) else 0
    hourly_count = 0
    for row in data.get("hourly_history", []):
        if symbol and row.get("symbol") != symbol:
            continue
        try:
            t = _time(row.get("window_end_kst", row.get("timestamp")), "window_end_kst" in row)
            hourly_count += now - 7 * 86400 <= t < now
        except (ValueError, TypeError, AttributeError):
            pass
    indicators = {
        "scoring_mode": "speed_5m_v1", "window_minutes": 5,
        "windows": windows, "data_status": "ready" if ready else gate,
        "coverage_basis": coverage, "invalid_rows": invalid, "gate": gate,
        "direction": direction, "imbalance_ratio_5m": imbalance,
        "short_liq_5m": a["amount_usd"], "long_liq_5m": b["amount_usd"],
        "total_liq_5m": total, "reference_hours_requested": config.liquidation_speed_reference_hours,
        # These legacy fields remain hourly quantities, never silently relabelled as 5m.
        "short_liq_1h": short_hour, "long_liq_1h": long_hour,
        "total_liq_1h": total_hour, "net_liq_1h": short_hour - long_hour,
        "imbalance_ratio": (short_hour - long_hour) / total_hour if total_hour else 0.0,
        "reference_hours": hourly_count,
        "latest_event_time": max(all_times) if all_times else None,
        "last_complete_minute_start": (now // 60 - 1) * 60,
        "last_complete_minute_short_liq": series["LONG"].amount((now // 60 - 1) * 60, now // 60 * 60),
        "last_complete_minute_long_liq": series["SHORT"].amount((now // 60 - 1) * 60, now // 60 * 60),
    }
    reason = (f"liquidation_score imbalance {direction} +{score} mode=speed_5m_v1 "
              f"gate={gate} speed_5m={selected.get('speed_usd_per_min', 0):.2f} "
              f"previous_5m={selected.get('previous_speed_usd_per_min', 0):.2f} "
              f"imbalance_ratio_5m={imbalance:.2f}")
    reasons = [reason]
    if bonus:
        reasons.append(f"liquidation_activity_bonus {direction} +{bonus}")
    return {"long_score": score if direction == "LONG" else 0,
            "short_score": score if direction == "SHORT" else 0,
            "activity_score": activity,
            "long_activity_bonus": bonus if direction == "LONG" else 0,
            "short_activity_bonus": bonus if direction == "SHORT" else 0,
            "indicators": indicators, "reasons": reasons}
