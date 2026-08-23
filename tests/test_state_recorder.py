import importlib.util
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_state_engine.engine import MarketStateEngine


MODULE_PATH = Path(__file__).parents[1] / "market_state_engine" / "state_recorder.py"
SPEC = importlib.util.spec_from_file_location("state_recorder", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
MarketStateRecorder = MODULE.MarketStateRecorder


class MarketStateRecorderTests(unittest.TestCase):
    def test_saves_one_complete_snapshot_and_ignores_duplicate_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "btc_market_state.db"
            recorder = MarketStateRecorder(db_path, strategy_version="test_v1")
            snapshot = self._snapshot()

            self.assertTrue(recorder.save(snapshot))
            self.assertFalse(recorder.save(snapshot))

            connection = sqlite3.connect(db_path)
            try:
                row = connection.execute(
                    """
                    SELECT symbol, price, long_score, short_score, score_diff,
                           body_long_score, range_position_long_score,
                           short_liq_1h, position_side, future_1h_long_score,
                           strategy_version
                    FROM market_state
                    """
                ).fetchone()
                columns = {item[1] for item in connection.execute("PRAGMA table_info(market_state)")}
            finally:
                connection.close()

            self.assertEqual(row, ("BTCUSDT", 65000.0, 13, 4, 9, 3, 2, 120000.0, "LONG", None, "test_v1"))
            self.assertIn("future_4h_entry_condition", columns)
            self.assertIn("strategy_config_json", columns)

    def test_invalid_snapshot_is_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = MarketStateRecorder(Path(directory) / "btc_market_state.db")
            with self.assertLogs("state_recorder", level="ERROR"):
                self.assertFalse(recorder.save({}))

    def test_real_engine_result_exposes_all_recorder_inputs(self):
        start = datetime(2025, 8, 1, tzinfo=timezone.utc)
        completed_candles = []
        for index in range(365):
            price = 50000.0 + index * 10
            completed_candles.append(
                {
                    "timestamp": (start + timedelta(days=index)).isoformat(),
                    "open": price,
                    "high": price + 100.0,
                    "low": price - 100.0,
                    "close": price + 20.0,
                    "volume": 1000.0 + index,
                }
            )

        current_time = (start + timedelta(days=365, hours=12)).isoformat()
        current_candle = {
            "timestamp": (start + timedelta(days=365)).isoformat(),
            "open": 53650.0,
            "high": 53900.0,
            "low": 53500.0,
            "close": 53800.0,
            "volume": 2200.0,
        }
        result = MarketStateEngine().update(
            completed_candles,
            current_candle=current_candle,
            current_time=current_time,
            liquidation_data=None,
        )

        self.assertEqual(result["indicators"]["current_candle"]["open"], 53650.0)
        self.assertIn("price_position", result["score_components"])
        self.assertIn("range", result["score_components"])
        self.assertIn("liquidation", result["score_components"])
        self.assertIn("expected_final_volume", result["indicators"]["volume"])

    @staticmethod
    def _snapshot():
        return {
            "time": "2026-08-23T09:00:00+00:00",
            "symbol": "BTCUSDT",
            "price": 65000.0,
            "mode": "LIVE",
            "order_mode": "REAL_ORDER",
            "result": {
                "long_score": 13,
                "short_score": 4,
                "activity_score": 2,
                "atr": 1800.5,
                "state": "LONG",
                "signal": "ENTER_LONG",
                "range": {
                    "high": 67000.0,
                    "low": 60000.0,
                    "width_pct": 11.67,
                    "position_bin": 6,
                    "breakout_level": None,
                    "breakdown_level": None,
                    "breakout_age_hours": None,
                    "breakdown_age_hours": None,
                },
                "failure_counts": {"upper_breakout_failure": 1, "lower_breakdown_failure": 0},
                "reasons": ["body_score LONG +3"],
                "score_components": {
                    "price_position": {"long_score": 2, "short_score": 0},
                    "body": {"long_score": 3, "short_score": 0},
                    "volume": {"long_score": 2, "short_score": 0},
                    "trend_continuity": {"long_score": 1, "short_score": 0},
                    "range": {
                        "long_score": 2,
                        "short_score": 0,
                        "position_long_score": 2,
                        "position_short_score": 0,
                        "breakout_long_score": 0,
                        "breakdown_short_score": 0,
                        "edge_penalty_long_score": 0,
                        "edge_penalty_short_score": 0,
                        "block_trade": False,
                    },
                    "liquidation": {
                        "long_score": 2,
                        "short_score": 0,
                        "activity_score": 4,
                        "long_activity_bonus": 0,
                        "short_activity_bonus": 0,
                    },
                    "atr": {"activity_score": 2},
                    "activity_direction": {"long_score": 1, "short_score": 0},
                },
                "indicators": {
                    "current_candle": {
                        "timestamp": "2026-08-23T00:00:00+00:00",
                        "open": 64000.0,
                        "high": 65200.0,
                        "low": 63800.0,
                        "close": 65000.0,
                        "volume": 4500.0,
                    },
                    "price_position": {
                        "period_levels": {
                            "1y": {"high": 110000.0, "low": 50000.0},
                            "6m": {"high": 90000.0, "low": 55000.0},
                            "1m": {"high": 68000.0, "low": 60000.0},
                        }
                    },
                    "body": {"body": 1000.0, "direction": "UP", "reference_count": 365},
                    "volume": {
                        "day_progress": 0.5,
                        "expected_final_volume": 9000.0,
                        "reference_count": 365,
                    },
                    "trend_continuity": {"previous_direction": "UP", "up_count_7": 6, "down_count_7": 1},
                    "atr": {"current_true_range": 1400.0, "reference_count": 351},
                    "liquidation": {
                        "short_liq_1h": 120000.0,
                        "long_liq_1h": 20000.0,
                        "total_liq_1h": 140000.0,
                        "net_liq_1h": 100000.0,
                        "imbalance_ratio": 0.714,
                        "reference_hours": 168,
                    },
                },
            },
            "position": {
                "status": "OPEN",
                "side": "LONG",
                "amount": 0.01,
                "entry": 64000.0,
                "stop": 62000.0,
                "unrealized_pnl_usdt": 10.0,
                "unrealized_pnl_pct": 1.56,
                "add_count": 0,
                "trailing_active": False,
            },
            "account": {"equity": 460.0},
            "trade_event": None,
            "strategy_config": {"entry_long_score": 10},
        }


if __name__ == "__main__":
    unittest.main()
