"""One-switch hypothetical reversal; no orders, persistence writes, or API calls."""
from bisect import bisect_right
from dataclasses import fields
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import unittest

import analyze_entry_paths as source
from early_exit_experiment_20261005 import ts, kst, net, flatten, identity, DB, MANIFEST
from profit_protection_experiment_20261004 import NoNetwork
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from market_state_engine.config import MarketStateConfig
from market_state_engine.live_trader import LiveTrader

ROOT=Path(__file__).resolve().parent


def trigger(side,row,threshold):
    own,other=('long_score','short_score') if side=='LONG' else ('short_score','long_score')
    return row.get(own)==0 and row.get(other) is not None and row[other]>=threshold


def result(row):
    return {'long_score':row['long_score'],'short_score':row['short_score'],
            'signal':row['decision'],'atr':row['atr'],
            'score_components':json.loads(row['score_components_json'] or '{}')}


def simulate(opposite,quantity,opening,following,config,baseline_end,baseline_price):
    trader=LiveTrader(config,client=NoNetwork(),dry_run=True,enabled=False,state_path=None,trade_log_path=None)
    # This experimental entry deliberately bypasses the ordinary entry threshold and evidence gate.
    trader._set_position_state('BTCUSDT',opposite,opening['price'],opening['timestamp'],
                               result(opening),quantity,dry_run=True)
    previous=opening; common=None
    for row in following:
        if row['group']!=opening['group']:
            status='strategy_change';break
        if row['t']-previous['t']>180:
            status='data_gap';break
        if common is None and row['t']>baseline_end:
            common={'net':net(opposite,opening['price'],baseline_price,quantity,config.futures_taker_fee_rate_pct),
                    'basis':'hypothetical_close_at_original_exit'}
        event=trader._sync_dry_run_position_state('BTCUSDT',row['price'],row['timestamp'],result(row))
        previous=row
        if event and event['type']=='LIVE_CLOSE':
            if common is None:
                common={'net':event['estimated_realized_pnl'],'basis':'closed_before_original_exit'}
            return {'status':'closed','exit':kst(row['t']),'net':event['estimated_realized_pnl'],
                    'reason':event['reason'],'hours':(row['t']-opening['t'])/3600,'common':common}
    else:
        status='data_end'
    if common is None and previous['t']>=baseline_end-2:
        common={'net':net(opposite,opening['price'],baseline_price,quantity,config.futures_taker_fee_rate_pct),
                'basis':'hypothetical_close_at_original_exit'}
    return {'status':status,'last':kst(previous['t']),
            'marked_net':net(opposite,opening['price'],previous['price'],quantity,config.futures_taker_fee_rate_pct),
            'hours':(previous['t']-opening['t'])/3600,'common':common}


def run():
    previous=json.loads((ROOT/'early_exit_experiment_20261005.json').read_text(encoding='utf-8'))
    manifest=json.loads(MANIFEST.read_text(encoding='utf-8'))
    digest=hashlib.sha256(DB.read_bytes()).hexdigest()
    assert digest==manifest['delta_sha256']
    source.FIELDS[:]=['timestamp','symbol','price','long_score','short_score','strategy_version',
                     'strategy_config_json','score_components_json','atr','decision']
    historical,files=source.load()
    merged={r['timestamp']:r for r in historical}
    merged.update({r['timestamp']:r for r in source.read(DB)})
    rows=sorted([r for r in merged.values() if ts(r['timestamp'])<=ts(manifest['until'])],key=lambda r:r['timestamp'])
    for row in rows:
        row['t']=ts(row['timestamp']);row['config']=json.loads(row['strategy_config_json'])
        row['group']=row['strategy_version']+':'+hashlib.sha256(json.dumps(row['config'],sort_keys=True).encode()).hexdigest()[:8]
    times=[r['t'] for r in rows]
    events={}; hashes={}
    for path in sorted(source.DOWNLOADS.glob('live_trade_log*.jsonl')):
        raw=path.read_bytes();hashes[str(path)]=hashlib.sha256(raw).hexdigest()
        for line in raw.decode('utf-8-sig').splitlines():
            if line.strip():
                for e in flatten(json.loads(line)):
                    if e.get('type')=='LIVE_CLOSE' and e.get('status')=='SENT' and e.get('symbol')=='BTCUSDT':
                        events[identity(e)]=e
    closed={ts(e.get('exit_time') or e['logged_at']):e for e in events.values()}
    output=[]
    for trade in previous['trades']:
        original=closed[trade['end_t']]
        quantity=abs(float(original['quantity']))
        a=bisect_right(times,trade['start_t']);b=bisect_right(times,trade['end_t']-2)
        path=rows[a:b]
        assert all(r['group']==trade['group'] for r in path)
        item={k:trade[k] for k in ('entry','exit','side','actual_net','group','start_t','end_t')}
        item['variants']={}
        for threshold in (7,8,9):
            found=next((r for r in path if trigger(trade['side'],r,threshold)),None)
            if found is None:
                item['variants'][str(threshold)]={'triggered':False,'same_horizon_net':trade['actual_net']}
                continue
            assert found['atr'] is not None and found['atr']>0
            config=MarketStateConfig(**{f.name:found['config'][f.name] for f in fields(MarketStateConfig) if f.name in found['config']})
            old_net=net(trade['side'],original['entry_price'],found['price'],quantity,config.futures_taker_fee_rate_pct)
            opposite='SHORT' if trade['side']=='LONG' else 'LONG'
            follow=rows[bisect_right(times,found['t']):]
            reverse=simulate(opposite,quantity,found,follow,config,trade['end_t'],original['exit_price'])
            common=reverse['common']
            item['variants'][str(threshold)]={'triggered':True,'switch_time':kst(found['t']),
                'long':found['long_score'],'short':found['short_score'],'old_position_net':old_net,
                'reverse_side':opposite,'reverse':reverse,
                'same_horizon_net':old_net+common['net'] if common else None,
                'two_leg_net':old_net+reverse['net'] if reverse['status']=='closed' else None}
        output.append(item)
    groups={}
    for group in sorted({t['group'] for t in output}):
        sample=[t for t in output if t['group']==group]
        common=[t for t in sample if all(t['variants'][str(h)]['same_horizon_net'] is not None for h in (7,8,9))]
        groups[group]={'n':len(sample),'matched_n':len(common),'baseline':sum(t['actual_net'] for t in common),'thresholds':{}}
        for h in ('7','8','9'):
            changed=[t for t in sample if t['variants'][h]['triggered']]
            finished=[t for t in changed if t['variants'][h]['two_leg_net'] is not None]
            gains=[t['variants'][h]['same_horizon_net']-t['actual_net'] for t in common]
            groups[group]['thresholds'][h]={
                'triggered':len(changed),'same_horizon_net':sum(t['variants'][h]['same_horizon_net'] for t in common),
                'same_horizon_delta':sum(gains),'improved':sum(x>1e-8 for x in gains),'worsened':sum(x< -1e-8 for x in gains),
                'reverse_closed':len(finished),'reverse_unfinished':len(changed)-len(finished),
                'reverse_net_closed':sum(t['variants'][h]['reverse']['net'] for t in finished),
                'old_leg_net_closed':sum(t['variants'][h]['old_position_net'] for t in finished),
                'two_leg_net_closed':sum(t['variants'][h]['two_leg_net'] for t in finished)}
    report={'eligible':len(output),'groups':groups,'trades':output,
            'method':'First own score == 0 and opposite >= 7/8/9; single reversal, same BTC quantity. '
                'New leg uses existing local exit code with recorded config and scores. No further entries. '
                'Primary comparison marks or closes new leg by original actual exit time; natural exits reported separately.',
            'limitations':'No tick fills, funding or slippage. Frozen historical entries; overlapping independent episodes, '
                'not portfolio profit. No actual v3 score history. Current exit implementation with stored settings '
                'does not reconstruct undocumented historical source changes.'}
    for path,h in hashes.items(): assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==h
    assert hashlib.sha256(DB.read_bytes()).hexdigest()==digest
    (ROOT/'zero_score_reversal_20261005.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'eligible':len(output),'groups':groups},ensure_ascii=False,indent=2))
    return report


class Tests(unittest.TestCase):
    def row(self,t,price=100,own=0,other=8):
        return {'t':t,'timestamp':datetime.fromtimestamp(t,timezone.utc).isoformat(),
                'group':'same','price':price,'long_score':own,'short_score':other,
                'decision':'NO_TRADE','atr':1,'score_components_json':'{}'}

    def test_exact_zero_and_thresholds(self):
        for threshold in (7,8,9):
            self.assertTrue(trigger('LONG',self.row(0,other=threshold),threshold))
            self.assertFalse(trigger('LONG',self.row(0,own=1,other=threshold),threshold))
            self.assertFalse(trigger('LONG',self.row(0,other=threshold-1),threshold))
            self.assertTrue(trigger('SHORT',self.row(0,own=threshold,other=0),threshold))

    def test_stop_and_common_horizon_costs(self):
        r=simulate('LONG',1,self.row(0),[self.row(60,98)],MarketStateConfig(),120,99)
        self.assertEqual(r['status'],'closed')
        self.assertEqual(r['reason'],'stop loss')
        self.assertAlmostEqual(r['common']['net'],-2.099)

    def test_pending_is_not_realized(self):
        r=simulate('LONG',1,self.row(0),[self.row(60)],MarketStateConfig(),60,100)
        self.assertEqual(r['status'],'data_end')
        self.assertNotIn('net',r)
        self.assertAlmostEqual(r['common']['net'],-.1)

    def test_short_fees_and_common_endpoint(self):
        r=simulate('SHORT',1,self.row(0),[self.row(60,100)],MarketStateConfig(),30,99.5)
        self.assertEqual(r['status'],'data_end')
        self.assertAlmostEqual(r['common']['net'],.40025)

    def test_missing_future_is_not_a_loss(self):
        for change in ('gap','version'):
            row=self.row(240 if change=='gap' else 60)
            if change=='version': row['group']='other'
            r=simulate('LONG',1,self.row(0),[row],MarketStateConfig(),120,99)
            self.assertEqual(r['status'],'data_gap' if change=='gap' else 'strategy_change')
            self.assertIsNone(r['common'])
            self.assertNotIn('net',r)


if __name__=='__main__':
    if '--test' in sys.argv: unittest.main(argv=[sys.argv[0]])
    else: run()
