from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from market_state_engine.config import MarketStateConfig
from market_state_engine.engine import MarketStateEngine
from market_state_engine.entry_filter import entry_evidence
from market_state_engine.live_trader import LiveTrader
from market_state_engine.paper_trader import PaperTrader
from examples.run_live_loop import build_decision_snapshot


def result(side='LONG', count=3, score=14):
    other = 'SHORT' if side == 'LONG' else 'LONG'
    return {side.lower()+'_score':score, other.lower()+'_score':0,
            'signal':'ENTER_'+side, 'atr':10, 'reasons':[],
            'score_components':{name:{side.lower()+'_score':1}
                                for name in ('body','volume','liquidation')[:count]}}


class EntryFilterTests(unittest.TestCase):
    def traders(self):
        return [PaperTrader(MarketStateConfig(), state_path=None, trade_log_path=None),
                LiveTrader(MarketStateConfig(), client=Mock(), dry_run=True, state_path=None, trade_log_path=None)]

    def update(self, trader, r, price=100, minute=0):
        return trader.update(r, {'close':price}, f'2026-09-27T00:{minute:02}:00+00:00', 'BTCUSDT')

    def position(self, trader):
        return trader.position if isinstance(trader, PaperTrader) else trader.position_state

    def test_bonus_only_does_not_qualify(self):
        r=result(count=2)
        r['score_components'].update(activity_direction={'long_score':3}, atr={'activity_score':6},
                                    liquidation={'long_score':0,'long_activity_bonus':1})
        self.assertEqual(entry_evidence(r,'LONG',3)['count'],2)
        self.assertFalse(entry_evidence(r,'LONG',3)['allowed'])

    def test_range_net_score_is_one_component(self):
        r=result(count=2)
        r['score_components']['range']={'long_score':0, 'position_long_score':3,
                                       'breakout_long_score':3, 'edge_penalty_long_score':-6}
        self.assertFalse(entry_evidence(r,'LONG',3)['allowed'])
        r['score_components']['range']['long_score']=1
        self.assertEqual(entry_evidence(r,'LONG',3)['count'],3)

    def test_missing_invalid_and_other_side_do_not_count(self):
        self.assertFalse(entry_evidence({},'LONG',3)['allowed'])
        self.assertEqual(entry_evidence(result('SHORT'),'LONG',3)['count'],0)
        r=result()
        for value in (None, float('nan'), float('inf'), -1, 0, True, '2'):
            r['score_components']['body']['long_score']=value
            self.assertFalse(entry_evidence(r,'LONG',3)['allowed'])

    def test_both_sides_require_three_for_initial_entry(self):
        for side in ('LONG','SHORT'):
            for trader in self.traders():
                with self.subTest(side=side,trader=type(trader).__name__):
                    self.assertIsNone(self.update(trader,result(side,2)))
                    self.assertIsNone(self.position(trader))
                    self.assertIsNotNone(self.update(trader,result(side,3)))
                    self.assertEqual(self.position(trader)['side'],side)

    def test_both_reversal_directions_wait_without_closing(self):
        for side in ('LONG','SHORT'):
            other='SHORT' if side=='LONG' else 'LONG'
            for trader in self.traders():
                self.update(trader,result(side))
                self.assertIsNone(self.update(trader,result(other,2),minute=1))
                self.assertEqual(self.position(trader)['side'],side)
                event=self.update(trader,result(other,3),minute=2)
                self.assertIn(event['type'],('REVERSAL','LIVE_REVERSAL'))
                self.assertEqual(self.position(trader)['side'],other)

    def test_real_order_path_blocks_before_order(self):
        trader=LiveTrader(MarketStateConfig(), client=Mock(), dry_run=False, enabled=True,
                          state_path=None, trade_log_path=None)
        self.assertIsNone(trader._open_entry('BTCUSDT',100,'2026-09-27T00:00:00+00:00',result(count=2),False))
        trader.client.place_market_order.assert_not_called()
        with patch.object(trader,'_close_live_position') as close:
            self.assertIsNone(trader._maybe_close_on_opposite_signal('BTCUSDT',{'side':'LONG'},100,result('SHORT',2)))
            close.assert_not_called()

    def test_pending_reversal_does_not_bypass_filter(self):
        trader=self.traders()[1]
        trader._record_opposite_exit('LONG','2026-09-27T00:00:00+00:00')
        self.assertIsNone(self.update(trader,result('SHORT',2),minute=1))
        self.assertIsNone(trader.position_state)

    def test_stop_loss_works_with_missing_components(self):
        for side in ('LONG','SHORT'):
            for trader in self.traders():
                self.update(trader,result(side))
                r=result(side)
                r.pop('score_components')
                event=self.update(trader,r,price=84 if side=='LONG' else 116,minute=1)
                self.assertIn(event['type'],('CLOSE','LIVE_CLOSE'))
                self.assertIsNone(self.position(trader))

    def test_profit_protection_works_with_missing_components(self):
        for trader in self.traders():
            self.update(trader,result())
            self.position(trader)['peak_profit_pct']=1.0
            r=result(count=0)
            event=self.update(trader,r,price=100.6,minute=1)
            self.assertIn(event['type'],('CLOSE','LIVE_CLOSE'))
            self.assertIsNone(self.position(trader))

    def test_engine_scores_signals_and_atr_raw_unchanged(self):
        history=[{'open':100,'high':101,'low':99,'close':100,'volume':1} for _ in range(380)]
        current={'open':100,'high':200,'low':1,'close':150,'volume':1}
        config=MarketStateConfig()
        a=MarketStateEngine(config).update(history,current_candle=current,day_progress=0.5)
        b=MarketStateEngine(replace(config,entry_min_positive_components=0)).update(history,current_candle=current,day_progress=0.5)
        for key in ('long_score','short_score','signal','score_components'):
            self.assertEqual(a[key],b[key])
        self.assertEqual(a['indicators']['atr']['raw_score'],6)
        self.assertEqual(a['activity_score'],3)
        self.assertIn('entry_eligibility',a['indicators'])

    def test_block_reason_in_report(self):
        r=result(count=2)
        r['entry_eligibility']={'LONG':entry_evidence(r,'LONG',3)}
        report=build_decision_snapshot(r,None,{'status':'FLAT'})
        self.assertIn('2/3',report['reason'])
