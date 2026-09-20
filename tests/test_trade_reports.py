import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, call

from market_state_engine.config import MarketStateConfig
from market_state_engine.live_trader import LiveTrader
from market_state_engine.trade_report import _duration
from examples.run_live_loop import format_trade_event_message


class TradeReportTests(unittest.TestCase):
    def result(self, side):
        return {'long_score': 11 if side == 'LONG' else 3, 'short_score': 3 if side == 'LONG' else 11,
                'activity_score': 4, 'atr': 10, 'signal': f'ENTER_{side}',
                'reasons': [f'trend_continuity same direction {side} +3', f'volume_score {side} +2',
                            f'body_score {side} +0', f'range_edge_penalty {side} -3']}

    def trader(self, dry=True):
        return LiveTrader(MarketStateConfig(), client=Mock(), dry_run=dry, enabled=not dry,
                          state_path=None, trade_log_path=None)

    def open(self, trader, side):
        if not trader.dry_run:
            trader.client.get_position_risk.return_value = [{'positionAmt': '0'}]
            trader.client.place_market_order.return_value = {'status': 'NEW'}
        return trader.update(self.result(side), {'close': 100}, '2026-09-20T01:42:00+00:00', 'BTCUSDT')

    def test_long_and_short_entry_and_original_order_arguments(self):
        for side in ('LONG', 'SHORT'):
            with self.subTest(side=side):
                trader = self.trader(False)
                event = self.open(trader, side)
                trader.client.place_market_order.assert_called_once_with('BTCUSDT', 'BUY' if side == 'LONG' else 'SELL', event['quantity'])
                self.assertEqual(trader.client.get_position_risk.call_args_list, [call('BTCUSDT'), call('BTCUSDT')])
                self.assertEqual(event['entry_long_score'], self.result(side)['long_score'])
                self.assertEqual(event['entry_short_score'], self.result(side)['short_score'])
                self.assertEqual(event['stop_price'], 85 if side == 'LONG' else 115)
                message = format_trade_event_message(event)
                self.assertIn(f'{side} 진입', message)
                self.assertIn('09-20 10:42 KST', message)
                self.assertIn('Spread +8.00' if side == 'LONG' else 'Spread -8.00', message)
                self.assertIn('추세: +3', message)
                self.assertNotIn('봉 몸통:', message)
                self.assertIn('체결가·체결시각 확정 보고가 아닙니다', message)

    def test_long_short_close_preserves_scores_peak_and_reduce_only_order(self):
        for side in ('LONG', 'SHORT'):
            with self.subTest(side=side):
                trader = self.trader(False)
                self.open(trader, side)
                trader.position_state['peak_profit_pct'] = 0.18
                trader.client.place_market_order.reset_mock()
                amount = trader.position_state['amount']
                event = trader._close_live_position('BTCUSDT', {'side': side, 'entry': 100, 'amount': amount},
                    99, 'stop loss', result={'long_score': 2, 'short_score': 9}, exit_time='2026-09-20T05:18:00+00:00')
                self.assertIsNone(trader.position_state)
                self.assertEqual(event['peak_profit_pct'], 0.18)
                self.assertEqual(event['entry_long_score'], self.result(side)['long_score'])
                trader.client.place_market_order.assert_called_once_with('BTCUSDT', 'SELL' if side == 'LONG' else 'BUY', event['quantity'], reduce_only=True)
                message = format_trade_event_message(event)
                self.assertIn(f'{side} 청산', message)
                self.assertIn('3h 36m', message)
                self.assertIn('Spread -7.00', message)
                self.assertIn('최고 수익률(관측): +0.18%', message)
                self.assertIn('수수료 차감·추정', message)

    def test_scores_survive_restart_and_legacy_does_not_fabricate_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            trader = self.trader()
            trader.state_path = path
            self.open(trader, 'LONG')
            restarted = LiveTrader(MarketStateConfig(), client=Mock(), state_path=path, trade_log_path=None)
            self.assertEqual(restarted.position_state['entry_long_score'], 11)
            self.assertEqual(restarted.position_state['entry_short_score'], 3)
            del restarted.position_state['entry_short_score']
            event = restarted._close_dry_run_position(99, 'stop loss', result=self.result('SHORT'))
            self.assertIsNone(event['entry_short_score'])
            self.assertIn('SHORT N/A', format_trade_event_message(event))

    def test_durations(self):
        start = '2026-09-20T00:00:00+00:00'
        for end, expected in (('2026-09-20T00:42:00+00:00', '42m'),
                              ('2026-09-20T03:36:00+00:00', '3h 36m'),
                              ('2026-09-21T02:00:00+00:00', '1d 2h')):
            self.assertEqual(_duration(start, end), expected)
        self.assertEqual(_duration(None, None), 'N/A')

    def test_reversal_report_includes_close_and_entry(self):
        trader = self.trader()
        self.open(trader, 'LONG')
        result = self.result('SHORT')
        result['short_score'] = 14
        event = trader.update(result, {'close': 99}, '2026-09-20T01:43:00+00:00', 'BTCUSDT')
        self.assertEqual(event['type'], 'LIVE_REVERSAL')
        self.assertEqual(event['close_event']['entry_long_score'], 11)
        self.assertEqual(event['entry_event']['entry_short_score'], 14)
        report = format_trade_event_message(event)
        self.assertIn('LONG 청산', report)
        self.assertIn('SHORT 진입', report)

    def test_exchange_flat_observation_keeps_closed_position_context(self):
        trader = self.trader(False)
        self.open(trader, 'SHORT')
        trader.position_state['peak_profit_pct'] = 0.4
        event = trader.update(self.result('LONG'), {'close': 101}, '2026-09-20T02:00:00+00:00', 'BTCUSDT')
        self.assertEqual(event['type'], 'LIVE_POSITION_CLOSED')
        self.assertEqual(event['entry_short_score'], 11)
        self.assertEqual(event['peak_profit_pct'], 0.4)
        self.assertIn('SHORT 청산', format_trade_event_message(event))


if __name__ == '__main__':
    unittest.main()
