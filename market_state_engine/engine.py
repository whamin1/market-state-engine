from datetime import datetime, timedelta, timezone

from .config import MarketStateConfig


class MarketStateEngine:
    def __init__(self, config: MarketStateConfig | None = None):
        self.config = config or MarketStateConfig()
        self.failure_counts = {
            "upper_breakout_failure": 0,
            "lower_breakdown_failure": 0,
        }
        self._counted_failure_keys = set()

    def update(self, ohlcv_data, current_candle=None, day_progress=None, current_time=None, liquidation_data=None):
        failure_result = self.update_failure_counts(ohlcv_data)
        price_position_result = self.calc_price_position_score(ohlcv_data, current_candle)
        body_result = self.calc_body_score(ohlcv_data, current_candle)
        volume_result = self.calc_volume_score(ohlcv_data, current_candle, day_progress, current_time)
        trend_result = self.calc_trend_continuity_score(ohlcv_data, current_candle)
        range_result = self.calc_range_score(ohlcv_data, current_candle)
        atr_result = self.calc_atr_score(ohlcv_data, current_candle)
        liquidation_result = self.calc_liquidation_score(liquidation_data, current_time)

        long_score = (
            price_position_result["long_score"]
            + body_result["long_score"]
            + volume_result["long_score"]
            + trend_result["long_score"]
            + range_result["long_score"]
            + liquidation_result["long_score"]
        )
        short_score = (
            price_position_result["short_score"]
            + body_result["short_score"]
            + volume_result["short_score"]
            + trend_result["short_score"]
            + range_result["short_score"]
            + liquidation_result["short_score"]
        )

        if range_result["zero_long_score"]:
            long_score = 0

        if range_result["zero_short_score"]:
            short_score = 0

        activity_score = atr_result["activity_score"]

        if range_result["block_trade"]:
            state = "HOLD"
            signal = "NO_TRADE"
        else:
            state = self.get_state(long_score, short_score)
            signal = self.get_signal(long_score, short_score)

        return {
            "long_score": long_score,
            "short_score": short_score,
            "activity_score": activity_score,
            "state": state,
            "signal": signal,
            "range": range_result["range"],
            "atr": atr_result["atr"],
            "failure_counts": dict(self.failure_counts),
            "reasons": (
                failure_result["reasons"]
                + price_position_result["reasons"]
                + body_result["reasons"]
                + volume_result["reasons"]
                + trend_result["reasons"]
                + range_result["reasons"]
                + atr_result["reasons"]
                + liquidation_result["reasons"]
            ),
        }

    def update_failure_counts(self, ohlcv_data):
        if len(ohlcv_data) < self.config.range_days + 1:
            return {"reasons": ["failure_count skipped: not enough completed candles"]}

        completed_candle = ohlcv_data[-1]
        reference_candles = ohlcv_data[: -1]
        range_candles = reference_candles[-self.config.range_days :]
        range_high = max(candle["high"] for candle in range_candles)
        range_low = min(candle["low"] for candle in range_candles)

        candle_key = completed_candle.get("timestamp", len(ohlcv_data))
        reasons = []

        upper_breakout_price = range_high * (1 + self.config.range_breakout_pct / 100)
        lower_breakdown_price = range_low * (1 - self.config.range_breakout_pct / 100)

        upper_failure_key = (candle_key, "upper_breakout_failure")
        if (
            completed_candle["high"] > upper_breakout_price
            and completed_candle["close"] <= range_high
            and upper_failure_key not in self._counted_failure_keys
        ):
            self.failure_counts["upper_breakout_failure"] += 1
            self._counted_failure_keys.add(upper_failure_key)
            reasons.append("failure_count upper breakout failure +1")

        lower_failure_key = (candle_key, "lower_breakdown_failure")
        if (
            completed_candle["low"] < lower_breakdown_price
            and completed_candle["close"] >= range_low
            and lower_failure_key not in self._counted_failure_keys
        ):
            self.failure_counts["lower_breakdown_failure"] += 1
            self._counted_failure_keys.add(lower_failure_key)
            reasons.append("failure_count lower breakdown failure +1")

        if not reasons:
            reasons.append("failure_count 0: no completed breakout failure")

        return {"reasons": reasons}

    def calc_liquidation_score(self, liquidation_data=None, current_time=None):
        if not liquidation_data:
            return {"long_score": 0, "short_score": 0, "reasons": ["liquidation_score skipped: no data"]}

        raw_events = liquidation_data.get("raw_events", [])
        hourly_history = liquidation_data.get("hourly_history", [])
        symbol = liquidation_data.get("symbol")

        if not raw_events or not hourly_history:
            return {"long_score": 0, "short_score": 0, "reasons": ["liquidation_score skipped: missing raw or hourly data"]}

        now = self._parse_time(current_time) if current_time is not None else datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(hours=1)
        one_week_ago = now - timedelta(days=7)

        current_short_liq = 0.0
        current_long_liq = 0.0

        for event in raw_events:
            if symbol and event.get("symbol") != symbol:
                continue

            event_time = self._parse_time(event.get("event_time_kst", event.get("timestamp")))
            if not (one_hour_ago < event_time <= now):
                continue

            usd_size = self._safe_float(event.get("usd_size"))
            liq_type = event.get("liq_type")
            side = event.get("side")

            if liq_type == "short_liquidation" or side == "BUY":
                current_short_liq += usd_size
            elif liq_type == "long_liquidation" or side == "SELL":
                current_long_liq += usd_size

        reference_short_liqs = []
        reference_long_liqs = []

        for row in hourly_history:
            if symbol and row.get("symbol") != symbol:
                continue

            row_time = self._parse_time(row.get("window_end_kst", row.get("timestamp")))
            if not (one_week_ago <= row_time < now):
                continue

            reference_short_liqs.append(self._safe_float(row.get("short_liq_usd_1h", row.get("short_liq_usd"))))
            reference_long_liqs.append(self._safe_float(row.get("long_liq_usd_1h", row.get("long_liq_usd"))))

        short_liq_score = self._percentile_to_score(current_short_liq, reference_short_liqs)
        long_liq_score = self._percentile_to_score(current_long_liq, reference_long_liqs)

        return {
            "long_score": short_liq_score,
            "short_score": long_liq_score,
            "reasons": [
                (
                    f"liquidation_score short_liq LONG +{short_liq_score} "
                    f"current_1h={current_short_liq:.2f} reference_hours={len(reference_short_liqs)}"
                ),
                (
                    f"liquidation_score long_liq SHORT +{long_liq_score} "
                    f"current_1h={current_long_liq:.2f} reference_hours={len(reference_long_liqs)}"
                ),
            ],
        }

    def calc_atr_score(self, ohlcv_data, current_candle=None):
        if current_candle is None:
            candles = ohlcv_data
        else:
            candles = ohlcv_data + [current_candle]

        minimum_candles = self.config.atr_period + 2
        if len(candles) < minimum_candles:
            return {"activity_score": 0, "atr": None, "reasons": ["atr_score skipped: not enough candles"]}

        true_ranges = self._calculate_true_ranges(candles)
        atr_values = self._calculate_atr_values(true_ranges, self.config.atr_period)

        if len(atr_values) < 2:
            return {"activity_score": 0, "atr": None, "reasons": ["atr_score skipped: not enough ATR values"]}

        current_atr = atr_values[-1]
        reference_atrs = atr_values[-366:-1]
        score = self._percentile_to_score(current_atr, reference_atrs)

        return {
            "activity_score": score,
            "atr": current_atr,
            "reasons": [f"atr_score activity +{score} atr={current_atr:.2f}"],
        }

    def calc_trend_continuity_score(self, ohlcv_data, current_candle=None):
        if current_candle is None:
            if len(ohlcv_data) < 8:
                return {"long_score": 0, "short_score": 0, "reasons": ["trend_continuity skipped: not enough candles"]}

            reference_candles = ohlcv_data[:-1]
            current_candle = ohlcv_data[-1]
        else:
            if len(ohlcv_data) < 7:
                return {"long_score": 0, "short_score": 0, "reasons": ["trend_continuity skipped: not enough reference candles"]}

            reference_candles = ohlcv_data

        current_direction = self._candle_direction(current_candle)
        previous_direction = self._candle_direction(reference_candles[-1])

        if current_direction == "FLAT":
            return {"long_score": 0, "short_score": 0, "reasons": ["trend_continuity 0: current candle is doji"]}

        long_score = 0
        short_score = 0
        reasons = []

        if current_direction == previous_direction:
            current_body = abs(current_candle["close"] - current_candle["open"])
            reference_bodies = [abs(candle["close"] - candle["open"]) for candle in reference_candles]
            continuity_score = self._percentile_to_score(current_body, reference_bodies)

            if current_direction == "UP":
                long_score += continuity_score
                reasons.append(f"trend_continuity same direction LONG +{continuity_score}")
            else:
                short_score += continuity_score
                reasons.append(f"trend_continuity same direction SHORT +{continuity_score}")
        else:
            reasons.append("trend_continuity 0: current and previous candle differ")

        recent_7_candles = reference_candles[-6:] + [current_candle]
        up_count = sum(1 for candle in recent_7_candles if self._candle_direction(candle) == "UP")
        down_count = sum(1 for candle in recent_7_candles if self._candle_direction(candle) == "DOWN")

        if up_count == 7:
            long_score += 2
            reasons.append("trend_continuity 7 up candles LONG +2")
        elif up_count >= 6:
            long_score += 1
            reasons.append("trend_continuity 6 up candles LONG +1")

        if down_count == 7:
            short_score += 2
            reasons.append("trend_continuity 7 down candles SHORT +2")
        elif down_count >= 6:
            short_score += 1
            reasons.append("trend_continuity 6 down candles SHORT +1")

        return {"long_score": long_score, "short_score": short_score, "reasons": reasons}

    def calc_range_score(self, ohlcv_data, current_candle=None):
        if current_candle is None:
            if len(ohlcv_data) < self.config.range_days + 1:
                return self._empty_range_result("range skipped: not enough candles")

            reference_candles = ohlcv_data[:-1]
            current_candle = ohlcv_data[-1]
        else:
            if len(ohlcv_data) < self.config.range_days:
                return self._empty_range_result("range skipped: not enough reference candles")

            reference_candles = ohlcv_data

        range_candles = reference_candles[-self.config.range_days :]
        range_high = max(candle["high"] for candle in range_candles)
        range_low = min(candle["low"] for candle in range_candles)
        current_price = current_candle["close"]
        range_width_pct = (range_high - range_low) / range_low * 100

        long_score = 0
        short_score = 0
        block_trade = False
        zero_long_score = False
        zero_short_score = False
        reasons = []

        if range_width_pct < self.config.range_min_width_pct:
            block_trade = True
            reasons.append(f"range HOLD: width {range_width_pct:.2f}% below {self.config.range_min_width_pct:.2f}%")

        upper_breakout_price = range_high * (1 + self.config.range_breakout_pct / 100)
        lower_breakdown_price = range_low * (1 - self.config.range_breakout_pct / 100)

        if current_price > upper_breakout_price:
            long_score += self.config.range_breakout_score
            reasons.append(f"range upper breakout LONG +{self.config.range_breakout_score}")
        elif self._pct_distance(current_price, range_high) <= self.config.range_near_pct:
            zero_long_score = True
            reasons.append("range near high: LONG score forced to 0")

        if current_price < lower_breakdown_price:
            short_score += self.config.range_breakout_score
            reasons.append(f"range lower breakdown SHORT +{self.config.range_breakout_score}")
        elif self._pct_distance(current_price, range_low) <= self.config.range_near_pct:
            zero_short_score = True
            reasons.append("range near low: SHORT score forced to 0")

        if not reasons:
            reasons.append("range 0: normal range conditions")

        return {
            "long_score": long_score,
            "short_score": short_score,
            "block_trade": block_trade,
            "zero_long_score": zero_long_score,
            "zero_short_score": zero_short_score,
            "range": {
                "high": range_high,
                "low": range_low,
                "width_pct": range_width_pct,
            },
            "reasons": reasons,
        }

    def _empty_range_result(self, reason):
        return {
            "long_score": 0,
            "short_score": 0,
            "block_trade": False,
            "zero_long_score": False,
            "zero_short_score": False,
            "range": None,
            "reasons": [reason],
        }

    def _pct_distance(self, price, reference_price):
        return abs(price - reference_price) / reference_price * 100

    def _safe_float(self, value, default=0.0):
        try:
            if value is None or value == "":
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _candle_direction(self, candle):
        if candle["close"] > candle["open"]:
            return "UP"

        if candle["close"] < candle["open"]:
            return "DOWN"

        return "FLAT"

    def _calculate_true_ranges(self, candles):
        true_ranges = []

        for index in range(1, len(candles)):
            candle = candles[index]
            previous_close = candles[index - 1]["close"]
            true_range = max(
                candle["high"] - candle["low"],
                abs(candle["high"] - previous_close),
                abs(candle["low"] - previous_close),
            )
            true_ranges.append(true_range)

        return true_ranges

    def _calculate_atr_values(self, true_ranges, period):
        atr_values = []

        for index in range(period - 1, len(true_ranges)):
            atr_window = true_ranges[index - period + 1 : index + 1]
            atr_values.append(sum(atr_window) / period)

        return atr_values

    def calc_price_position_score(self, ohlcv_data, current_candle=None):
        if current_candle is None:
            if len(ohlcv_data) < 2:
                return {"long_score": 0, "short_score": 0, "reasons": ["price_position skipped: not enough candles"]}

            reference_candles = ohlcv_data[:-1]
            current_candle = ohlcv_data[-1]
        else:
            if len(ohlcv_data) < 1:
                return {"long_score": 0, "short_score": 0, "reasons": ["price_position skipped: no reference candles"]}

            reference_candles = ohlcv_data

        periods = [
            ("1y", 365, 3),
            ("6m", 182, 2),
            ("1m", 30, 1),
        ]

        long_score = 0
        short_score = 0
        reasons = []
        current_price = current_candle["close"]

        for label, days, weight in periods:
            candles = reference_candles[-days:]
            if not candles:
                continue

            period_high = max(candle["high"] for candle in candles)
            period_low = min(candle["low"] for candle in candles)

            if current_price > period_high:
                long_score += weight
                reasons.append(f"price_position {label} high breakout LONG +{weight}")

            if current_price < period_low:
                short_score += weight
                reasons.append(f"price_position {label} low breakdown SHORT +{weight}")

        if not reasons:
            reasons.append("price_position 0: inside previous ranges")

        return {"long_score": long_score, "short_score": short_score, "reasons": reasons}

    def calc_body_score(self, ohlcv_data, current_candle=None):
        if current_candle is None:
            if len(ohlcv_data) < 2:
                return {"long_score": 0, "short_score": 0, "reasons": ["body_score skipped: not enough candles"]}

            reference_candles = ohlcv_data[:-1]
            current_candle = ohlcv_data[-1]
        else:
            if len(ohlcv_data) < 1:
                return {"long_score": 0, "short_score": 0, "reasons": ["body_score skipped: no reference candles"]}

            reference_candles = ohlcv_data

        current_body = abs(current_candle["close"] - current_candle["open"])
        reference_bodies = [abs(candle["close"] - candle["open"]) for candle in reference_candles]
        score = self._percentile_to_score(current_body, reference_bodies)

        if current_candle["close"] > current_candle["open"]:
            return {"long_score": score, "short_score": 0, "reasons": [f"body_score LONG +{score}"]}

        if current_candle["close"] < current_candle["open"]:
            return {"long_score": 0, "short_score": score, "reasons": [f"body_score SHORT +{score}"]}

        return {"long_score": 0, "short_score": 0, "reasons": ["body_score 0: current candle is doji"]}

    def calc_volume_score(self, ohlcv_data, current_candle=None, day_progress=None, current_time=None):
        if current_candle is None:
            if len(ohlcv_data) < 2:
                return {"long_score": 0, "short_score": 0, "reasons": ["volume_score skipped: not enough candles"]}

            reference_candles = ohlcv_data[:-1]
            current_candle = ohlcv_data[-1]
            expected_volume = current_candle["volume"]
        else:
            if len(ohlcv_data) < 1:
                return {"long_score": 0, "short_score": 0, "reasons": ["volume_score skipped: no reference candles"]}

            reference_candles = ohlcv_data
            if day_progress is None:
                day_progress = self._calculate_day_progress(current_candle, current_time)
            expected_volume = self._estimate_final_volume(current_candle["volume"], day_progress)

        reference_volumes = [candle["volume"] for candle in reference_candles]
        score = self._percentile_to_score(expected_volume, reference_volumes)

        if current_candle["close"] > current_candle["open"]:
            return {
                "long_score": score,
                "short_score": 0,
                "reasons": [f"volume_score LONG +{score} expected_volume={expected_volume:.2f}"],
            }

        if current_candle["close"] < current_candle["open"]:
            return {
                "long_score": 0,
                "short_score": score,
                "reasons": [f"volume_score SHORT +{score} expected_volume={expected_volume:.2f}"],
            }

        return {"long_score": 0, "short_score": 0, "reasons": ["volume_score 0: current candle is doji"]}

    def _estimate_final_volume(self, current_volume, day_progress):
        if day_progress is None:
            return current_volume

        if day_progress <= 0:
            return current_volume

        if day_progress > 1:
            day_progress = 1

        return current_volume / day_progress

    def _calculate_day_progress(self, current_candle, current_time=None):
        candle_open_time = current_candle.get("open_time", current_candle.get("timestamp"))
        if candle_open_time is None:
            return None

        start_time = self._parse_time(candle_open_time)
        now = self._parse_time(current_time) if current_time is not None else datetime.now(timezone.utc)

        elapsed_seconds = (now - start_time).total_seconds()
        day_seconds = 24 * 60 * 60
        progress = elapsed_seconds / day_seconds

        if progress <= 0:
            return 0
        if progress > 1:
            return 1

        return progress

    def _parse_time(self, value):
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, timezone.utc)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise TypeError(f"Unsupported time value: {value!r}")

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    def _percentile_to_score(self, current_value, reference_values):
        if not reference_values:
            return 0

        lower_or_equal_count = sum(1 for value in reference_values if value <= current_value)
        percentile = lower_or_equal_count / len(reference_values) * 100

        if percentile < 30:
            return 0
        if percentile < 40:
            return 1
        if percentile < 50:
            return 2
        if percentile < 60:
            return 3
        if percentile < 70:
            return 4
        if percentile < 80:
            return 5

        return 6

    def get_state(self, long_score: int, short_score: int) -> str:
        if long_score >= self.config.entry_long_score and long_score - short_score >= self.config.entry_score_gap:
            return "LONG"

        if short_score >= self.config.entry_short_score and short_score - long_score >= self.config.entry_score_gap:
            return "SHORT"

        return "HOLD"

    def get_signal(self, long_score: int, short_score: int) -> str:
        if long_score >= self.config.entry_long_score and long_score - short_score >= self.config.entry_score_gap:
            return "ENTER_LONG"

        if short_score >= self.config.entry_short_score and short_score - long_score >= self.config.entry_score_gap:
            return "ENTER_SHORT"

        return "NO_TRADE"
