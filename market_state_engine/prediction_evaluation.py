"""Read-only, strategy-separated evaluation of issued four-hour v2 forecasts."""

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3

from .prediction_summary import _most_likely


V2 = "per_side_changes_v2"
FOUR_HOURS = timedelta(hours=4)


class Excluded(ValueError):
    pass


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _object(value):
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError("expected JSON object")
    return parsed


def _sample(connection, row, now):
    forecast = _object(row["forecast_json"])
    version = row.get("forecast_version") or forecast.get("forecast_version")
    if version != V2 or forecast.get("forecast_version") != V2:
        raise Excluded("not_forecast_v2")
    start, created = _time(row["source_timestamp"]), _time(row["created_at"])
    if created > now:
        raise Excluded("not_yet_issued")
    if not 0 <= (created - start).total_seconds() <= 120:
        raise Excluded("stale_source")
    if now < start + FOUR_HOURS:
        raise Excluded("pending")
    strategy = row["strategy_version"]
    if row["source_strategy"] != strategy or forecast.get("strategy_version") != strategy:
        raise Excluded("strategy_mismatch")

    # Source labels are authoritative; a cached outcome can supply an older missing label.
    actual = {"timestamp": row["label_timestamp"], "long_score": row["label_long"],
              "short_score": row["label_short"]}
    if not actual["timestamp"]:
        actual = _object(row.get("actual_outcomes_json") or "{}").get("4h") or {}
    if not actual.get("timestamp"):
        raise Excluded("missing_actual")
    end = _time(actual["timestamp"])
    if end > now:
        raise Excluded("pending")
    if not start + FOUR_HOURS <= end <= start + FOUR_HOURS + timedelta(minutes=10):
        raise Excluded("invalid_label_time")
    target = connection.execute(
        "SELECT strategy_version, long_score, short_score FROM market_state WHERE symbol=? AND timestamp=?",
        (row["symbol"], actual["timestamp"]),
    ).fetchone()
    if target is None:
        raise Excluded("missing_target_snapshot")
    if target["strategy_version"] != strategy:
        raise Excluded("strategy_mismatch")
    # Also reject an interval that crossed a different version and then switched back.
    crossed = connection.execute(
        """SELECT 1 FROM market_state WHERE symbol=? AND timestamp>? AND timestamp<=?
           AND (strategy_version IS NULL OR strategy_version != ?) LIMIT 1""",
        (row["symbol"], row["source_timestamp"], actual["timestamp"], strategy),
    ).fetchone()
    if crossed:
        raise Excluded("strategy_mismatch")
    sides = {}
    items = forecast["horizons"]["4h"].get("score_changes") or {}
    for side in ("LONG", "SHORT"):
        name = side.lower()
        item = items.get(side) or {}
        current = row[f"source_{name}_score"]
        score = actual.get(f"{name}_score")
        predicted = item.get("median_score")
        if not item.get("ready") or not all(_number(x) and x >= 0 for x in (current, score, predicted)):
            continue
        if (item.get("start_score") != current or row[f"observed_{name}"] != current
                or target[f"{name}_score"] != score):
            continue
        sides[side] = {"current": current, "actual": score, "predicted": predicted, "item": item}
    if not sides:
        raise Excluded("no_evaluable_side")
    return {"id": row["id"], "start": start, "end": end, "sides": sides}


def _event_metrics(pairs, event):
    predicted = sum(p == event for p, a in pairs)
    actual = sum(a == event for p, a in pairs)
    hits = sum(p == event and a == event for p, a in pairs)
    return {"count": len(pairs), "true_positive": hits, "predicted_count": predicted,
            "actual_count": actual, "precision": hits / predicted if predicted else None,
            "recall": hits / actual if actual else None}


def _metrics(samples):
    output = {"sample_count": len(samples), "forecast_ids": [s["id"] for s in samples], "sides": {}}
    for side in ("LONG", "SHORT"):
        rows = [s["sides"][side] for s in samples if side in s["sides"]]
        n = len(rows)
        model = sum(abs(r["predicted"] - r["actual"]) for r in rows) / n if n else None
        baseline = sum(abs(r["current"] - r["actual"]) for r in rows) / n if n else None
        result = {"count": n, "forecast_mae": model, "persistence_mae": baseline,
                  "improvement_pct": (baseline - model) / baseline * 100 if baseline else None,
                  "large_changes": {}}
        for threshold in (7, 10):
            pairs = []
            for row in rows:
                probabilities = {key: row["item"].get(f"{key}_{threshold}_pct")
                                 for key in ("stable", "up", "down")}
                if (not all(_number(v) and 0 <= v <= 100 for v in probabilities.values())
                        or abs(sum(probabilities.values()) - 100) > 0.1):
                    continue
                change = row["actual"] - row["current"]
                actual = "up" if change >= threshold else "down" if change <= -threshold else "stable"
                pairs.append((_most_likely(probabilities), actual))
            result["large_changes"][str(threshold)] = {
                "probability_missing_count": n - len(pairs),
                "up": _event_metrics(pairs, "up"), "down": _event_metrics(pairs, "down"),
                "any_change": _event_metrics([(p != "stable", a != "stable") for p, a in pairs], True),
            }
        output["sides"][side] = result
    return output


def _non_overlapping(samples):
    selected = []
    next_start = None
    for sample in sorted(samples, key=lambda s: (s["start"], s["id"])):
        if next_start is None or sample["start"] >= next_start:
            selected.append(sample)
            next_start = max(sample["start"] + FOUR_HOURS, sample["end"])
    return selected


def evaluate_database(db_path="work/data/btc_market_state.db", symbol="BTCUSDT", now=None):
    """Read existing labels only. Never migrate, label, forecast, or write to the database."""
    now = _time(now or datetime.now(timezone.utc))
    groups = {}
    unknown = 0
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        records = connection.execute(
            """SELECT f.*, s.strategy_version AS source_strategy,
                      s.long_score AS observed_long, s.short_score AS observed_short,
                      s.future_4h_timestamp AS label_timestamp,
                      s.future_4h_long_score AS label_long, s.future_4h_short_score AS label_short
               FROM prediction_forecast f LEFT JOIN market_state s
                 ON s.symbol=f.symbol AND s.timestamp=f.source_timestamp
               WHERE f.symbol=? ORDER BY f.source_timestamp, f.id""", (symbol,),
        )
        for raw in records:
            row = dict(raw)
            strategy = row["strategy_version"]
            if not strategy:
                unknown += 1
                continue
            group = groups.setdefault(strategy, {"input_count": 0, "excluded": Counter(), "samples": []})
            group["input_count"] += 1
            try:
                group["samples"].append(_sample(connection, row, now))
            except Excluded as error:
                group["excluded"][str(error)] += 1
            except (ValueError, TypeError, KeyError, AttributeError):
                group["excluded"]["invalid_record"] += 1
    output = {"symbol": symbol, "as_of": now.isoformat(), "forecast_version": V2,
              "point_estimate": "median_score", "classification": "argmax_stable_up_down",
              "unknown_strategy_excluded": unknown, "strategies": {}}
    for strategy, group in sorted(groups.items()):
        output["strategies"][strategy] = {
            "input_count": group["input_count"], "excluded": dict(group["excluded"]),
            "all": _metrics(group["samples"]),
            "non_overlapping": _metrics(_non_overlapping(group["samples"])),
        }
    return output


def format_evaluation(result):
    def number(value):
        return "N/A" if value is None else f"{value:.3f}"

    lines = [f"{result['symbol']} 4h forecast v2 vs persistence", f"as_of: {result['as_of']}",
             "estimate: median_score; improvement=(persistence-MAE)/persistence*100",
             "events: highest-probability class; ties prefer stable; any_change ignores direction",
             "N/A: no samples/zero denominator; pending outcomes are not errors",
             f"unknown_strategy_excluded: {result['unknown_strategy_excluded']}"]
    if not result["strategies"]:
        lines.append("No forecasts available.")
    for version, group in result["strategies"].items():
        lines.extend(["", f"strategy_version: {version}",
                      f"input={group['input_count']} excluded={json.dumps(group['excluded'], sort_keys=True)}"])
        for cohort in ("all", "non_overlapping"):
            stats = group[cohort]
            lines.append(f"{cohort}: {stats['sample_count']} forecasts")
            for side, values in stats["sides"].items():
                lines.append(f" {side} n={values['count']} forecast_MAE={number(values['forecast_mae'])}"
                             f" persistence_MAE={number(values['persistence_mae'])}"
                             f" improvement_pct={number(values['improvement_pct'])}")
                for threshold, events in values["large_changes"].items():
                    for event in ("up", "down", "any_change"):
                        metric = events[event]
                        lines.append(f"  {threshold} {event}: n={metric['count']} actual={metric['actual_count']}"
                                     f" predicted={metric['predicted_count']} TP={metric['true_positive']}"
                                     f" precision={number(metric['precision'])} recall={number(metric['recall'])}")
                    if events["probability_missing_count"]:
                        lines.append(f"  missing probabilities: {events['probability_missing_count']}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-state-db-path", default="work/data/btc_market_state.db")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--as-of", help="UTC/offset ISO timestamp; default: now")
    parser.add_argument("--json", action="store_true", help="Print detailed machine-readable results")
    args = parser.parse_args()
    try:
        result = evaluate_database(args.market_state_db_path, args.symbol, args.as_of)
    except (sqlite3.Error, OSError, ValueError) as error:
        parser.exit(1, f"Evaluation failed (database was not modified): {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) if args.json else format_evaluation(result))


if __name__ == "__main__":
    main()
