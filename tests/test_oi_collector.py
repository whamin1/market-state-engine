from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from market_state_engine.oi_collector import OICollector


class OICollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'market.db'
        self.now = datetime(2026, 9, 26, 1, tzinfo=timezone.utc)

    def collector(self, at, value=100):
        return OICollector(self.path, fetch=lambda: {
            'symbol': 'BTCUSDT', 'openInterest': str(value),
            'time': int(at.timestamp() * 1000)})

    def rows(self):
        with closing(sqlite3.connect(self.path)) as c:
            return c.execute('SELECT open_interest,status FROM oi_snapshots ORDER BY timestamp').fetchall()

    def test_dedup_and_restart_history(self):
        self.collector(self.now, 100).sample_once(self.now)
        self.collector(self.now, 999).sample_once(self.now)
        self.assertEqual(self.rows(), [(100, 'ok')])
        later = self.now + timedelta(hours=1)
        result = self.collector(later, 110).sample_once(later)
        self.assertAlmostEqual(result['changes']['60']['pct'], 10)
        self.assertEqual(len(self.rows()), 2)

    def test_failed_fetch_and_recovery(self):
        def fail():
            raise TimeoutError('test timeout')
        with self.assertLogs('market_state_engine.oi_collector', level='WARNING'):
            OICollector(self.path, fail).sample_once(self.now)
        self.assertEqual(self.rows(), [(None, 'error')])
        self.collector(self.now).sample_once(self.now)
        self.assertEqual(self.rows(), [(100, 'ok')])

    def test_invalid_observations(self):
        with self.assertLogs('market_state_engine.oi_collector', level='WARNING'):
            self.collector(self.now - timedelta(minutes=3)).sample_once(self.now)
            self.collector(self.now, float('nan')).sample_once(self.now)
        self.assertEqual(self.rows(), [(None, 'error')])

    def test_storage_failure(self):
        collector = self.collector(self.now)
        collector.db_path = Path(self.temp.name)
        with self.assertLogs('market_state_engine.oi_collector', level='WARNING'):
            result = collector.sample_once(self.now)
        self.assertEqual(result['status'], 'storage_error')

    def test_report_freshness(self):
        collector = self.collector(self.now)
        collector.sample_once(self.now)
        self.assertEqual(collector.report(self.now)['status'], 'observed')
        self.assertEqual(collector.report(self.now + timedelta(minutes=3))['status'], 'stale')

    def test_background_fetch(self):
        entered, release = threading.Event(), threading.Event()
        def fetch():
            entered.set()
            release.wait(3)
            return {'symbol': 'BTCUSDT', 'openInterest': '100',
                    'time': int(datetime.now(timezone.utc).timestamp() * 1000)}
        collector = OICollector(self.path, fetch)
        try:
            collector.start()
            self.assertTrue(entered.wait(2))
            self.assertEqual(collector.report()['status'], 'waiting')
        finally:
            release.set()
            collector.stop()
        self.assertFalse(collector.thread.is_alive())
