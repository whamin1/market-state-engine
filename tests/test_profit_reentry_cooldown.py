import tempfile
import unittest
from pathlib import Path

from market_state_engine.config import MarketStateConfig
from market_state_engine.live_trader import LiveTrader
from market_state_engine.paper_trader import PaperTrader


class ProfitReentryCooldownTests(unittest.TestCase):
    def make(self, kind, state_path=None):
        kwargs = {'state_path': state_path, 'trade_log_path': None}
        if kind is LiveTrader:
            kwargs.update(client=object(), dry_run=True)
        return kind(MarketStateConfig(), **kwargs)

    def record(self, trader, side='LONG', score=15):
        pos = {'side': side, 'entry_score': 10, 'entry_price': 100}
        result = {side.lower()+'_score': score, ('short' if side=='LONG' else 'long')+'_score': 0}
        if isinstance(trader, LiveTrader):
            trader._record_profit_exit_if_needed(pos, 101, '2026-01-01T00:00:00+00:00', 1, result, 'profit protection')
        else:
            trader._record_profit_exit_if_needed(pos, {'exit_time': '2026-01-01T00:00:00+00:00', 'pnl_pct': 1, 'exit_reason': 'profit protection'}, result)

    def blocked(self, trader, side, score, minute=30, price=100):
        result = {side.lower()+'_score': score, ('short' if side=='LONG' else 'long')+'_score': 0}
        return trader._is_entry_blocked_by_profit_reentry(side, result, price, f'2026-01-01T00:{minute:02}:00+00:00')

    def test_exit_score_plus_two_and_no_price_bypass(self):
        for kind in (LiveTrader, PaperTrader):
            for side in ('LONG', 'SHORT'):
                with self.subTest(kind=kind, side=side):
                    trader = self.make(kind)
                    self.record(trader, side)
                    self.assertEqual(trader.last_profit_exit['exit_score'], 15)
                    for n in (10,13,15,16):
                        self.assertTrue(self.blocked(trader,side,n,price=200 if side=='LONG' else 50))
                    self.assertFalse(self.blocked(trader,side,17))
                    self.assertTrue(self.blocked(trader,side,18,minute=29))

    def test_reset_strictly_below_eight_preserves_cooldown(self):
        for kind in (LiveTrader, PaperTrader):
            for side in ('LONG', 'SHORT'):
                trader = self.make(kind)
                self.record(trader, side)
                self.assertTrue(self.blocked(trader,side,8,minute=10))
                self.assertFalse(trader.last_profit_exit['score_reset'])
                self.assertTrue(self.blocked(trader,side,7,minute=15))
                self.assertTrue(trader.last_profit_exit['score_reset'])
                self.assertTrue(self.blocked(trader,side,10,minute=29))
                self.assertFalse(self.blocked(trader,side,10,minute=30))

    def test_no_trade_tick_reset_survives_restart(self):
        for kind in (LiveTrader, PaperTrader):
            for side in ('LONG', 'SHORT'):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory)/'state.json'
                    trader = self.make(kind,path)
                    self.record(trader,side)
                    trader._save_state()
                    r = {'signal':'NO_TRADE','long_score':0,'short_score':0,'atr':10}
                    r[side.lower()+'_score'] = 7
                    trader.update(r,{'close':100},'2026-01-01T00:05:00+00:00','BTCUSDT')
                    resumed = self.make(kind,path)
                    self.assertTrue(resumed.last_profit_exit['score_reset'])
                    self.assertTrue(self.blocked(resumed,side,10,minute=29))
                    self.assertFalse(self.blocked(resumed,side,10,minute=30))

    def test_entry_resumes_only_after_reset_and_other_filters_still_apply(self):
        for kind in (LiveTrader, PaperTrader):
            trader = self.make(kind)
            self.record(trader)
            r = {'signal':'ENTER_LONG','long_score':13,'short_score':0,'atr':10,'reasons':[],
                 'score_components':{k:{'long_score':1} for k in ('body','volume','range')}}
            self.assertIsNone(trader.update(r,{'close':100},'2026-01-01T00:30:00+00:00','BTCUSDT'))
            trader.update({**r,'signal':'NO_TRADE','long_score':7},{'close':100},'2026-01-01T00:31:00+00:00','BTCUSDT')
            blocked = {**r,'long_score':10,'score_components':{'body':{'long_score':10}}}
            self.assertIsNone(trader.update(blocked,{'close':100},'2026-01-01T00:32:00+00:00','BTCUSDT'))
            event = trader.update({**r,'long_score':10},{'close':100},'2026-01-01T00:33:00+00:00','BTCUSDT')
            self.assertIsNotNone(event)

    def test_legacy_unknown_exit_score_requires_reset(self):
        for kind in (LiveTrader, PaperTrader):
            trader = self.make(kind)
            trader.last_profit_exit = {'side':'LONG','entry_score':10,'entry_price':100,
                                       'exit_time':'2026-01-01T00:00:00+00:00'}
            self.assertTrue(self.blocked(trader,'LONG',25))
            self.blocked(trader,'LONG',7)
            self.assertFalse(self.blocked(trader,'LONG',10))

    def test_reset_at_exit_and_new_profit_exit_rearms(self):
        for kind in (LiveTrader, PaperTrader):
            trader = self.make(kind)
            self.record(trader,score=7)
            self.assertTrue(trader.last_profit_exit['score_reset'])
            self.assertTrue(self.blocked(trader,'LONG',10,minute=29))
            self.assertFalse(self.blocked(trader,'LONG',10))
            self.record(trader,score=20)
            self.assertFalse(trader.last_profit_exit['score_reset'])
            self.assertTrue(self.blocked(trader,'LONG',21))
            self.assertFalse(self.blocked(trader,'LONG',22))

    def test_other_side_is_not_blocked_and_does_not_reset_saved_side(self):
        for kind in (LiveTrader, PaperTrader):
            trader = self.make(kind)
            self.record(trader)
            self.assertFalse(trader._is_entry_blocked_by_profit_reentry('SHORT',
                {'long_score':15,'short_score':14},100,'2026-01-01T00:01:00+00:00'))
            self.assertFalse(trader.last_profit_exit['score_reset'])

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
                    "LONG", {"long_score": 15, "short_score": 0}, 101.0, "2026-01-02T00:20:00+00:00"
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
