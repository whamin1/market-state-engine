import tempfile
import unittest
from pathlib import Path

from market_state_engine.config import MarketStateConfig
from market_state_engine.live_trader import LiveTrader


class ProfitReentryCooldownTests(unittest.TestCase):
    def _result(self):
        return {"long_score": 13, "short_score": 13}

    def test_same_side_is_blocked_for_30_minutes_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "live_state.json"
            config = MarketStateConfig()
            trader = LiveTrader(config, client=object(), state_path=state_path, trade_log_path=None)
            trader._record_profit_exit_if_needed(
                {
                    "side": "LONG",
                    "entry_score": 10,
                    "entry_price": 100.0,
                    "entry_candle_key": "2026-01-01T00:00:00+00:00",
                },
                101.0,
                "2026-01-01T23:50:00+00:00",
                1.0,
                self._result(),
                "profit protection",
            )
            trader._save_state()

            restarted = LiveTrader(config, client=object(), state_path=state_path, trade_log_path=None)
            self.assertTrue(
                restarted._is_entry_blocked_by_profit_reentry(
                    "LONG", self._result(), 101.0, "2026-01-02T00:05:00+00:00"
                )
            )
            self.assertFalse(
                restarted._is_entry_blocked_by_profit_reentry(
                    "SHORT", self._result(), 99.0, "2026-01-02T00:05:00+00:00"
                )
            )
            self.assertFalse(
                restarted._is_entry_blocked_by_profit_reentry(
                    "LONG", self._result(), 101.0, "2026-01-02T00:20:00+00:00"
                )
            )

    def test_old_saved_state_uses_the_same_cooldown(self):
        config = MarketStateConfig()
        trader = LiveTrader(config, client=object(), state_path=None, trade_log_path=None)
        trader.last_profit_exit = {
            "side": "LONG",
            "exit_time": "2026-01-01T00:00:00+00:00",
            "entry_score": 10,
            "entry_price": 100.0,
        }

        self.assertTrue(
            trader._is_entry_blocked_by_profit_reentry(
                "LONG", self._result(), 101.0, "2026-01-01T00:29:00+00:00"
            )
        )


if __name__ == "__main__":
    unittest.main()
