import json
import logging
import sqlite3
from pathlib import Path


LOGGER = logging.getLogger(__name__)


class MarketStateRecorder:
    """Persist already-calculated live market snapshots without affecting trading."""

    SCHEMA_VERSION = 2
    COLUMNS = (
        "timestamp", "symbol", "price", "strategy_version", "schema_version", "mode", "order_mode",
        "decision", "action", "long_score", "short_score", "score_diff",
        "price_position_long_score", "price_position_short_score", "body_long_score", "body_short_score",
        "volume_long_score", "volume_short_score", "trend_continuity_long_score",
        "trend_continuity_short_score", "range_long_score", "range_short_score",
        "range_position_long_score", "range_position_short_score", "range_breakout_long_score",
        "range_breakdown_short_score", "range_edge_penalty_long_score", "range_edge_penalty_short_score",
        "liquidation_long_score", "liquidation_short_score", "liquidation_activity_score",
        "liquidation_activity_long_bonus", "liquidation_activity_short_bonus", "atr_activity_score",
        "activity_direction_long_score", "activity_direction_short_score", "atr", "atr_current_true_range",
        "atr_reference_count", "candle_timestamp", "candle_open", "candle_high", "candle_low",
        "candle_close", "candle_volume", "candle_body", "candle_direction", "day_progress",
        "expected_final_volume", "body_reference_count", "volume_reference_count",
        "trend_previous_direction", "trend_up_count_7", "trend_down_count_7", "range_high", "range_low",
        "range_width_pct", "range_position_bin", "breakout_level", "breakdown_level", "breakout_age_hours",
        "breakdown_age_hours", "range_block_trade", "price_1y_high", "price_1y_low", "price_6m_high",
        "price_6m_low", "price_1m_high", "price_1m_low", "short_liq_1h", "long_liq_1h",
        "total_liq_1h", "net_liq_1h", "liquidation_imbalance_ratio", "liquidation_reference_hours",
        "upper_breakout_failure_count", "lower_breakdown_failure_count", "position_status", "position_side",
        "position_size", "entry_price", "stop_price", "unrealized_pnl", "unrealized_pnl_pct",
        "position_add_count", "trailing_active", "future_15m_long_score", "future_15m_short_score",
        "future_1h_long_score", "future_1h_short_score", "future_4h_long_score", "future_4h_short_score",
        "future_15m_timestamp", "future_1h_timestamp", "future_4h_timestamp",
        "future_15m_core_score_diff", "future_1h_core_score_diff", "future_4h_core_score_diff",
        "return_15m", "return_1h", "return_4h", "future_15m_entry_condition",
        "future_1h_entry_condition", "future_4h_entry_condition", "score_reasons_json",
        "score_components_json", "indicators_json", "position_json", "account_json", "trade_event_json",
        "strategy_config_json",
    )

    def __init__(self, db_path="work/data/btc_market_state.db", strategy_version="market_state_engine_v1"):
        self.db_path = Path(db_path)
        self.strategy_version = strategy_version
        self._initialized = False
        self._initialize_safely()

    def save(self, snapshot):
        """Save one snapshot. Storage errors are contained so trading can continue."""
        try:
            if not self._initialized:
                self._initialize_safely()
            if not self._initialized:
                return False

            row = self._build_row(snapshot)
            if not row["timestamp"] or not row["symbol"]:
                raise ValueError("market state snapshot requires timestamp and symbol")

            placeholders = ", ".join("?" for _ in self.COLUMNS)
            columns = ", ".join(self.COLUMNS)
            values = [row.get(column) for column in self.COLUMNS]
            connection = self._connect()
            try:
                cursor = connection.execute(
                    f"INSERT OR IGNORE INTO market_state ({columns}) VALUES ({placeholders})",
                    values,
                )
                connection.commit()
                return cursor.rowcount == 1
            finally:
                connection.close()
        except Exception:
            LOGGER.exception("market state recorder save failed; live loop will continue")
            return False

    def _initialize_safely(self):
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute(CREATE_MARKET_STATE_TABLE)
                ensure_market_state_extensions(connection)
                connection.commit()
            finally:
                connection.close()
            self._initialized = True
        except Exception:
            LOGGER.exception("market state recorder initialization failed; live loop will continue")
            self._initialized = False

    def _connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=3)
        connection.execute("PRAGMA busy_timeout=3000")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _build_row(self, snapshot):
        result = snapshot.get("result") or {}
        components = result.get("score_components") or {}
        indicators = result.get("indicators") or {}
        current_candle = indicators.get("current_candle") or {}
        price_indicators = indicators.get("price_position") or {}
        price_levels = price_indicators.get("period_levels") or {}
        body_indicators = indicators.get("body") or {}
        volume_indicators = indicators.get("volume") or {}
        trend_indicators = indicators.get("trend_continuity") or {}
        range_info = result.get("range") or indicators.get("range") or {}
        range_components = components.get("range") or {}
        liquidation_components = components.get("liquidation") or {}
        liquidation_indicators = indicators.get("liquidation") or {}
        atr_components = components.get("atr") or {}
        atr_indicators = indicators.get("atr") or {}
        activity_direction = components.get("activity_direction") or {}
        failure_counts = result.get("failure_counts") or {}
        position = snapshot.get("position") or {}
        trade_event = snapshot.get("trade_event") or {}

        long_score = result.get("long_score")
        short_score = result.get("short_score")
        return {
            "timestamp": snapshot.get("time"),
            "symbol": snapshot.get("symbol"),
            "price": snapshot.get("price", current_candle.get("close")),
            "strategy_version": snapshot.get("strategy_version") or self.strategy_version,
            "schema_version": self.SCHEMA_VERSION,
            "mode": snapshot.get("mode"),
            "order_mode": snapshot.get("order_mode"),
            "decision": result.get("signal"),
            "action": trade_event.get("type"),
            "long_score": long_score,
            "short_score": short_score,
            "score_diff": self._difference(long_score, short_score),
            "price_position_long_score": self._component_score(components, "price_position", "long_score"),
            "price_position_short_score": self._component_score(components, "price_position", "short_score"),
            "body_long_score": self._component_score(components, "body", "long_score"),
            "body_short_score": self._component_score(components, "body", "short_score"),
            "volume_long_score": self._component_score(components, "volume", "long_score"),
            "volume_short_score": self._component_score(components, "volume", "short_score"),
            "trend_continuity_long_score": self._component_score(components, "trend_continuity", "long_score"),
            "trend_continuity_short_score": self._component_score(components, "trend_continuity", "short_score"),
            "range_long_score": self._component_score(components, "range", "long_score", result.get("range")),
            "range_short_score": self._component_score(components, "range", "short_score", result.get("range")),
            "range_position_long_score": range_components.get("position_long_score"),
            "range_position_short_score": range_components.get("position_short_score"),
            "range_breakout_long_score": range_components.get("breakout_long_score"),
            "range_breakdown_short_score": range_components.get("breakdown_short_score"),
            "range_edge_penalty_long_score": range_components.get("edge_penalty_long_score"),
            "range_edge_penalty_short_score": range_components.get("edge_penalty_short_score"),
            "liquidation_long_score": liquidation_components.get("long_score"),
            "liquidation_short_score": liquidation_components.get("short_score"),
            "liquidation_activity_score": liquidation_components.get("activity_score"),
            "liquidation_activity_long_bonus": liquidation_components.get("long_activity_bonus"),
            "liquidation_activity_short_bonus": liquidation_components.get("short_activity_bonus"),
            "atr_activity_score": atr_components.get("activity_score"),
            "activity_direction_long_score": activity_direction.get("long_score"),
            "activity_direction_short_score": activity_direction.get("short_score"),
            "atr": result.get("atr"),
            "atr_current_true_range": atr_indicators.get("current_true_range"),
            "atr_reference_count": atr_indicators.get("reference_count"),
            "candle_timestamp": current_candle.get("timestamp"),
            "candle_open": current_candle.get("open"),
            "candle_high": current_candle.get("high"),
            "candle_low": current_candle.get("low"),
            "candle_close": current_candle.get("close"),
            "candle_volume": current_candle.get("volume"),
            "candle_body": body_indicators.get("body"),
            "candle_direction": body_indicators.get("direction"),
            "day_progress": volume_indicators.get("day_progress"),
            "expected_final_volume": volume_indicators.get("expected_final_volume"),
            "body_reference_count": body_indicators.get("reference_count"),
            "volume_reference_count": volume_indicators.get("reference_count"),
            "trend_previous_direction": trend_indicators.get("previous_direction"),
            "trend_up_count_7": trend_indicators.get("up_count_7"),
            "trend_down_count_7": trend_indicators.get("down_count_7"),
            "range_high": range_info.get("high"),
            "range_low": range_info.get("low"),
            "range_width_pct": range_info.get("width_pct"),
            "range_position_bin": range_info.get("position_bin"),
            "breakout_level": range_info.get("breakout_level"),
            "breakdown_level": range_info.get("breakdown_level"),
            "breakout_age_hours": range_info.get("breakout_age_hours"),
            "breakdown_age_hours": range_info.get("breakdown_age_hours"),
            "range_block_trade": self._sqlite_bool(
                range_components.get("block_trade", self._range_block_trade(result))
            ),
            "price_1y_high": self._period_value(price_levels, "1y", "high"),
            "price_1y_low": self._period_value(price_levels, "1y", "low"),
            "price_6m_high": self._period_value(price_levels, "6m", "high"),
            "price_6m_low": self._period_value(price_levels, "6m", "low"),
            "price_1m_high": self._period_value(price_levels, "1m", "high"),
            "price_1m_low": self._period_value(price_levels, "1m", "low"),
            "short_liq_1h": liquidation_indicators.get("short_liq_1h"),
            "long_liq_1h": liquidation_indicators.get("long_liq_1h"),
            "total_liq_1h": liquidation_indicators.get("total_liq_1h"),
            "net_liq_1h": liquidation_indicators.get("net_liq_1h"),
            "liquidation_imbalance_ratio": liquidation_indicators.get("imbalance_ratio"),
            "liquidation_reference_hours": liquidation_indicators.get("reference_hours"),
            "upper_breakout_failure_count": failure_counts.get("upper_breakout_failure"),
            "lower_breakdown_failure_count": failure_counts.get("lower_breakdown_failure"),
            "position_status": position.get("status"),
            "position_side": position.get("side"),
            "position_size": position.get("amount", position.get("remaining_size", position.get("total_size"))),
            "entry_price": position.get("entry"),
            "stop_price": position.get("stop"),
            "unrealized_pnl": position.get("unrealized_pnl_usdt"),
            "unrealized_pnl_pct": position.get("unrealized_pnl_pct"),
            "position_add_count": position.get("add_count"),
            "trailing_active": self._sqlite_bool(position.get("trailing_active")),
            "future_15m_long_score": None,
            "future_15m_short_score": None,
            "future_1h_long_score": None,
            "future_1h_short_score": None,
            "future_4h_long_score": None,
            "future_4h_short_score": None,
            "future_15m_timestamp": None,
            "future_1h_timestamp": None,
            "future_4h_timestamp": None,
            "future_15m_core_score_diff": None,
            "future_1h_core_score_diff": None,
            "future_4h_core_score_diff": None,
            "return_15m": None,
            "return_1h": None,
            "return_4h": None,
            "future_15m_entry_condition": None,
            "future_1h_entry_condition": None,
            "future_4h_entry_condition": None,
            "score_reasons_json": self._json(result.get("reasons", [])),
            "score_components_json": self._json(components),
            "indicators_json": self._json(indicators),
            "position_json": self._json(position),
            "account_json": self._json(snapshot.get("account") or {}),
            "trade_event_json": self._json(trade_event),
            "strategy_config_json": self._json(snapshot.get("strategy_config") or {}),
        }

    @staticmethod
    def _component_score(components, component_name, score_name, fallback=None):
        component = components.get(component_name) or {}
        if score_name in component:
            return component.get(score_name)
        if fallback is not None and isinstance(fallback, dict):
            return fallback.get(score_name)
        return None

    @staticmethod
    def _difference(long_score, short_score):
        if long_score is None or short_score is None:
            return None
        return long_score - short_score

    @staticmethod
    def _period_value(period_levels, period, key):
        return (period_levels.get(period) or {}).get(key)

    @staticmethod
    def _range_block_trade(result):
        return int(result.get("state") == "HOLD" and any(
            str(reason).startswith("range HOLD:") for reason in result.get("reasons", [])
        ))

    @staticmethod
    def _sqlite_bool(value):
        if value is None:
            return None
        return int(bool(value))

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


CREATE_MARKET_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS market_state (
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL,
    strategy_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    mode TEXT,
    order_mode TEXT,
    decision TEXT,
    action TEXT,
    long_score INTEGER,
    short_score INTEGER,
    score_diff INTEGER,
    price_position_long_score INTEGER,
    price_position_short_score INTEGER,
    body_long_score INTEGER,
    body_short_score INTEGER,
    volume_long_score INTEGER,
    volume_short_score INTEGER,
    trend_continuity_long_score INTEGER,
    trend_continuity_short_score INTEGER,
    range_long_score INTEGER,
    range_short_score INTEGER,
    range_position_long_score INTEGER,
    range_position_short_score INTEGER,
    range_breakout_long_score INTEGER,
    range_breakdown_short_score INTEGER,
    range_edge_penalty_long_score INTEGER,
    range_edge_penalty_short_score INTEGER,
    liquidation_long_score INTEGER,
    liquidation_short_score INTEGER,
    liquidation_activity_score INTEGER,
    liquidation_activity_long_bonus INTEGER,
    liquidation_activity_short_bonus INTEGER,
    atr_activity_score INTEGER,
    activity_direction_long_score INTEGER,
    activity_direction_short_score INTEGER,
    atr REAL,
    atr_current_true_range REAL,
    atr_reference_count INTEGER,
    candle_timestamp TEXT,
    candle_open REAL,
    candle_high REAL,
    candle_low REAL,
    candle_close REAL,
    candle_volume REAL,
    candle_body REAL,
    candle_direction TEXT,
    day_progress REAL,
    expected_final_volume REAL,
    body_reference_count INTEGER,
    volume_reference_count INTEGER,
    trend_previous_direction TEXT,
    trend_up_count_7 INTEGER,
    trend_down_count_7 INTEGER,
    range_high REAL,
    range_low REAL,
    range_width_pct REAL,
    range_position_bin INTEGER,
    breakout_level REAL,
    breakdown_level REAL,
    breakout_age_hours INTEGER,
    breakdown_age_hours INTEGER,
    range_block_trade INTEGER,
    price_1y_high REAL,
    price_1y_low REAL,
    price_6m_high REAL,
    price_6m_low REAL,
    price_1m_high REAL,
    price_1m_low REAL,
    short_liq_1h REAL,
    long_liq_1h REAL,
    total_liq_1h REAL,
    net_liq_1h REAL,
    liquidation_imbalance_ratio REAL,
    liquidation_reference_hours INTEGER,
    upper_breakout_failure_count INTEGER,
    lower_breakdown_failure_count INTEGER,
    position_status TEXT,
    position_side TEXT,
    position_size REAL,
    entry_price REAL,
    stop_price REAL,
    unrealized_pnl REAL,
    unrealized_pnl_pct REAL,
    position_add_count INTEGER,
    trailing_active INTEGER,
    future_15m_long_score INTEGER,
    future_15m_short_score INTEGER,
    future_1h_long_score INTEGER,
    future_1h_short_score INTEGER,
    future_4h_long_score INTEGER,
    future_4h_short_score INTEGER,
    future_15m_timestamp TEXT,
    future_1h_timestamp TEXT,
    future_4h_timestamp TEXT,
    future_15m_core_score_diff INTEGER,
    future_1h_core_score_diff INTEGER,
    future_4h_core_score_diff INTEGER,
    return_15m REAL,
    return_1h REAL,
    return_4h REAL,
    future_15m_entry_condition INTEGER,
    future_1h_entry_condition INTEGER,
    future_4h_entry_condition INTEGER,
    score_reasons_json TEXT,
    score_components_json TEXT,
    indicators_json TEXT,
    position_json TEXT,
    account_json TEXT,
    trade_event_json TEXT,
    strategy_config_json TEXT,
    PRIMARY KEY (timestamp, symbol)
)
"""


MARKET_STATE_EXTENSION_COLUMNS = {
    "future_15m_timestamp": "TEXT",
    "future_1h_timestamp": "TEXT",
    "future_4h_timestamp": "TEXT",
    "future_15m_core_score_diff": "INTEGER",
    "future_1h_core_score_diff": "INTEGER",
    "future_4h_core_score_diff": "INTEGER",
}


def ensure_market_state_extensions(connection):
    """Upgrade an existing recorder database without touching saved snapshots."""
    existing = {row[1] for row in connection.execute("PRAGMA table_info(market_state)")}
    for name, column_type in MARKET_STATE_EXTENSION_COLUMNS.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE market_state ADD COLUMN {name} {column_type}")
