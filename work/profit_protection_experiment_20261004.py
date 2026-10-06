"""Read-only, paired exit experiment using recorded scores, not recomputed signals."""
from bisect import bisect_left, bisect_right
from contextlib import closing
from copy import deepcopy
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from market_state_engine.config import MarketStateConfig
from market_state_engine.live_trader import LiveTrader

DOWNLOADS = Path('C:/Users/chlgh/Downloads')
DB = Path('C:/Users/chlgh/AppData/Local/Temp/delta (3).db')
MANIFEST = Path('C:/Users/chlgh/AppData/Local/Temp/manifest (4).json')
LOG = DOWNLOADS / 'live_trade_log (14).jsonl'
START = '2026-09-27T15:00:00+00:00'
KST = timezone(timedelta(hours=9))


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def local(value):
    return datetime.fromtimestamp(value, KST).strftime('%m-%d %H:%M')


def flatten(event):
    if event.get('type') == 'LIVE_REVERSAL':
        return [e for e in (event.get('close_event'), event.get('entry_event')) if e]
    return [event]


def load_rows():
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    assert hashlib.sha256(DB.read_bytes()).hexdigest() == manifest['delta_sha256']
    end = manifest['until']
    merged, sources = {}, []

    def read(path):
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True)) as c:
            c.row_factory = sqlite3.Row
            query = '''SELECT timestamp,price,long_score,short_score,decision AS signal,atr,
                strategy_version,strategy_config_json,score_components_json,
                position_json,trade_event_json FROM market_state
                WHERE symbol='BTCUSDT' AND timestamp>=? AND timestamp<=? ORDER BY timestamp'''
            for raw in c.execute(query, (START, end)):
                r = dict(raw)
                r['t'] = stamp(r['timestamp'])
                r['config'] = json.loads(r.pop('strategy_config_json'))
                r['components'] = json.loads(r.pop('score_components_json'))
                r['position'] = json.loads(r.pop('position_json') or '{}')
                r['group'] = r['strategy_version'] + ':' + hashlib.sha256(
                    json.dumps(r['config'], sort_keys=True).encode()).hexdigest()[:8]
                merged[r['timestamp']] = r

    archives = []
    for path in DOWNLOADS.glob('btc_market_state_delta_*.zip'):
        with zipfile.ZipFile(path) as z:
            meta = json.loads(z.read('manifest.json'))
            archives.append((meta['created_at'], path, meta))
    for _, path, meta in sorted(archives):
        with zipfile.ZipFile(path) as z, tempfile.TemporaryDirectory() as temp:
            data = z.read('delta.db')
            assert hashlib.sha256(data).hexdigest() == meta['delta_sha256']
            temporary = Path(temp) / 'delta.db'
            temporary.write_bytes(data)
            read(temporary)
            sources.append({'path': str(path), 'sha256_verified': True})
    read(DB)
    sources.append({'path': str(DB), 'sha256_verified': True})
    return sorted(merged.values(), key=lambda r:r['t']), sources, manifest


class NoNetwork:
    def __getattr__(self, name):
        raise AssertionError('Network/client access forbidden: '+name)


def make_trader(config, state, remove_low):
    if remove_low:
        config = replace(config, small_profit_protection_min_peak_pct=config.small_profit_protection_mid_peak_pct)
    trader = LiveTrader(config, client=NoNetwork(), dry_run=True, enabled=False,
                        state_path=None, trade_log_path=None)
    trader.position_state = deepcopy(state)
    return trader


def result_for(row):
    return {'long_score':row['long_score'], 'short_score':row['short_score'],
            'signal':row['signal'], 'atr':row['atr'], 'score_components':row['components']}


def step(trader, row):
    return trader._sync_dry_run_position_state('BTCUSDT', row['price'], row['timestamp'], result_for(row))


def net_at(state, price, config):
    quantity = state['amount']
    sign = 1 if state['side'] == 'LONG' else -1
    gross = sign * quantity * (price-state['entry_price'])
    fee = quantity * (price+state['entry_price']) * config.futures_taker_fee_rate_pct/100
    return gross-fee


def replay(config, state, initial, future):
    trader = make_trader(config, state, True)
    last = initial
    best = worst = (initial['price']/state['entry_price']-1)*100*(1 if state['side']=='LONG' else -1)
    touches = {}
    for row in [initial]+future:
        if row['group'] != initial['group']:
            status = 'strategy_change'
            break
        if row['t']-last['t'] > 180:
            status = 'data_gap'
            break
        pnl = (row['price']/state['entry_price']-1)*100*(1 if state['side']=='LONG' else -1)
        best, worst = max(best,pnl), min(worst,pnl)
        for level in (0,1.5,3):
            if (pnl<=level if level==0 else pnl>=level) and str(level) not in touches:
                touches[str(level)] = local(row['t'])
        event = step(trader, row)
        last = row
        if event and event['type'] == 'LIVE_CLOSE':
            return {'status':'closed', 'event':event, 'end_t':row['t'], 'end':local(row['t']),
                    'net':event['estimated_realized_pnl'], 'pnl_pct':event['pnl_pct'],
                    'extra_hours':(row['t']-initial['t'])/3600,
                    'total_hours':(row['t']-stamp(state['entry_time']))/3600,
                    'best_observed_pct':best,'worst_observed_pct':worst,'touches':touches}
    else:
        status = 'data_end'
    return {'status':status, 'end_t':last['t'], 'end':local(last['t']),
            'net_if_closed_at_last':net_at(state,last['price'],config),
            'extra_hours':(last['t']-initial['t'])/3600,
            'best_observed_pct':best,'worst_observed_pct':worst,'touches':touches}


def run():
    rows,sources,manifest = load_rows()
    times = [r['t'] for r in rows]
    events = [e for line in LOG.read_text(encoding='utf-8-sig').splitlines() if line.strip()
              for e in flatten(json.loads(line))]
    actual = [e for e in events if e.get('type')=='LIVE_CLOSE' and e.get('status')=='SENT'
              and stamp(START)<=stamp(e['exit_time'])<=stamp(manifest['until'])]
    entries = {e['entry_time']:e for e in events if e.get('type')=='LIVE_ORDER' and e.get('status')=='SENT'}
    closes = [e for e in actual if e.get('reason','').startswith('profit protection')
              and 0.6<=e.get('peak_profit_pct',0)<1.5]
    output, exclusions = [], []
    for close in closes:
        t = stamp(close['exit_time'])
        index = bisect_right(times,t)-1
        if index<0 or t-times[index]>120 or close['entry_time'] not in entries:
            exclusions.append({'entry':close['entry_time'],'reason':'missing close snapshot or entry'})
            continue
        row = rows[index]
        config = MarketStateConfig(**{f.name:row['config'][f.name] for f in fields(MarketStateConfig)
                                     if f.name in row['config']})
        assert config.max_add_entries==0
        assert config.small_profit_protection_min_peak_pct==0.6
        assert config.small_profit_protection_mid_peak_pct==1.5
        opening = entries[close['entry_time']]
        state = {'symbol':'BTCUSDT','side':close['side'],'entry_time':close['entry_time'],
                 'entry_price':close['entry_price'],'amount':abs(float(close['quantity'])),
                 'dry_run':True,'stop_price':opening['stop_price'],'trailing_stop_price':None,
                 'trailing_active':False,'peak_profit_pct':close['peak_profit_pct'],
                 'best_price':close['entry_price'],'partial_taken':False,'add_count':0,
                 'entry_score':opening['score_context'][close['side'].lower()+'_score']}
        # The immediately preceding snapshot is the authoritative active stop, if present.
        for prior in reversed(rows[max(0,index-3):index+1]):
            pos = prior['position']
            if pos.get('entry_time')==close['entry_time'] and pos.get('status')=='OPEN':
                state['stop_price'] = pos['stop']
                assert not pos.get('trailing_active')
                break
        initial = {**row,'t':t,'timestamp':close['exit_time'],'price':close['exit_price']}
        initial.update({k:close['score_context'][k] for k in ('long_score','short_score','signal','atr')})
        assert (row['long_score'],row['short_score']) == (initial['long_score'],initial['short_score'])
        baseline = step(make_trader(config,state,False),initial)
        assert baseline and baseline['type']=='LIVE_CLOSE'
        assert baseline['reason']==close['reason']
        assert abs(baseline['estimated_realized_pnl']-close['estimated_realized_pnl'])<1e-7
        future = rows[index+1:]
        counter = replay(config,state,initial,future)
        horizons = {}
        for hours in (.5,1,2,4,8,24):
            target = t+hours*3600
            j = bisect_left(times,target)
            path = rows[index:j+1]
            valid = j<len(rows) and times[j]-target<=120
            valid = valid and all(q['group']==row['group'] for q in path)
            valid = valid and all(b['t']-a['t']<=180 for a,b in zip(path,path[1:]))
            if valid:
                horizons[str(hours)] = {'net_if_held':net_at(state,rows[j]['price'],config),
                    'pnl_pct':(rows[j]['price']/state['entry_price']-1)*100*(1 if state['side']=='LONG' else -1)}
        skipped = [e for e in entries.values() if t<stamp(e['entry_time'])<=counter['end_t']]
        output.append({'entry':local(stamp(close['entry_time'])),'entry_time':close['entry_time'],
                       'exit':local(t),'exit_t':t,'group':row['group'],'side':close['side'],
                       'actual_net':close['estimated_realized_pnl'],'actual_pct':close['pnl_pct'],
                       'actual_peak':close['peak_profit_pct'],
                       'actual_hours':(t-stamp(close['entry_time']))/3600,
                       'baseline_reproduced':True,'counterfactual':counter,'horizons':horizons,
                       'overlapping_actual_entries':len(skipped)})
    groups = {}
    for group in sorted({r['group'] for r in output}):
        subset = [r for r in output if r['group']==group]
        done = [r for r in subset if r['counterfactual']['status']=='closed']
        groups[group] = {'eligible':len(subset),'closed':len(done),'censored':len(subset)-len(done),
            'paired_actual_net':sum(r['actual_net'] for r in done),
            'paired_counterfactual_net':sum(r['counterfactual']['net'] for r in done),
            'better':sum(r['counterfactual']['net']>r['actual_net'] for r in done),
            'worse':sum(r['counterfactual']['net']<r['actual_net'] for r in done),
            'became_loss':sum(r['counterfactual']['net']<0 for r in done),
            'median_extra_hours':statistics.median(r['counterfactual']['extra_hours'] for r in done) if done else None,
            'horizons':{}}
        for h in (.5,1,2,4,8,24):
            sample=[r for r in subset if str(h) in r['horizons']]
            groups[group]['horizons'][str(h)]={'n':len(sample),
                'better':sum(r['horizons'][str(h)]['net_if_held']>r['actual_net'] for r in sample),
                'net_sum':sum(r['horizons'][str(h)]['net_if_held'] for r in sample),
                'actual_net_sum':sum(r['actual_net'] for r in sample)}
    result = {'start_kst':local(stamp(START)),'end_kst':local(stamp(manifest['until'])),
              'actual_closes':len(actual),'eligible_closes':len(closes),'exclusions':exclusions,
              'sources':sources,'rows':len(rows),'groups':groups,'trades':output}
    assert hashlib.sha256(DB.read_bytes()).hexdigest()==manifest['delta_sha256']
    (ROOT/'profit_protection_experiment_20261004.json').write_text(
        json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return result


class ExperimentTests(unittest.TestCase):
    def sample_state(self):
        return {'symbol':'BTCUSDT','side':'LONG','entry_price':100,'entry_time':START,
                'amount':1,'dry_run':True,'peak_profit_pct':0.8,'stop_price':98}

    def sample_row(self, minutes=0, price=100.4, group='same'):
        t=stamp(START)+minutes*60
        return {'timestamp':datetime.fromtimestamp(t,timezone.utc).isoformat(),'t':t,
                'price':price,'long_score':0,'short_score':0,'signal':'NO_TRADE',
                'atr':2,'components':{},'group':group}

    def test_remove_low_keeps_peak_tracking_and_upper_band(self):
        for side, sign in (('LONG',1),('SHORT',-1)):
            state={'symbol':'BTCUSDT','side':side,'entry_price':100,'entry_time':START,
                   'amount':1,'dry_run':True,'peak_profit_pct':0.8,'stop_price':90 if sign==1 else 110}
            trader=make_trader(MarketStateConfig(),state,True)
            self.assertIsNone(trader._get_small_profit_protection_reason(side,100+sign*0.4))
            self.assertIsNone(trader._get_small_profit_protection_reason(side,100+sign*2))
            self.assertEqual(trader.position_state['peak_profit_pct'],2)
            self.assertIsNotNone(trader._get_small_profit_protection_reason(side,100+sign*1.5))

    def test_original_is_unchanged(self):
        state={'entry_price':100,'peak_profit_pct':0.8}
        trader=make_trader(MarketStateConfig(),state,False)
        self.assertIsNotNone(trader._get_small_profit_protection_reason('LONG',100.4))
        self.assertEqual(state,{'entry_price':100,'peak_profit_pct':0.8})

    def test_stop_loss_still_executes_without_client(self):
        state={'symbol':'BTCUSDT','side':'LONG','entry_price':100,'entry_time':START,
               'amount':1,'dry_run':True,'peak_profit_pct':0.8,'stop_price':98}
        trader=make_trader(MarketStateConfig(),state,True)
        event=trader._sync_dry_run_position_state('BTCUSDT',97,START,
                  {'long_score':0,'short_score':0,'signal':'NO_TRADE'})
        self.assertEqual(event['reason'],'stop loss')
        self.assertLess(event['estimated_realized_pnl'],0)

    def test_reversal_requires_existing_score_and_component_rules(self):
        trader=make_trader(MarketStateConfig(),self.sample_state(),True)
        row=self.sample_row(price=99)
        row.update(short_score=14,signal='ENTER_SHORT')
        self.assertIsNone(step(trader,row))
        row['components']={k:{'short_score':1} for k in ('body','volume','liquidation')}
        event=step(trader,row)
        self.assertEqual(event['reason'],'confirmed opposite reversal')

    def test_three_percent_trailing_is_retained(self):
        trader=make_trader(MarketStateConfig(),self.sample_state(),True)
        event=step(trader,self.sample_row(price=104))
        self.assertEqual(event['type'],'LIVE_TRAILING_START')
        event=step(trader,self.sample_row(1,103.7))
        self.assertEqual(event['reason'],'trailing stop')

    def test_censors_gap_version_change_and_unfinished(self):
        cases=[([self.sample_row(4)],'data_gap'),
               ([self.sample_row(1,group='changed')],'strategy_change'),
               ([self.sample_row(1)],'data_end')]
        for future,status in cases:
            result=replay(MarketStateConfig(),self.sample_state(),self.sample_row(),future)
            self.assertEqual(result['status'],status)
            self.assertNotIn('net',result)


if __name__=='__main__':
    if '--test' in sys.argv:
        unittest.main(argv=[sys.argv[0]])
    else:
        run()
