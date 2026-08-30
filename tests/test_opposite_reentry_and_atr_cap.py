import tempfile
import unittest
from pathlib import Path

from market_state_engine.config import MarketStateConfig
from market_state_engine.engine import MarketStateEngine
from market_state_engine.live_trader import LiveTrader
from market_state_engine.paper_trader import PaperTrader


class OppositeReentryAndAtrCapTests(unittest.TestCase):
    def _result(self, long_score=0, short_score=0, signal="NO_TRADE"):
        return {
            "long_score": long_score,
            "short_score": short_score,
            "signal": signal,
            "atr": 10.0,
            "reasons": [],
        }

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

    def test_paper_reversal_waits_for_fourteen_then_closes_and_opens_in_one_update(self):
        trader = PaperTrader(MarketStateConfig(), state_path=None, trade_log_path=None)
        opened = trader.update(
            self._result(long_score=10, short_score=0, signal="ENTER_LONG"),
            {"close": 100.0},
            "2026-01-01T00:00:00+00:00",
            "BTCUSDT",
        )
        self.assertEqual(opened["type"], "OPEN")

        no_reversal = trader.update(
            self._result(long_score=4, short_score=13, signal="ENTER_SHORT"),
            {"close": 99.0},
            "2026-01-01T00:01:00+00:00",
            "BTCUSDT",
        )
        self.assertIsNone(no_reversal)
        self.assertEqual(trader.position["side"], "LONG")

        reversal = trader.update(
            self._result(long_score=4, short_score=14, signal="ENTER_SHORT"),
            {"close": 98.0},
            "2026-01-01T00:02:00+00:00",
            "BTCUSDT",
        )
        self.assertEqual(reversal["type"], "REVERSAL")
        self.assertEqual(reversal["close_event"]["exit_reason"], "confirmed opposite reversal")
        self.assertEqual(reversal["entry_event"]["side"], "SHORT")
        self.assertEqual(trader.position["side"], "SHORT")

    def test_live_dry_run_reversal_closes_and_opens_in_one_update(self):
        trader = LiveTrader(MarketStateConfig(), client=object(), dry_run=True, state_path=None, trade_log_path=None)
        trader.update(
            self._result(long_score=10, short_score=0, signal="ENTER_LONG"),
            {"close": 100.0},
            "2026-01-01T00:00:00+00:00",
            "BTCUSDT",
        )
        reversal = trader.update(
            self._result(long_score=4, short_score=14, signal="ENTER_SHORT"),
            {"close": 98.0},
            "2026-01-01T00:01:00+00:00",
            "BTCUSDT",
        )

        self.assertEqual(reversal["type"], "LIVE_REVERSAL")
        self.assertEqual(reversal["close_event"]["reason"], "confirmed opposite reversal")
        self.assertEqual(reversal["entry_event"]["position_side"], "SHORT")
        self.assertEqual(trader.position_state["side"], "SHORT")


if __name__ == "__main__":
    unittest.main()
