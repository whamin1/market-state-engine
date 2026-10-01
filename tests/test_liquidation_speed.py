from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from market_state_engine.config import MarketStateConfig, STRATEGY_VERSION
from market_state_engine.engine import MarketStateEngine
from market_state_engine.liquidation_loader import load_liquidation_data
from market_state_engine.liquidation_speed import _score, _Series, calculate_liquidation_speed
from market_state_engine.state_recorder import MarketStateRecorder


class LiquidationSpeedTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 2, 3, tzinfo=timezone.utc)
        self.config = MarketStateConfig()

    def event(self, ago, amount, side="BUY"):
        return {"timestamp": (self.now-timedelta(minutes=ago)).isoformat(),
                "symbol": "BTCUSDT", "side": side, "usd_size": amount}

    def data(self, current=10000, previous=1000, side="BUY"):
        # Nonconstant history, identical across both directions, excluding live windows.
        history = [self.event(m, (m % 7 + 1)*100, s)
                   for m in range(11, 1501, 5) for s in ("BUY", "SELL")]
        return {"symbol": "BTCUSDT", "raw_events": history + [self.event(7,previous,side),self.event(2,current,side)]}

    def calc(self, data):
        return calculate_liquidation_speed(data, self.now, self.config)

    def test_increasing_long_and_short(self):
        for side,own,other in (("BUY","long_score","short_score"),("SELL","short_score","long_score")):
            result=self.calc(self.data(side=side))
            self.assertEqual(result[own],6)
            self.assertEqual(result[other],0)
            self.assertEqual(result['indicators']['gate'],'passed')
            self.assertEqual(result['long_activity_bonus']+result['short_activity_bonus'],1)

    def test_decreasing_does_not_keep_high_hour_score(self):
        result=self.calc(self.data(current=1000,previous=100000))
        self.assertEqual(result['long_score'],0)
        self.assertEqual(result['long_activity_bonus'],0)
        self.assertEqual(result['indicators']['gate'],'not_increasing')
        self.assertGreater(result['indicators']['short_liq_1h'],result['indicators']['short_liq_5m'])

    def test_small_increase_below_average_is_zero(self):
        result=self.calc(self.data(current=1,previous=0))
        self.assertEqual(result['indicators']['gate'],'not_above_mean')
        self.assertEqual(result['long_score'],0)

    def test_equal_speed_is_not_acceleration(self):
        result=self.calc(self.data(current=10000,previous=10000))
        self.assertEqual(result['indicators']['gate'],'not_increasing')

    def test_balanced_flow_is_not_directional(self):
        data=self.data()
        data['raw_events'].append(self.event(2,10000,'SELL'))
        result=self.calc(data)
        self.assertEqual(result['indicators']['gate'],'balanced')
        self.assertEqual(result['long_score']+result['short_score'],0)
        self.assertEqual(result['long_activity_bonus']+result['short_activity_bonus'],0)

    def test_missing_history_and_no_recent_events(self):
        result=self.calc({'raw_events':[self.event(2,10000)]})
        self.assertEqual(result['indicators']['gate'],'insufficient_history')
        self.assertEqual(result['long_score'],0)
        result=self.calc(self.data(current=0,previous=0))
        self.assertEqual(result['indicators']['gate'],'no_recent_events')
        self.assertEqual(result['activity_score'],0)

    def test_future_and_other_symbol_are_ignored(self):
        data=self.data()
        base=self.calc(data)
        data['raw_events'] += [self.event(-1,1e10),{**self.event(2,1e10),'symbol':'ETHUSDT'}]
        self.assertEqual(self.calc(data),base)

    def test_window_boundary_excludes_current_second(self):
        s=_Series([(0,10),(60,20),(120,30)])
        self.assertEqual(s.amount(60,120),20)

    def test_reference_does_not_overlap_live_window(self):
        data=self.data()
        result=self.calc(data)
        for w,by_side in result['indicators']['windows'].items():
            self.assertLessEqual(by_side['LONG']['reference_end'],self.now.timestamp()-int(w)*60)
            self.assertEqual(by_side['LONG']['reference_count'],1440//int(w))
        before=result['indicators']['windows']['5']['LONG']
        after=self.calc(self.data(current=1e8))['indicators']['windows']['5']['LONG']
        self.assertEqual(before['reference_mean_speed'],after['reference_mean_speed'])
        self.assertEqual(before['reference_std_speed'],after['reference_std_speed'])

    def test_percentile_bands_and_zero(self):
        self.assertEqual([_score(x,1) for x in (29,30,40,50,60,70,80,100)],[0,1,2,3,4,5,6,6])
        self.assertEqual(_score(100,0),0)

    def test_invalid_events_disable_direction_without_crash(self):
        for bad in ({'symbol':'BTCUSDT','timestamp':'bad'},self.event(1,'nan'),self.event(1,-1)):
            data=self.data(); data['raw_events'].append(bad)
            result=self.calc(data)
            self.assertEqual(result['long_score'],0)
            self.assertEqual(result['indicators']['gate'],'invalid_rows')
            json.dumps(result,allow_nan=False)

    def test_zero_variance_and_empty_data_are_json_safe(self):
        result=self.calc({})
        json.dumps(result,allow_nan=False)
        self.assertIsNone(result['indicators']['windows']['5']['LONG']['speed_z'])
        s=_Series([])
        f,_=s.window(self.now.timestamp(),5,self.now.timestamp()-86400,self.config)
        self.assertIsNone(f['speed_z'])
        self.assertEqual(f['raw_score'],0)

    def test_missing_current_date_and_gapped_files(self):
        data=self.data(); data['raw_file_dates']=['2026-09-30','2026-10-01']
        result=self.calc(data)
        self.assertEqual(result['indicators']['gate'],'missing_raw_file')
        # A missing yesterday prevents using earlier dates as continuous coverage.
        data['raw_file_dates']=['2026-09-30','2026-10-02']
        early=self.now.replace(hour=16)-timedelta(days=1) # 01:00 KST October 2
        result=calculate_liquidation_speed(data,early,self.config)
        self.assertEqual(result['indicators']['gate'],'insufficient_history')

    def test_new_file_is_not_assumed_to_cover_from_midnight(self):
        data={'symbol':'BTCUSDT','raw_file_dates':['2026-10-02'],
              'raw_events':[self.event(2,10000)]}
        self.assertEqual(self.calc(data)['indicators']['gate'],'insufficient_history')

    def test_engine_routes_new_and_legacy_modes(self):
        data=self.data()
        self.assertEqual(MarketStateEngine().calc_liquidation_score(data,self.now),self.calc(data))
        legacy=MarketStateEngine(replace(self.config,liquidation_scoring_mode='rolling_hour_v1'))
        self.assertEqual(legacy.calc_liquidation_score(data,self.now),legacy._calc_liquidation_hour_score(data,self.now))
        bad=MarketStateEngine(replace(self.config,liquidation_scoring_mode='unknown'))
        with self.assertRaises(ValueError): bad.calc_liquidation_score(data,self.now)

    def test_other_score_components_are_unchanged(self):
        candles=[{'timestamp':(self.now-timedelta(days=365-i)).isoformat(),
                  'open':50000+i*10,'close':50020+i*10,'high':50100+i*10,
                  'low':49900+i*10,'volume':1000+i} for i in range(365)]
        current={'open':53650,'high':53900,'low':53500,'close':53800,'volume':2000}
        args={'current_candle':current,'current_time':self.now,'liquidation_data':self.data()}
        new=MarketStateEngine().update(candles,**args)
        old=MarketStateEngine(replace(self.config,liquidation_scoring_mode='rolling_hour_v1')).update(candles,**args)
        for key in new['score_components']:
            if key!='liquidation':
                self.assertEqual(new['score_components'][key],old['score_components'][key])

    def test_loader_dates_and_kst(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'liquidation_raw_2026_10_02.csv'
            p.write_text('event_time_kst,symbol,side,usd_size\n2026-10-02 11:58:00,BTCUSDT,BUY,10000\n',encoding='utf-8')
            data=load_liquidation_data(temp,'BTCUSDT')
            self.assertEqual(data['raw_file_dates'],['2026-10-02'])
            self.assertTrue(data['raw_events'][0]['event_time_kst'].endswith('+09:00'))
            self.assertEqual(self.calc(data)['indicators']['short_liq_5m'],10000)

    def test_recorder_preserves_indicators_and_strategy(self):
        liquidation=self.calc(self.data())
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'state.db'
            recorder=MarketStateRecorder(p)
            snapshot={'time':self.now.isoformat(),'symbol':'BTCUSDT','price':80000,
                      'result':{'long_score':6,'short_score':0,'indicators':{'liquidation':liquidation['indicators']},
                                'score_components':{'liquidation':{'long_score':6,'short_score':0}}}}
            self.assertTrue(recorder.save(snapshot))
            with closing(sqlite3.connect(p)) as c:
                version,raw,hour=c.execute('SELECT strategy_version,indicators_json,short_liq_1h FROM market_state').fetchone()
            self.assertEqual(version,STRATEGY_VERSION)
            self.assertNotEqual(version,'market_state_engine_v1')
            self.assertEqual(json.loads(raw)['liquidation'],liquidation['indicators'])
            self.assertEqual(hour,liquidation['indicators']['short_liq_1h'])


if __name__=='__main__':
    unittest.main()
