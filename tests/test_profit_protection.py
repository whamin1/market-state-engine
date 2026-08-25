import unittest

from market_state_engine.config import MarketStateConfig
from market_state_engine.live_trader import LiveTrader
from market_state_engine.paper_trader import PaperTrader


class ProfitProtectionTests(unittest.TestCase):
    def setUp(self):
        self.config = MarketStateConfig()

    def _live_reason(self, peak_profit_pct, current_profit_pct):
        trader = LiveTrader(self.config, client=object(), state_path=None, trade_log_path=None)
        trader.position_state = {
            "side": "LONG",
            "entry_price": 100.0,
            "peak_profit_pct": peak_profit_pct,
        }
        return trader._get_small_profit_protection_reason("LONG", 100.0 * (1 + current_profit_pct / 100))

    def _paper_position(self, peak_profit_pct):
        trader = PaperTrader(self.config, state_path=None, trade_log_path=None)
        trader.position = {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "entry_time": "2026-01-01T00:00:00+00:00",
            "entry_price": 100.0,
            "peak_profit_pct": peak_profit_pct,
            "remaining_size": 1.0,
            "entry_reasons": [],
            "entry_score": 10,
            "last_add_score": 10,
            "entry_candle_key": "2026-01-01T00:00:00+00:00",
        }
        return trader

    def test_live_profit_protection_uses_the_correct_band(self):
        self.assertIsNone(self._live_reason(1.0, 0.71))
        self.assertIn("70% of peak", self._live_reason(1.0, 0.69))

        self.assertIsNone(self._live_reason(1.5, 1.21))
        self.assertIn("80% of peak", self._live_reason(1.5, 1.19))

        self.assertIsNone(self._live_reason(3.0, 2.0))

    def test_paper_profit_protection_uses_the_correct_band(self):
        low_band = self._paper_position(1.0)
        low_event = low_band._check_small_profit_protection(
            100.69,
            "2026-01-01T00:01:00+00:00",
            "BTCUSDT",
            {"reasons": [], "long_score": 10, "short_score": 0},
        )
        self.assertIn("70% of peak", low_event["exit_reason"])

        mid_band = self._paper_position(1.5)
        mid_event = mid_band._check_small_profit_protection(
            101.19,
            "2026-01-01T00:01:00+00:00",
            "BTCUSDT",
            {"reasons": [], "long_score": 10, "short_score": 0},
        )
        self.assertIn("80% of peak", mid_event["exit_reason"])


if __name__ == "__main__":
    unittest.main()
