from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from market_state_engine.prediction_summary import (
    summarize_persistence_performance, summarize_recent_actuals, format_prediction_digest,
)
from test_prediction_digest import forecast_at, record_at
import test_prediction_digest as digest_tests


class PersistenceSummaryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        self.record = record_at(self.now - timedelta(hours=5))
        self.record['actual_outcomes'] = {
            name: {'timestamp': (self.now - timedelta(hours=5) + timedelta(minutes=minutes)).isoformat(),
                   'confirmed_at': self.now.isoformat(), 'long_score': 11, 'short_score': 4}
            for name, minutes in (('15m', 15), ('1h', 60), ('4h', 240))}

    def metric(self, record=None, side='LONG', period='all_time'):
        groups = summarize_persistence_performance([record or self.record], self.now)
        return groups[0][period]['horizons']['4h'][side] if groups else {'count': 0}

    def test_paired_mae_improvement_and_own_side_bands(self):
        groups = summarize_persistence_performance([self.record], self.now)
        stats = groups[0]['all_time']
        long, short = stats['horizons']['4h']['LONG'], stats['horizons']['4h']['SHORT']
        self.assertEqual((long['forecast_mae'], long['persistence_mae'], long['count']), (2, 7, 1))
        self.assertAlmostEqual(long['improvement_pct'], 500 / 7)
        self.assertEqual((short['forecast_mae'], short['persistence_mae']), (3, 6))
        self.assertEqual(short['improvement_pct'], 50)
        self.assertEqual(stats['bands_4h']['LONG']['0_9']['count'], 1)
        self.assertEqual(stats['bands_4h']['LONG']['10_plus']['count'], 0)
        self.assertEqual(stats['bands_4h']['SHORT']['10_plus']['count'], 1)
        self.assertEqual(stats['bands_4h']['SHORT']['0_9']['count'], 0)

    def test_seven_day_boundary_uses_source_not_created(self):
        outside = deepcopy(self.record)
        outside['source_timestamp'] = (self.now - timedelta(days=7, seconds=1)).isoformat()
        outside['created_at'] = (self.now - timedelta(days=7) + timedelta(seconds=1)).isoformat()
        self.assertEqual(self.metric(outside, period='last_7_days')['count'], 0)
        self.assertEqual(self.metric(outside)['count'], 1)
        outside['source_timestamp'] = (self.now - timedelta(days=7)).isoformat()
        self.assertEqual(self.metric(outside, period='last_7_days')['count'], 1)

    def test_pending_missing_not_ready_and_bad_timestamps_excluded_from_both(self):
        for case in ('pending', 'missing', 'side_not_ready', 'horizon_not_ready', 'stale', 'negative_age',
                     'future_created', 'future_confirmed', 'early_actual', 'wrong_target', 'wrong_source'):
            with self.subTest(case=case):
                record = deepcopy(self.record)
                item = record['forecast']['horizons']['4h']['score_changes']['LONG']
                actual = record['actual_outcomes']['4h']
                if case == 'pending': actual['timestamp'] = (self.now + timedelta(hours=1)).isoformat()
                if case == 'missing': actual['long_score'] = None
                if case == 'side_not_ready': item['ready'] = False
                if case == 'horizon_not_ready': record['forecast']['horizons']['4h']['ready'] = False
                if case == 'stale': record['created_at'] = (self.now - timedelta(hours=5) + timedelta(seconds=121)).isoformat()
                if case == 'negative_age': record['created_at'] = (self.now - timedelta(hours=6)).isoformat()
                if case == 'future_created': record['created_at'] = (self.now + timedelta(hours=1)).isoformat()
                if case == 'future_confirmed': actual['confirmed_at'] = (self.now + timedelta(hours=1)).isoformat()
                if case == 'early_actual': actual['timestamp'] = record['source_timestamp']
                if case == 'wrong_target': actual['report_strategy_matches'] = False
                if case == 'wrong_source': record['report_source_matches'] = False
                metric = self.metric(record)
                self.assertEqual(metric['count'], 0)
                self.assertIsNone(metric.get('forecast_mae'))
                self.assertIsNone(metric.get('persistence_mae'))

    def test_versions_separated_and_legacy_excluded(self):
        other = deepcopy(self.record)
        other['forecast']['strategy_version'] = 'other'
        legacy = deepcopy(self.record)
        legacy['forecast']['forecast_version'] = 'legacy_core_gap_v1'
        groups = summarize_persistence_performance([self.record, other, legacy], self.now)
        self.assertEqual(len(groups), 2)
        self.assertEqual([g['all_time']['horizons']['4h']['LONG']['count'] for g in groups], [1, 1])

    def test_baseline_zero_and_missing_median(self):
        self.record['actual_outcomes']['4h']['long_score'] = 4
        metric = self.metric()
        self.assertEqual(metric['persistence_mae'], 0)
        self.assertIsNone(metric['improvement_pct'])
        self.record['forecast']['horizons']['4h']['score_changes']['LONG']['median_score'] = None
        self.assertEqual(self.metric()['count'], 0)
        self.assertEqual(self.metric(side='SHORT')['count'], 1)

    def test_exact_sample_pairing_negative_improvement_and_band_boundary(self):
        second = deepcopy(self.record)
        second['forecast']['long_score'] = 9
        second['forecast']['short_score'] = 10
        second['actual_outcomes']['4h']['long_score'] = 9
        second['forecast']['horizons']['4h']['score_changes']['LONG']['median_score'] = 29
        excluded = deepcopy(second)
        excluded['forecast']['horizons']['4h']['score_changes']['LONG']['ready'] = False
        stats = summarize_persistence_performance([self.record, second, excluded], self.now)[0]['all_time']
        metric = stats['horizons']['4h']['LONG']
        self.assertEqual((metric['count'], metric['forecast_mae'], metric['persistence_mae']), (2, 11, 3.5))
        self.assertLess(metric['improvement_pct'], 0)
        self.assertEqual(stats['bands_4h']['LONG']['0_9']['count'], 2)
        self.assertEqual(stats['bands_4h']['SHORT']['10_plus']['count'], 3)

    def test_recent_six_hours_unchanged_and_sections_one_to_four_unchanged(self):
        recent = summarize_recent_actuals([self.record], self.now)
        context = {'recent_actuals': recent}
        current = forecast_at(self.now)
        old = format_prediction_digest(current, [], {}, self.now, context=context)
        context['persistence_performance'] = summarize_persistence_performance([self.record], self.now)
        new = format_prediction_digest(current, [], {}, self.now, context=context)
        self.assertEqual(old.split('⑤')[0], new.split('⑤')[0])
        self.assertIn('4H: LONG 2.00 (1건) / SHORT 3.00 (1건)', new)
        self.assertIn('[최근 7일]', new)
        self.assertIn('[전체 누적]', new)
        self.assertIn('SHORT 10+: Forecast 3.00 / Persistence 6.00 / 개선 +50.0% (1건)', new)
        self.assertIn('평가 건수에는 서로 겹치는', new)
        self.assertLessEqual(len(new.encode('utf-16-le')) // 2, 4096)

    def test_three_recent_versions_plus_full_metrics_fit_telegram(self):
        records = []
        for index in range(3):
            record = deepcopy(self.record)
            record['forecast']['strategy_version'] = f'version_{index}'
            records.append(record)
        current = forecast_at(self.now, 'version_0')
        message = format_prediction_digest(current, [], {}, self.now, context={
            'recent_actuals': summarize_recent_actuals(records, self.now),
            'persistence_performance': summarize_persistence_performance(records, self.now)})
        self.assertLessEqual(len(message.encode('utf-16-le')) // 2, 4096)


class PersistenceStorageTests(unittest.TestCase):
    setUp = digest_tests.DigestStorageTests.setUp
    insert = digest_tests.DigestStorageTests.insert
    label = digest_tests.DigestStorageTests.label

    def test_read_only_all_history_and_no_database_changes(self):
        self.label('4h', 240)
        now = self.start + timedelta(days=9)
        self.forecaster.refresh_actual_outcomes(now=now)
        before = self.forecaster.get_schedule_record('BTCUSDT', 'first')
        groups = self.forecaster.persistence_performance('BTCUSDT', now)
        self.assertEqual(groups[0]['all_time']['horizons']['4h']['LONG']['count'], 1)
        self.assertEqual(groups[0]['last_7_days']['horizons']['4h']['LONG']['count'], 0)
        self.assertEqual(before, self.forecaster.get_schedule_record('BTCUSDT', 'first'))

    def test_database_target_strategy_mismatch_is_excluded(self):
        from contextlib import closing
        import sqlite3
        self.label('4h', 240)
        now = self.start + timedelta(hours=5)
        self.forecaster.refresh_actual_outcomes(now=now)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('UPDATE market_state SET strategy_version=? WHERE timestamp=?',
                      ('changed', (self.start + timedelta(hours=4)).isoformat()))
        groups = self.forecaster.persistence_performance('BTCUSDT', now)
        self.assertEqual(groups[0]['all_time']['horizons']['4h']['LONG']['count'], 0)


if __name__ == '__main__':
    unittest.main()
