import argparse
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .future_labeler import HORIZONS, FutureStateLabeler, _as_utc, _core_score_diff
from .prediction_telegram import send_prediction_alert
from .prediction_summary import (evaluate_forecasts, format_prediction_digest, format_forecast_scores,
                                 summarize_score_changes, summarize_recent_actuals, summarize_persistence_performance)
from .state_recorder import ensure_market_state_extensions


LOGGER = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))
SCHEDULE_HOURS_KST = (0, 6, 12, 18)
FORECAST_VERSION = "per_side_changes_v2"


class MarketStateForecaster:
    """Research-only nearest-score forecaster based on labeled past snapshots."""

    def __init__(self, db_path="work/data/btc_market_state.db", min_case_count=30, score_tolerance=1):
        self.db_path = Path(db_path)
        self.min_case_count = min_case_count
        self.score_tolerance = score_tolerance
        self._initialize()

    def latest_snapshot(self, symbol="BTCUSDT", at_or_before=None):
        if not self.db_path.exists():
            return None
        connection = self._connect()
        try:
            query = "SELECT * FROM market_state WHERE symbol = ?"
            parameters = [symbol]
            if at_or_before is not None:
                query += " AND timestamp <= ?"
                parameters.append(_as_utc(at_or_before).isoformat())
            query += " ORDER BY timestamp DESC LIMIT 1"
            row = connection.execute(query, parameters).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def forecast_for_snapshot(self, source):
        if source is None:
            return None
        source = dict(source)
        source_core_diff = _core_score_diff(source)
        forecast = {
            "forecast_version": FORECAST_VERSION,
            "selection": {"score_tolerance": self.score_tolerance, "min_case_count": self.min_case_count,
                          "outcomes_known_by_source": True, "side_start_bands": ["0_9", "10_plus"]},
            "source_timestamp": source.get("timestamp"),
            "symbol": source.get("symbol"),
            "price": source.get("price"),
            "long_score": source.get("long_score"),
            "short_score": source.get("short_score"),
            "atr_activity_score": source.get("atr_activity_score"),
            "core_score_diff": source_core_diff,
            "strategy_version": source.get("strategy_version"),
            "horizons": {},
        }
        connection = self._connect()
        try:
            for name in HORIZONS:
                forecast["horizons"][name] = self._forecast_horizon(connection, source, source_core_diff, name)
        finally:
            connection.close()
        return forecast

    def get_schedule_record(self, symbol, schedule_key):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM prediction_forecast WHERE symbol = ? AND schedule_key = ?",
                (symbol, schedule_key),
            ).fetchone()
            return self._forecast_record(row)
        finally:
            connection.close()

    def save_forecast(self, forecast, schedule_key, created_at=None):
        created_at = _as_utc(created_at or datetime.now(timezone.utc)).isoformat()
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO prediction_forecast (
                    symbol, schedule_key, created_at, source_timestamp, source_price,
                    source_long_score, source_short_score, source_atr_activity_score,
                    source_core_score_diff, strategy_version, forecast_json, forecast_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    forecast["symbol"], schedule_key, created_at, forecast["source_timestamp"], forecast["price"],
                    forecast["long_score"], forecast["short_score"], forecast["atr_activity_score"],
                    forecast["core_score_diff"], forecast["strategy_version"], _json(forecast),
                    forecast.get("forecast_version", "legacy_core_gap_v1"),
                ),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            connection.close()

    def mark_telegram_sent(self, symbol, schedule_key, sent_at=None):
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE prediction_forecast SET telegram_sent_at = ? WHERE symbol = ? AND schedule_key = ?",
                (_as_utc(sent_at or datetime.now(timezone.utc)).isoformat(), symbol, schedule_key),
            )
            connection.commit()
        finally:
            connection.close()

    def refresh_actual_outcomes(self, symbol="BTCUSDT", now=None):
        """Attach each horizon independently; missing data never means a failed prediction."""
        now = _as_utc(now or datetime.now(timezone.utc))
        connection = self._connect()
        updated = 0
        try:
            records = connection.execute(
                """
                SELECT * FROM prediction_forecast
                WHERE symbol = ? AND outcomes_complete = 0 AND created_at <= ?
                ORDER BY created_at ASC
                """,
                (symbol, now.isoformat()),
            ).fetchall()
            for record in records:
                source = connection.execute(
                    "SELECT * FROM market_state WHERE symbol = ? AND timestamp = ?",
                    (symbol, record["source_timestamp"]),
                ).fetchone()
                if source is None:
                    continue
                source = dict(source)
                outcome = self._actual_outcome(source, now=now)
                previous = _load_json(record["actual_outcomes_json"]) or {}
                for name, item in outcome.items():
                    old = previous.get(name) or {}
                    if item["timestamp"] and item["long_score"] is not None and item["short_score"] is not None:
                        if old.get("confirmed_at"):
                            item["confirmed_at"] = old["confirmed_at"]
                        elif not (old.get("timestamp") and old.get("long_score") is not None
                                  and old.get("short_score") is not None):
                            item["confirmed_at"] = now.isoformat()
                if not any(item["timestamp"] for item in outcome.values()):
                    continue
                complete = all(item["timestamp"] and item["long_score"] is not None
                               and item["short_score"] is not None and item["core_score_change"] is not None
                               for item in outcome.values())
                if _load_json(record["actual_outcomes_json"]) == outcome and not complete:
                    continue
                connection.execute(
                    """
                    UPDATE prediction_forecast
                    SET actual_outcomes_json = ?, actual_labeled_at = ?, outcomes_complete = ?
                    WHERE id = ?
                    """,
                    (_json(outcome), now.isoformat(), int(complete), record["id"]),
                )
                updated += 1
            connection.commit()
        finally:
            connection.close()
        return updated

    def _forecast_horizon(self, connection, source, source_core_diff, name):
        if name not in HORIZONS:
            raise ValueError("unsupported prediction horizon")
        future_core_column = f"future_{name}_core_score_diff"
        future_entry_column = f"future_{name}_entry_condition"
        source_atr = source.get("atr_activity_score") or 0
        rows = connection.execute(
            f"""
            SELECT {future_core_column}, {future_entry_column}, long_score, short_score,
                   future_{name}_long_score, future_{name}_short_score
            FROM market_state
            WHERE symbol = ?
              AND strategy_version = ?
              AND timestamp < ?
              AND ABS(COALESCE(long_score, 0) - ?) <= ?
              AND ABS(COALESCE(short_score, 0) - ?) <= ?
              AND ABS(COALESCE(atr_activity_score, 0) - ?) <= ?
              AND {future_core_column} IS NOT NULL
              AND future_{name}_timestamp <= ?
            """,
            (
                source.get("symbol"), source.get("strategy_version"), source.get("timestamp"),
                source.get("long_score") or 0, self.score_tolerance,
                source.get("short_score") or 0, self.score_tolerance,
                source_atr, self.score_tolerance,
                source.get("timestamp"),
            ),
        ).fetchall()
        count = len(rows)
        long_count = sum(row[future_core_column] - source_core_diff >= 3 for row in rows)
        short_count = sum(row[future_core_column] - source_core_diff <= -3 for row in rows)
        stable_count = count - long_count - short_count
        entry_count = sum(bool(row[future_entry_column]) for row in rows)
        return {
            "case_count": count,
            "ready": count >= self.min_case_count,
            "long_strengthen_pct": _percent(long_count, count),
            "short_strengthen_pct": _percent(short_count, count),
            "stable_pct": _percent(stable_count, count),
            "entry_condition_pct": _percent(entry_count, count),
            "score_changes": {
                side: summarize_score_changes(
                    source.get(f"{side.lower()}_score") or 0,
                    [(row[f"{side.lower()}_score"], row[f"future_{name}_{side.lower()}_score"]) for row in rows],
                    self.min_case_count,
                ) for side in ("LONG", "SHORT")
            },
        }

    @staticmethod
    def _actual_outcome(source, now=None):
        source_core_diff = _core_score_diff(source)
        output = {}
        for name in HORIZONS:
            timestamp = source.get(f"future_{name}_timestamp")
            known = bool(timestamp and (now is None or _as_utc(timestamp) <= now))
            future_core_diff = source.get(f"future_{name}_core_score_diff") if known else None
            core_change = None if future_core_diff is None else future_core_diff - source_core_diff
            long_score = source.get(f"future_{name}_long_score") if known else None
            short_score = source.get(f"future_{name}_short_score") if known else None
            entry = source.get(f"future_{name}_entry_condition") if known else None
            output[name] = {
                "timestamp": timestamp if known else None,
                "long_score": long_score,
                "short_score": short_score,
                "long_score_change": None if long_score is None else long_score - source["long_score"],
                "short_score_change": None if short_score is None else short_score - source["short_score"],
                "core_score_change": core_change,
                "return_pct": source.get(f"return_{name}") if known else None,
                "entry_condition": None if entry is None else bool(entry),
            }
        return output

    def _initialize(self):
        if not self.db_path.exists():
            return
        connection = self._connect()
        try:
            ensure_market_state_extensions(connection)
            connection.execute(CREATE_PREDICTION_FORECAST_TABLE)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(prediction_forecast)")}
            for name, definition in {"forecast_version": "TEXT NOT NULL DEFAULT 'legacy_core_gap_v1'",
                                     "outcomes_complete": "INTEGER NOT NULL DEFAULT 0"}.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE prediction_forecast ADD COLUMN {name} {definition}")
            connection.execute(CREATE_PREDICTION_DIGEST_TABLE)
            connection.commit()
        finally:
            connection.close()

    def _connect(self):
        connection = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _forecast_record(record):
        if record is None:
            return None
        output = dict(record)
        output["forecast"] = _load_json(output.pop("forecast_json", None))
        output["actual_outcomes"] = _load_json(output.pop("actual_outcomes_json", None))
        return output

    def recent_forecasts(self, symbol, since, until):
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM prediction_forecast WHERE symbol = ? AND created_at >= ? AND created_at <= ? ORDER BY created_at",
                (symbol, _as_utc(since).isoformat(), _as_utc(until).isoformat()),
            ).fetchall()
            return [self._forecast_record(row) for row in rows]
        finally:
            connection.close()

    def save_digest(self, symbol, schedule_key, message, evaluation, now, previous_sent_at=None):
        connection = self._connect()
        try:
            connection.execute(
                """INSERT OR IGNORE INTO prediction_digest
                   (symbol, schedule_key, created_at, message, evaluation_json, telegram_sent_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (symbol, schedule_key, now.isoformat(), message, _json(evaluation), previous_sent_at),
            )
            connection.commit()
            return dict(connection.execute(
                "SELECT * FROM prediction_digest WHERE symbol = ? AND schedule_key = ?", (symbol, schedule_key)
            ).fetchone())
        finally:
            connection.close()

    def report_context(self, symbol, forecast, now):
        """Read comparison inputs; no additional forecasts or market requests."""
        since = now - timedelta(hours=6)
        source_time = _as_utc(forecast["source_timestamp"]) if forecast else now
        earlier = self.latest_snapshot(symbol, at_or_before=source_time - timedelta(hours=1))
        if earlier and (source_time - timedelta(hours=1) - _as_utc(earlier["timestamp"])).total_seconds() > 120:
            earlier = None
        connection = self._connect()
        try:
            previous = connection.execute(
                """SELECT * FROM prediction_forecast WHERE symbol=? AND source_timestamp<? AND created_at<=?
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (symbol, source_time.isoformat(), now.isoformat()),
            ).fetchone()
            records = connection.execute(
                """SELECT * FROM prediction_forecast WHERE symbol=? AND created_at<=?
                   AND (source_timestamp>=? OR actual_labeled_at>?) ORDER BY created_at""",
                (symbol, now.isoformat(), (since - timedelta(hours=4, minutes=10)).isoformat(), since.isoformat()),
            ).fetchall()
            records = [self._forecast_record(row) for row in records]
            # A changed scoring version cannot be treated as a prediction error.
            for record in records:
                for actual in (record.get("actual_outcomes") or {}).values():
                    if actual.get("timestamp"):
                        target = connection.execute(
                            "SELECT strategy_version FROM market_state WHERE symbol=? AND timestamp=?",
                            (symbol, actual["timestamp"]),
                        ).fetchone()
                        actual["report_strategy_matches"] = bool(target and target[0] == record["strategy_version"])
        finally:
            connection.close()
        return {
            "previous_snapshot": {key: earlier.get(key) for key in
                                  ("timestamp", "long_score", "short_score", "strategy_version")} if earlier else None,
            "previous_forecast": self._forecast_record(previous),
            "recent_actuals": summarize_recent_actuals(records, now),
            "persistence_performance": self.persistence_performance(symbol, now),
        }

    def persistence_performance(self, symbol, now):
        """Read historical v2 predictions without reforecasting or relabeling them."""
        connection = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """SELECT * FROM prediction_forecast WHERE symbol=? AND forecast_version=?
                   AND created_at<=? ORDER BY source_timestamp""",
                (symbol, FORECAST_VERSION, _as_utc(now).isoformat()),
            )
            def records():
                for row in rows:
                    record = self._forecast_record(row)
                    source = connection.execute(
                        "SELECT strategy_version, long_score, short_score FROM market_state WHERE symbol=? AND timestamp=?",
                        (symbol, record["source_timestamp"]),
                    ).fetchone()
                    forecast = record["forecast"]
                    record["report_source_matches"] = bool(
                        source and source[0] == record["strategy_version"]
                        and source[1] == forecast.get("long_score") and source[2] == forecast.get("short_score"))
                    for actual in (record.get("actual_outcomes") or {}).values():
                        if actual.get("timestamp"):
                            target = connection.execute(
                                "SELECT strategy_version FROM market_state WHERE symbol=? AND timestamp=?",
                                (symbol, actual["timestamp"]),
                            ).fetchone()
                            actual["report_strategy_matches"] = bool(target and target[0] == record["strategy_version"])
                    yield record
            return summarize_persistence_performance(records(), _as_utc(now))
        finally:
            connection.close()

    def mark_digest_sent(self, symbol, schedule_key, now):
        connection = self._connect()
        try:
            for table in ("prediction_digest", "prediction_forecast"):
                connection.execute(f"UPDATE {table} SET telegram_sent_at = ? WHERE symbol = ? AND schedule_key = ?",
                                   (now.isoformat(), symbol, schedule_key))
            connection.commit()
        finally:
            connection.close()


class PredictionResearchScheduler:
    """Save hourly forecasts; deliver one digest at 00/06/12/18 KST."""

    def __init__(self, db_path="work/data/btc_market_state.db", min_case_count=30, report_days=7):
        if min_case_count < 1 or report_days < 1:
            raise ValueError("min_case_count and report_days must be positive")
        self.report_days = report_days
        self.labeler = FutureStateLabeler(db_path)
        self.forecaster = MarketStateForecaster(db_path, min_case_count=min_case_count)

    def run_once(self, symbol="BTCUSDT", now=None, send_telegram=False, force=False):
        now = _as_utc(now or datetime.now(timezone.utc))
        if not self.forecaster.db_path.exists():
            return {"status": "missing_database"}
        now_kst = now.astimezone(KST)
        if not force and now_kst.minute > 4:
            return {"status": "not_scheduled"}
        label_counts = self.labeler.label_available(symbol=symbol, now=now)
        completed_count = self.forecaster.refresh_actual_outcomes(symbol=symbol, now=now)

        schedule_key = now_kst.strftime("%Y-%m-%d-%H")
        record = self.forecaster.get_schedule_record(symbol, schedule_key)
        warning = None
        if record is None:
            source = self.forecaster.latest_snapshot(symbol=symbol, at_or_before=now)
            if source is None:
                warning = "새 예측 없음: 시장 기록이 없습니다."
            elif (now - _as_utc(source["timestamp"])).total_seconds() > 120:
                warning = "새 예측 보류: 시장 기록이 2분 이상 지연됐습니다."
            else:
                forecast = self.forecaster.forecast_for_snapshot(source)
                self.forecaster.save_forecast(forecast, schedule_key, created_at=now)
                record = self.forecaster.get_schedule_record(symbol, schedule_key)

        result = {"status": "recorded" if record else "missing_snapshot", "schedule_key": schedule_key,
                  "label_counts": label_counts, "completed_count": completed_count,
                  "forecast": record["forecast"] if record else None, "warning": warning}
        if not force and now_kst.hour not in SCHEDULE_HOURS_KST:
            result["message"] = warning or f"hourly prediction saved: {schedule_key}; Telegram digest at 00/06/12/18 KST"
            return result

        slot = now.replace(minute=0, second=0, microsecond=0)
        recent = self.forecaster.recent_forecasts(symbol, slot - timedelta(hours=5), now)
        history = self.forecaster.recent_forecasts(symbol, now - timedelta(days=self.report_days), now)
        evaluation = evaluate_forecasts(history, now)
        evaluation["report"] = self.forecaster.report_context(symbol, record["forecast"] if record else None, now)
        message = format_prediction_digest(record["forecast"] if record else None, recent, evaluation, now,
                                           days=self.report_days, warning=warning, context=evaluation["report"])
        digest = self.forecaster.save_digest(symbol, schedule_key, message, evaluation, now,
                                            previous_sent_at=record.get("telegram_sent_at") if record else None)
        sent = False
        if send_telegram and digest["telegram_sent_at"] is None:
            sent = send_prediction_alert(digest["message"])
            if sent:
                self.forecaster.mark_digest_sent(symbol, schedule_key, now)
        result.update({"status": "sent" if sent else "ready", "message": digest["message"],
                       "evaluation": _load_json(digest["evaluation_json"])})
        return result


def format_prediction_report(forecast, previous_actual_outcomes=None):
    if forecast.get("forecast_version") == FORECAST_VERSION:
        return "\n".join(["BTCUSDT Score Research", *format_forecast_scores(forecast)])
    lines = [
        "BTCUSDT Score Research",
        "",
        "current_scores:",
        f"- LONG: {forecast.get('long_score')}",
        f"- SHORT: {forecast.get('short_score')}",
        f"- ATR: {forecast.get('atr_activity_score')}",
        f"- core_diff_without_ATR: {forecast.get('core_score_diff')}",
        "",
        "score_direction_forecast:",
    ]
    for name in ("15m", "1h", "4h"):
        item = (forecast.get("horizons") or {}).get(name) or {}
        if not item.get("ready"):
            lines.append(f"- {name}: data insufficient (cases: {item.get('case_count', 0)})")
            continue
        lines.extend(
            [
                f"- {name}: LONG strengthen {item['long_strengthen_pct']:.1f}% | "
                f"SHORT strengthen {item['short_strengthen_pct']:.1f}% | stable {item['stable_pct']:.1f}%",
                f"  cases: {item['case_count']} | future entry condition: {item['entry_condition_pct']:.1f}%",
            ]
        )

    if previous_actual_outcomes:
        lines.extend(["", "previous_forecast_actual:"])
        for name in ("15m", "1h", "4h"):
            item = previous_actual_outcomes.get(name) or {}
            lines.append(
                f"- {name}: LONG {item.get('long_score')} | SHORT {item.get('short_score')} | "
                f"core_change {_format_signed_int(item.get('core_score_change'))} | "
                f"return {_format_pct(item.get('return_pct'))} | "
                f"entry {'YES' if item.get('entry_condition') else 'NO'}"
            )
    return "\n".join(lines)


def _percent(value, total):
    return 0.0 if total == 0 else value / total * 100


def _format_pct(value):
    return "None" if value is None else f"{value:+.2f}%"


def _format_signed_int(value):
    return "None" if value is None else f"{value:+d}"


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load_json(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


CREATE_PREDICTION_FORECAST_TABLE = """
CREATE TABLE IF NOT EXISTS prediction_forecast (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    schedule_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_timestamp TEXT NOT NULL,
    source_price REAL,
    source_long_score INTEGER,
    source_short_score INTEGER,
    source_atr_activity_score INTEGER,
    source_core_score_diff INTEGER,
    strategy_version TEXT,
    forecast_json TEXT NOT NULL,
    actual_outcomes_json TEXT,
    actual_labeled_at TEXT,
    telegram_sent_at TEXT,
    forecast_version TEXT NOT NULL DEFAULT 'legacy_core_gap_v1',
    outcomes_complete INTEGER NOT NULL DEFAULT 0,
    UNIQUE (symbol, schedule_key)
)
"""

CREATE_PREDICTION_DIGEST_TABLE = """
CREATE TABLE IF NOT EXISTS prediction_digest (
    symbol TEXT NOT NULL,
    schedule_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    message TEXT NOT NULL,
    evaluation_json TEXT NOT NULL,
    telegram_sent_at TEXT,
    PRIMARY KEY (symbol, schedule_key)
)
"""


def main():
    parser = argparse.ArgumentParser(description="Research-only market-state score forecast")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--market-state-db-path", default="work/data/btc_market_state.db")
    parser.add_argument("--min-case-count", type=int, default=30)
    parser.add_argument("--report-days", type=int, default=7)
    parser.add_argument("--send-telegram", action="store_true")
    parser.add_argument("--force", action="store_true", help="Manually save an hourly forecast and preview/send its digest now")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    scheduler = PredictionResearchScheduler(
        db_path=args.market_state_db_path,
        min_case_count=args.min_case_count,
        report_days=args.report_days,
    )
    result = scheduler.run_once(
        symbol=args.symbol,
        send_telegram=args.send_telegram,
        force=args.force,
    )
    print(result.get("message") or result)


if __name__ == "__main__":
    main()
