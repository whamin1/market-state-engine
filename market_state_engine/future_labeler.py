import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state_recorder import ensure_market_state_extensions


LOGGER = logging.getLogger(__name__)

HORIZONS = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}


class FutureStateLabeler:
    """Fill future outcomes using snapshots already stored in SQLite.

    It never fetches market data and never participates in live trade decisions.
    """

    def __init__(
        self,
        db_path="work/data/btc_market_state.db",
        entry_long_score=10,
        entry_short_score=10,
        entry_score_gap=5,
        max_snapshot_lag_minutes=10,
    ):
        self.db_path = Path(db_path)
        self.entry_long_score = entry_long_score
        self.entry_short_score = entry_short_score
        self.entry_score_gap = entry_score_gap
        self.max_snapshot_lag = timedelta(minutes=max_snapshot_lag_minutes)

    def label_available(self, symbol="BTCUSDT", now=None):
        """Label every source snapshot whose actual future snapshot is ready."""
        if not self.db_path.exists():
            return {name: 0 for name in HORIZONS}

        now = _as_utc(now or datetime.now(timezone.utc))
        counts = {name: 0 for name in HORIZONS}
        try:
            connection = self._connect()
            try:
                ensure_market_state_extensions(connection)
                for name, duration in HORIZONS.items():
                    counts[name] = self._label_horizon(connection, symbol, now, name, duration)
                connection.commit()
            finally:
                connection.close()
        except (sqlite3.Error, OSError, ValueError):
            LOGGER.exception("future state labeling failed; research data will retry later")
        return counts

    def _label_horizon(self, connection, symbol, now, name, duration):
        timestamp_column = f"future_{name}_timestamp"
        cutoff = (now - duration).isoformat()
        sources = connection.execute(
            f"""
            SELECT * FROM market_state
            WHERE symbol = ?
              AND timestamp <= ?
              AND {timestamp_column} IS NULL
            ORDER BY timestamp ASC
            """,
            (symbol, cutoff),
        ).fetchall()
        columns = [item[0] for item in connection.execute("SELECT * FROM market_state LIMIT 0").description]
        count = 0
        for source_values in sources:
            source = dict(zip(columns, source_values))
            target_time = _as_utc(source["timestamp"]) + duration
            target = connection.execute(
                """
                SELECT * FROM market_state
                WHERE symbol = ? AND timestamp >= ?
                ORDER BY timestamp ASC
                LIMIT 1
                """,
                (symbol, target_time.isoformat()),
            ).fetchone()
            if target is None:
                continue

            target = dict(zip(columns, target))
            target_timestamp = _as_utc(target["timestamp"])
            if target_timestamp - target_time > self.max_snapshot_lag:
                continue

            target_core_diff = _core_score_diff(target)
            entry_condition = self._entry_condition(target)
            price = source.get("price")
            future_price = target.get("price")
            return_pct = None
            if price not in (None, 0) and future_price is not None:
                return_pct = (future_price - price) / price * 100

            connection.execute(
                f"""
                UPDATE market_state
                SET future_{name}_long_score = ?,
                    future_{name}_short_score = ?,
                    {timestamp_column} = ?,
                    future_{name}_core_score_diff = ?,
                    return_{name} = ?,
                    future_{name}_entry_condition = ?
                WHERE timestamp = ? AND symbol = ?
                """,
                (
                    target.get("long_score"),
                    target.get("short_score"),
                    target.get("timestamp"),
                    target_core_diff,
                    return_pct,
                    int(entry_condition),
                    source["timestamp"],
                    symbol,
                ),
            )
            count += 1
        return count

    def _entry_condition(self, row):
        config = _load_json(row.get("strategy_config_json"))
        long_threshold = config.get("entry_long_score", self.entry_long_score)
        short_threshold = config.get("entry_short_score", self.entry_short_score)
        score_gap = config.get("entry_score_gap", self.entry_score_gap)
        long_score = row.get("long_score") or 0
        short_score = row.get("short_score") or 0
        return (
            long_score >= long_threshold and long_score - short_score >= score_gap
        ) or (
            short_score >= short_threshold and short_score - long_score >= score_gap
        )

    def _connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.row_factory = sqlite3.Row
        return connection


def _core_score_diff(row):
    long_score = row.get("long_score") or 0
    short_score = row.get("short_score") or 0
    long_activity = row.get("activity_direction_long_score") or 0
    short_activity = row.get("activity_direction_short_score") or 0
    return (long_score - long_activity) - (short_score - short_activity)


def _load_json(value):
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _as_utc(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
