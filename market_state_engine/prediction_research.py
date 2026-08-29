import argparse
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .future_labeler import HORIZONS, FutureStateLabeler, _as_utc, _core_score_diff
from .prediction_telegram import send_prediction_alert
from .state_recorder import ensure_market_state_extensions


LOGGER = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))
SCHEDULE_HOURS_KST = (0, 6, 12, 18)


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
                    source_core_score_diff, strategy_version, forecast_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    forecast["symbol"], schedule_key, created_at, forecast["source_timestamp"], forecast["price"],
                    forecast["long_score"], forecast["short_score"], forecast["atr_activity_score"],
                    forecast["core_score_diff"], forecast["strategy_version"], _json(forecast),
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

    def refresh_actual_outcomes(self, symbol="BTCUSDT"):
        """Attach actual 15m/1h/4h outcomes once a saved forecast has matured."""
        connection = self._connect()
        updated = 0
        try:
            records = connection.execute(
                """
                SELECT * FROM prediction_forecast
                WHERE symbol = ? AND actual_outcomes_json IS NULL
                ORDER BY created_at ASC
                """,
                (symbol,),
            ).fetchall()
            for record in records:
                source = connection.execute(
                    "SELECT * FROM market_state WHERE symbol = ? AND timestamp = ?",
                    (symbol, record["source_timestamp"]),
                ).fetchone()
                if source is None or source["future_4h_timestamp"] is None:
                    continue
                source = dict(source)
                outcome = self._actual_outcome(source)
                connection.execute(
                    """
                    UPDATE prediction_forecast
                    SET actual_outcomes_json = ?, actual_labeled_at = ?
                    WHERE id = ?
                    """,
                    (_json(outcome), datetime.now(timezone.utc).isoformat(), record["id"]),
                )
                updated += 1
            connection.commit()
        finally:
            connection.close()
        return updated

    def _forecast_horizon(self, connection, source, source_core_diff, name):
        future_core_column = f"future_{name}_core_score_diff"
        future_entry_column = f"future_{name}_entry_condition"
        source_atr = source.get("atr_activity_score") or 0
        rows = connection.execute(
            f"""
            SELECT {future_core_column}, {future_entry_column}
            FROM market_state
            WHERE symbol = ?
              AND strategy_version = ?
              AND timestamp < ?
              AND ABS(COALESCE(long_score, 0) - ?) <= ?
              AND ABS(COALESCE(short_score, 0) - ?) <= ?
              AND ABS(COALESCE(atr_activity_score, 0) - ?) <= ?
              AND {future_core_column} IS NOT NULL
            """,
            (
                source.get("symbol"), source.get("strategy_version"), source.get("timestamp"),
                source.get("long_score") or 0, self.score_tolerance,
                source.get("short_score") or 0, self.score_tolerance,
                source_atr, self.score_tolerance,
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
        }

    @staticmethod
    def _actual_outcome(source):
        source_core_diff = _core_score_diff(source)
        output = {}
        for name in HORIZONS:
            future_core_diff = source.get(f"future_{name}_core_score_diff")
            core_change = None if future_core_diff is None else future_core_diff - source_core_diff
            output[name] = {
                "timestamp": source.get(f"future_{name}_timestamp"),
                "long_score": source.get(f"future_{name}_long_score"),
                "short_score": source.get(f"future_{name}_short_score"),
                "core_score_change": core_change,
                "return_pct": source.get(f"return_{name}"),
                "entry_condition": bool(source.get(f"future_{name}_entry_condition")),
            }
        return output

    def _initialize(self):
        if not self.db_path.exists():
            return
        connection = self._connect()
        try:
            ensure_market_state_extensions(connection)
            connection.execute(CREATE_PREDICTION_FORECAST_TABLE)
            connection.commit()
        finally:
            connection.close()

    def _connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=5)
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


class PredictionResearchScheduler:
    """Run research reports at 00:00, 06:00, 12:00, and 18:00 KST only."""

    def __init__(self, db_path="work/data/btc_market_state.db", min_case_count=30):
        self.labeler = FutureStateLabeler(db_path)
        self.forecaster = MarketStateForecaster(db_path, min_case_count=min_case_count)

    def run_once(self, symbol="BTCUSDT", now=None, send_telegram=False, force=False):
        now = _as_utc(now or datetime.now(timezone.utc))
        label_counts = self.labeler.label_available(symbol=symbol, now=now)
        completed_count = self.forecaster.refresh_actual_outcomes(symbol=symbol)
        now_kst = now.astimezone(KST)
        if not force and (now_kst.hour not in SCHEDULE_HOURS_KST or now_kst.minute > 4):
            return {
                "status": "not_scheduled",
                "label_counts": label_counts,
                "completed_count": completed_count,
            }

        schedule_key = now_kst.strftime("%Y-%m-%d-%H")
        record = self.forecaster.get_schedule_record(symbol, schedule_key)
        if record is None:
            source = self.forecaster.latest_snapshot(symbol=symbol, at_or_before=now)
            forecast = self.forecaster.forecast_for_snapshot(source)
            if forecast is None:
                return {
                    "status": "missing_snapshot",
                    "label_counts": label_counts,
                    "completed_count": completed_count,
                }
            self.forecaster.save_forecast(forecast, schedule_key, created_at=now)
            record = self.forecaster.get_schedule_record(symbol, schedule_key)

        previous_key = (now_kst - timedelta(hours=6)).strftime("%Y-%m-%d-%H")
        previous = self.forecaster.get_schedule_record(symbol, previous_key)
        message = format_prediction_report(record["forecast"], previous.get("actual_outcomes") if previous else None)
        sent = False
        if send_telegram and record.get("telegram_sent_at") is None:
            sent = send_prediction_alert(message)
            if sent:
                self.forecaster.mark_telegram_sent(symbol, schedule_key, sent_at=now)

        return {
            "status": "sent" if sent else "ready",
            "label_counts": label_counts,
            "completed_count": completed_count,
            "schedule_key": schedule_key,
            "message": message,
            "forecast": record["forecast"],
            "previous_actual_outcomes": previous.get("actual_outcomes") if previous else None,
        }


def format_prediction_report(forecast, previous_actual_outcomes=None):
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
    UNIQUE (symbol, schedule_key)
)
"""


def main():
    parser = argparse.ArgumentParser(description="Research-only market-state score forecast")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--market-state-db-path", default="work/data/btc_market_state.db")
    parser.add_argument("--min-case-count", type=int, default=30)
    parser.add_argument("--send-telegram", action="store_true")
    parser.add_argument("--force", action="store_true", help="Run now outside the KST schedule")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    scheduler = PredictionResearchScheduler(
        db_path=args.market_state_db_path,
        min_case_count=args.min_case_count,
    )
    result = scheduler.run_once(
        symbol=args.symbol,
        send_telegram=args.send_telegram,
        force=args.force,
    )
    print(result.get("message") or result)


if __name__ == "__main__":
    main()
