from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from market_state_engine.config import MarketStateConfig, STRATEGY_VERSION
from market_state_engine.engine import MarketStateEngine
from market_state_engine.liquidation_speed import calculate_liquidation_speed


class ScoreWeightsV4Tests(unittest.TestCase):
    def setUp(self):
        self.engine = MarketStateEngine()
        self.reference = [{'open': 1000, 'close': 1000+i, 'volume': i}
                          for i in range(1, 101)]

    def test_body_decile_boundaries_both_directions(self):
        for size in (0, 1, 9, 10, 19, 20, 29, 30, 39, 40, 49, 50,
                     59, 60, 69, 70, 79, 80, 89, 90, 99, 100, 101):
            for sign, own, other in ((1, 'long_score', 'short_score'),
                                     (-1, 'short_score', 'long_score')):
                with self.subTest(size=size, sign=sign):
                    candle = {'open': 1000, 'close': 1000+sign*size}
                    result = self.engine.calc_body_score(self.reference, candle)
                    self.assertEqual(result[own], min(9, size//10))
                    self.assertEqual(result[other], 0)
                    self.assertEqual(result['indicators']['scoring_method'], 'deciles_0_to_9')

    def test_body_doji_tied_zero_history_and_missing_data(self):
        result = self.engine.calc_body_score([{'open': 1, 'close': 1}], {'open': 1, 'close': 1})
        self.assertEqual((result['long_score'], result['short_score']), (0, 0))
        self.assertEqual(self.engine.calc_body_score([])['long_score'], 0)
        self.assertEqual(self.engine.calc_body_score([], {'open': 1, 'close': 2})['long_score'], 0)

    def test_completed_candle_excluded_from_reference(self):
        result = self.engine.calc_body_score(self.reference + [{'open': 1000, 'close': 1019}])
        self.assertEqual(result['long_score'], 1)
        self.assertEqual(result['indicators']['reference_count'], 100)

    def test_other_percentile_bands_are_unchanged(self):
        points = (29, 30, 40, 50, 60, 70, 80, 100)
        self.assertEqual([self.engine._percentile_to_score(n, list(range(1,101)))
                          for n in points], [0,1,2,3,4,5,6,6])
        candle = {'open': 1000, 'close': 1090, 'volume': 90}
        self.assertEqual(self.engine.calc_volume_score(self.reference, candle, day_progress=1)['long_score'], 6)
        self.assertEqual(self.engine.calc_body_score(self.reference, candle)['long_score'], 9)

    def test_liquidation_full_raw_scale_preserved_and_capped(self):
        now = datetime(2026,10,2,3,tzinfo=timezone.utc)
        for side, key in [('BUY','long_score'), ('SELL','short_score')]:
            for amount, raw in [(350,0),(950,1),(1250,2),(1550,3),(1850,4),(2150,5),(2550,6)]:
                with self.subTest(side=side, raw=raw):
                    def event(ago, value):
                        return {'symbol':'BTCUSDT','side':side,'usd_size':value,
                                'timestamp':(now-timedelta(minutes=ago)).isoformat()}
                    data = {'symbol':'BTCUSDT', 'raw_events':
                            [event(1500,1)] + [event(11+5*i,(i+1)*100) for i in range(30)] + [event(2,amount)]}
                    result = calculate_liquidation_speed(data, now, MarketStateConfig())
                    self.assertEqual(result['indicators']['gate'], 'passed')
                    self.assertEqual(result['indicators']['raw_score'], raw)
                    self.assertEqual(result[key], min(raw,3))
                    self.assertEqual(result['long_activity_bonus']+result['short_activity_bonus'], 0)
                    self.assertFalse(any('liquidation_activity_bonus' in r for r in result['reasons']))

    def test_legacy_liquidation_cap_and_bonus(self):
        now = datetime(2026,10,2,3,tzinfo=timezone.utc)
        engine = MarketStateEngine(replace(MarketStateConfig(), liquidation_scoring_mode='rolling_hour_v1'))
        for side, key in [('BUY','long_score'), ('SELL','short_score')]:
            result = engine.calc_liquidation_score({'symbol':'BTCUSDT',
                'raw_events':[{'symbol':'BTCUSDT','side':side,'usd_size':10000,
                               'timestamp':(now-timedelta(minutes=2)).isoformat()}],
                'hourly_history':[{'symbol':'BTCUSDT','timestamp':(now-timedelta(hours=2)).isoformat(),
                                   'short_liq_usd':100,'long_liq_usd':0}]}, now)
            self.assertEqual(result[key],3)
            self.assertEqual(result['indicators']['raw_score'],6)
            self.assertEqual(result['long_activity_bonus']+result['short_activity_bonus'],0)

    def test_strategy_version_and_entry_thresholds(self):
        self.assertEqual(STRATEGY_VERSION,'market_state_engine_v5_exit_score_reentry')
        c = MarketStateConfig()
        self.assertEqual((c.entry_long_score,c.entry_short_score,c.entry_score_gap), (10,10,5))
        self.assertEqual(c.entry_min_positive_components,3)
        self.assertEqual(c.opposite_reentry_extra_score,4)


if __name__ == '__main__':
    unittest.main()
