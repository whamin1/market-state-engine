from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from market_state_engine.oi_report import summarize_oi, format_hourly_score_oi
from examples.run_live_loop import maybe_send_hourly_score_oi, maybe_send_score_alerts


class OIReportTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 26, 10, tzinfo=timezone.utc)

    def row(self, minutes, oi, symbol='BTCUSDT'):
        return {'event_time_kst': (self.now-timedelta(minutes=minutes)).isoformat(), 'oi': str(oi), 'symbol': symbol}

    def test_changes_are_asof_and_not_future_values(self):
        rows = [self.row(60, 100), self.row(15, 110), self.row(5, 120), self.row(0, 132),
                self.row(-1, 999), self.row(0, 999, 'ETHUSDT')]
        result = summarize_oi(rows, 'BTCUSDT', self.now)
        self.assertEqual(result['oi'], 132)
        self.assertAlmostEqual(result['changes']['5']['pct'], 10)
        self.assertAlmostEqual(result['changes']['15']['pct'], 20)
        self.assertAlmostEqual(result['changes']['60']['pct'], 32)
        self.assertFalse(result['fetch_success_verified'])

    def test_old_missing_invalid_zero_and_nan_are_not_zero_change(self):
        for rows in ([], [self.row(3, 100), self.row(15, 90)], [self.row(0, 0)],
                     [self.row(0, 'nan')], [self.row(0, 'bad')], [self.row(0, -100)]):
            with self.subTest(rows=rows):
                result = summarize_oi(rows, 'BTCUSDT', self.now)
                self.assertTrue(all(x['pct'] is None for x in result['changes'].values()))

    def test_missing_historical_point_and_kst_naive_times(self):
        rows = [self.row(0, 100), self.row(8, 90)]
        self.assertIsNone(summarize_oi(rows, 'BTCUSDT', self.now)['changes']['5']['pct'])
        rows = [{'symbol': 'BTCUSDT', 'event_time_kst': '2026-09-26 19:00:00', 'oi': '100'},
                {'symbol': 'BTCUSDT', 'event_time_kst': '2026-09-26 18:55:00', 'oi': '125'}]
        result = summarize_oi(rows, 'BTCUSDT', self.now)
        self.assertEqual(result['changes']['5']['pct'], -20)

    def test_message_caveat_and_position_only_when_open(self):
        snapshot = {'symbol': 'BTCUSDT', 'time': self.now.isoformat(), 'price': 80000,
                    'result': {'long_score': 10, 'short_score': 3, 'activity_score': 2}}
        message = format_hourly_score_oi(snapshot, self.now.isoformat())
        self.assertIn('판단 불가', message)
        self.assertIn('조회 성공 여부는 확인되지 않아', message)
        self.assertNotIn('보유:', message)
        snapshot['position'] = {'status': 'OPEN', 'side': 'LONG', 'unrealized_pnl_pct': 0.3}
        self.assertIn('수익률: +0.30%', format_hourly_score_oi(snapshot, self.now.isoformat()))

    def args(self, directory):
        return SimpleNamespace(hourly_score_oi_days=7, symbol='BTCUSDT', no_score_alerts=False,
                               telegram_report=True, telegram_trades=False, score_alert_cooldown_hours=6,
                               score_alert_state_path=str(Path(directory)/'state.json'),
                               score_alert_log_path=str(Path(directory)/'log.jsonl'))

    def test_hourly_send_restart_expiry_and_log(self):
        with tempfile.TemporaryDirectory() as d, patch('examples.run_live_loop.send_status_report', return_value=True) as send:
            args = self.args(d)
            snapshot = {'symbol': 'BTCUSDT', 'result': {'long_score': 0}, 'oi_report': {'status': 'missing'}}
            state = {}
            self.assertTrue(maybe_send_hourly_score_oi(args, snapshot, state, self.now))
            restored = json.loads(Path(args.score_alert_state_path).read_text())
            self.assertTrue(maybe_send_hourly_score_oi(args, snapshot, restored, self.now+timedelta(minutes=59)))
            self.assertEqual(send.call_count, 1)
            self.assertTrue(maybe_send_hourly_score_oi(args, snapshot, restored, self.now+timedelta(hours=1)))
            self.assertEqual(send.call_count, 2)
            self.assertFalse(maybe_send_hourly_score_oi(args, snapshot, restored, self.now+timedelta(days=7)))
            self.assertFalse(maybe_send_hourly_score_oi(args, snapshot, restored, self.now+timedelta(days=8)))
            records = [json.loads(line) for line in Path(args.score_alert_log_path).read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]['type'], 'HOURLY_SCORE_OI')

    def test_failed_send_retries_without_advancing_last_sent(self):
        with tempfile.TemporaryDirectory() as d, patch('examples.run_live_loop.send_status_report', side_effect=[False,True]) as send:
            args = self.args(d); state = {}; snapshot = {'result': {}}
            maybe_send_hourly_score_oi(args,snapshot,state,self.now)
            self.assertIsNone(state['hourly_oi_trial']['last_sent_at'])
            maybe_send_hourly_score_oi(args,snapshot,state,self.now+timedelta(minutes=1))
            self.assertEqual(send.call_count,2)

    def test_active_replaces_alerts_and_expired_restores_existing_path(self):
        with tempfile.TemporaryDirectory() as d, patch('examples.run_live_loop.maybe_send_hourly_score_oi') as hourly, patch('examples.run_live_loop.build_score_alerts', return_value=[]) as normal:
            args=self.args(d)
            hourly.return_value=True
            maybe_send_score_alerts(args, {'result': {}}, {})
            normal.assert_not_called()
            hourly.return_value=False
            maybe_send_score_alerts(args, {'result': {}}, {})
            normal.assert_called_once()
            hourly.side_effect=OSError('report failed')
            maybe_send_score_alerts(args, {'result': {}}, {})


if __name__ == '__main__':
    unittest.main()
