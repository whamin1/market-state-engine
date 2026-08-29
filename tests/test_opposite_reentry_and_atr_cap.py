import tempfile
import unittest
from pathlib import Path

from market_state_engine.config import MarketStateConfig
from market_state_engine.engine import MarketStateEngine
from market_state_engine.live_trader import LiveTrader
from market_state_engine.paper_trader import PaperTrader


class OppositeReentryAndAtrCapTests(unittest.TestCase):
    def _result(self, long_score=0, short_score=0):
        return {"long_score": long_score, "short_score": short_score}

    def test_atr_direction_score_is_capped_at_three(self):
        engine = MarketStateEngine(MarketStateConfig(), state_path=None)
        history = [
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0}
            for _ in range(380)
        ]
        current_candle = {"open": 100.0, "high": 200.0, "low": 1.0, "close": 150.0, "volume": 1.0}

        result = engine.calc_atr_score(history, current_candle)
        direction = engine.calc_activity_direction_bonus(current_candle, result["activity_score"])

        self.assertEqual(result["indicators"]["raw_score"], 6)
        self.assertEqual(result["activity_score"], 3)
        self.assertEqual(direction["long_score"], 3)

    def test_live_reversal_needs_four_extra_points_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "live_state.json"
            config = MarketStateConfig()
            trader = LiveTrader(config, client=object(), state_path=state_path, trade_log_path=None)
            trader._record_opposite_exit("LONG", "2026-01-01T00:00:00+00:00")
            trader._save_state()

            restarted = LiveTrader(config, client=object(), state_path=state_path, trade_log_path=None)
            self.assertTrue(
                restarted._is_entry_blocked_by_opposite_reentry(
                    "SHORT", self._result(short_score=13), "2026-01-01T00:01:00+00:00"
                )
            )
            self.assertFalse(
                restarted._is_entry_blocked_by_opposite_reentry(
                    "SHORT", self._result(short_score=14), "2026-01-01T00:01:00+00:00"
                )
            )
            self.assertFalse(
                restarted._is_entry_blocked_by_opposite_reentry(
                    "LONG", self._result(long_score=10), "2026-01-01T00:01:00+00:00"
                )
            )

    def test_paper_reversal_needs_four_extra_points(self):
        trader = PaperTrader(MarketStateConfig(), state_path=None, trade_log_path=None)
        trader._record_opposite_exit("SHORT", "2026-01-01T00:00:00+00:00")

        self.assertTrue(
            trader._is_entry_blocked_by_opposite_reentry(
                "LONG", self._result(long_score=13), "2026-01-01T00:01:00+00:00"
            )
        )
        self.assertFalse(
            trader._is_entry_blocked_by_opposite_reentry(
                "LONG", self._result(long_score=14), "2026-01-01T00:01:00+00:00"
            )
        )


if __name__ == "__main__":
    unittest.main()
