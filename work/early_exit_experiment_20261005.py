"""Paired additional-exit study; frozen actual entries, no orders or runtime changes."""
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest

import analyze_entry_paths as source
from collect_trade_losses_20261005 import identity, flatten

ROOT=Path(__file__).resolve().parent
DB=Path('C:/Users/chlgh/AppData/Local/Temp/delta (3).db')
MANIFEST=DB.with_name('manifest (4).json')
KST=timezone(timedelta(hours=9))


def ts(s): return datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()
def kst(t): return datetime.fromtimestamp(t,KST).strftime('%m-%d %H:%M')


def triggered(side,row,own_max):
    own,other=('long_score','short_score') if side=='LONG' else ('short_score','long_score')
    return row.get(own) is not None and row.get(other) is not None and row[other]>=8 and row[own]<=own_max


def net(side,entry,exit,quantity,fee):
    return (1 if side=='LONG' else -1)*quantity*(exit-entry)-quantity*(entry+exit)*fee/100


def candidate(side,path,own_max,exit_t):
    # The recorded original exit wins a tie. No re-entry or reverse order is simulated.
    return next((r for r in path if r['t']<exit_t and triggered(side,r,own_max)),None)


def summary(trades,variant):
    before=sum(t['actual_net'] for t in trades)
    after=sum(t['variants'][variant]['net'] for t in trades)
    deltas=[t['variants'][variant]['net']-t['actual_net'] for t in trades]
    return {'n':len(trades),'baseline_net':before,'candidate_net':after,'delta':after-before,
            'triggered':sum(t['variants'][variant]['triggered'] for t in trades),
            'improved':sum(d>1e-8 for d in deltas),'worsened':sum(d< -1e-8 for d in deltas),
            'loss_savings':sum(t['variants'][variant]['net']-t['actual_net'] for t in trades if t['actual_net']<0),
            'winner_change':sum(t['variants'][variant]['net']-t['actual_net'] for t in trades if t['actual_net']>0),
            'winners_turned_loss':sum(t['actual_net']>0 and t['variants'][variant]['net']<0 for t in trades),
            'baseline_worst':min((t['actual_net'] for t in trades),default=None),
            'candidate_worst':min((t['variants'][variant]['net'] for t in trades),default=None)}


def run():
    manifest=json.loads(MANIFEST.read_text(encoding='utf-8'))
    source_hash=hashlib.sha256(DB.read_bytes()).hexdigest()
    assert source_hash==manifest['delta_sha256']
    source.FIELDS[:]=['timestamp','symbol','price','long_score','short_score','strategy_version',
                     'strategy_config_json']
    historical,files=source.load()
    merged={r['timestamp']:r for r in historical}
    merged.update({r['timestamp']:r for r in source.read(DB)})
    rows=sorted([r for r in merged.values() if ts(r['timestamp'])<=ts(manifest['until'])],key=lambda r:r['timestamp'])
    for r in rows:
        r['t']=ts(r['timestamp'])
        r['config']=json.loads(r['strategy_config_json'] or '{}')
        r['group']=r['strategy_version']+':'+hashlib.sha256(json.dumps(r['config'],sort_keys=True).encode()).hexdigest()[:8]
    times=[r['t'] for r in rows]
    unique={}; hashes={}
    paths=sorted(source.DOWNLOADS.glob('live_trade_log*.jsonl'),key=lambda p:int(re.search(r'\((\d+)\)',p.name)[1]) if '(' in p.name else 0)
    for path in paths:
        raw=path.read_bytes(); hashes[str(path)]=hashlib.sha256(raw).hexdigest()
        for line in raw.decode('utf-8-sig').splitlines():
            if not line.strip(): continue
            for e in flatten(json.loads(line)):
                if e.get('status')=='SENT' and not e.get('dry_run') and e.get('symbol')=='BTCUSDT':
                    unique[identity(e)]=e
    events=sorted(unique.values(),key=lambda e:e['logged_at'])
    active=None; added=False; pairs=[]; excluded=[]
    for e in events:
        if e['type']=='LIVE_ORDER':
            active=e; added=False
        elif e['type']=='LIVE_ADD': added=True
        elif e['type']=='LIVE_CLOSE':
            pairs.append((active,e,added)); active=None; added=False
    trades=[]
    for opening,close,added in pairs:
        reason=None
        if opening is None: reason='missing_entry'
        elif added: reason='additional_entry'
        elif close.get('estimated_fees') is None: reason='missing_fee_record'
        if reason:
            excluded.append({'exit':close['logged_at'],'reason':reason}); continue
        start=ts(opening.get('entry_time') or opening['logged_at'])
        end=ts(close.get('exit_time') or close['logged_at'])
        if close.get('entry_time') and abs(ts(close['entry_time'])-start)>120:
            reason='entry_time_mismatch'
        if close['side']!=opening.get('position_side'):
            reason='side_mismatch'
        a=bisect_right(times,start)-1; b=bisect_right(times,end)-1
        if a<0 or b<0 or start-times[a]>120 or end-times[b]>120 or end>times[-1]+120:
            reason='missing_boundary_data'
        if reason:
            excluded.append({'exit':close['logged_at'],'reason':reason}); continue
        path=rows[a:b+1]
        if any(y['t']-x['t']>180 for x,y in zip(path,path[1:])):
            reason='data_gap'
        elif any(r['group']!=path[0]['group'] for r in path):
            reason='strategy_change'
        if reason:
            excluded.append({'exit':close['logged_at'],'reason':reason}); continue
        quantity=abs(float(close['quantity']))
        if abs(quantity-abs(float(opening['quantity'])))>1e-8:
            excluded.append({'exit':close['logged_at'],'reason':'quantity_changed'});continue
        fee=path[0]['config'].get('futures_taker_fee_rate_pct')
        if fee is None:
            excluded.append({'exit':close['logged_at'],'reason':'missing_fee_config'});continue
        entry=close['entry_price'];actual=close['estimated_realized_pnl']
        assert abs(net(close['side'],entry,close['exit_price'],quantity,fee)-actual)<1e-6
        # Avoid stale pre-entry observations and the actual exit observation itself.
        future=[r for r in path if start<r['t']<end-2]
        item={'entry':kst(start),'exit':kst(end),'start_t':start,'end_t':end,'side':close['side'],
              'entry_time_basis':'recorded' if opening.get('entry_time') else 'entry_event_logged_at',
              'entry_price':entry,'group':path[0]['group'],'actual_net':actual,
              'actual_reason':close['reason'],'hold_hours':(end-start)/3600,'variants':{}}
        for name,maximum in (('A',3),('B',4),('C',5)):
            found=candidate(close['side'],future,maximum,end)
            when=found['t'] if found else end
            amount=net(close['side'],entry,found['price'],quantity,fee) if found else actual
            item['variants'][name]={'triggered':found is not None,'net':amount,'delta':amount-actual,
                'exit':kst(when),'end_t':when,'hold_hours':(when-start)/3600,
                'hours_saved':(end-when)/3600,'long':found['long_score'] if found else None,
                'short':found['short_score'] if found else None,'price':found['price'] if found else close['exit_price']}
        trades.append(item)
    groups={}
    for group in sorted({t['group'] for t in trades}):
        sample=sorted([t for t in trades if t['group']==group],key=lambda t:t['start_t'])
        midpoint=len(sample)//2
        groups[group]={name:summary(sample,name) for name in ('A','B','C')}
        groups[group]['time_split']={label:{v:summary(part,v) for v in ('A','B','C')}
                                    for label,part in (('earlier',sample[:midpoint]),('later',sample[midpoint:]))}
    result={'sources':[str(p) for p in files]+[str(DB)],'market_start':kst(times[0]),'market_end':kst(times[-1]),
            'closed_events':len(pairs),'eligible':len(trades),'exclusion_counts':dict(Counter(e['reason'] for e in excluded)),
            'exclusions':excluded,'groups':groups,'trades':trades,
            'limitations':['Frozen actual entries; no counterfactual re-entry or portfolio simulation.',
                'Historical observed average entry and minute prices, no funding or extra slippage.',
                'Versions and configurations separated, but undocumented historical code changes may remain.',
                'Time splits are descriptive, not an untouched out-of-sample test.']}
    assert len(trades)+len(excluded)==len(pairs)
    for t in trades:
        fired=[t['variants'][x]['end_t'] for x in ('A','B','C')]
        assert fired[2]<=fired[1]<=fired[0]
    for path,digest in hashes.items(): assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest
    assert hashlib.sha256(DB.read_bytes()).hexdigest()==source_hash
    (ROOT/'early_exit_experiment_20261005.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    write_report(result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('trades','sources','exclusions')},ensure_ascii=False,indent=2))
    return result


def write_report(result):
    lines=['# 반대 점수 8점 조기 청산 실험','',
        '## 결론',
        '**일부 큰 손실을 줄였지만, 회복할 거래를 손실로 확정하는 부작용도 있었다. 실거래 기본값은 변경하지 않았다.**',
        '진입 조건은 그대로 두고 추가 청산 조건만 비교했다. 반대 방향으로 새 주문을 넣는 실험이 아니다.',
        '', '## 조건',
        '- A: 반대 점수 8 이상 AND 보유 방향 점수 3 이하.',
        '- B: 반대 점수 8 이상 AND 보유 방향 점수 4 이하.',
        '- C: 반대 점수 8 이상 AND 보유 방향 점수 5 이하.',
        '- 점수 차이, 양수 요소 수, 지속시간을 추가 조건으로 요구하지 않았다. 사용자 제안을 그대로 비교했다.',
        '- 기존 실제 청산보다 먼저 조건을 만족한 첫 관측 시점에 청산한다고 가정했다. 기존 청산과 같은 시점이면 기존 청산을 우선했다.',
        '', '## 자료와 제외',
        f"- 가격·점수 DB 범위: {result['market_start']} ~ {result['market_end']} KST (2026년).",
        f"- 실제 청산 로그 {result['closed_events']}건 중 비교 가능 {result['eligible']}건. 수익·손실 모두 포함.",
        '- 제외: 비용 기록 없음 16건, 추가 진입 14건, 진입 또는 청산 경계 자료 부족 23건, 보유 중 전략/설정 변경 2건.',
        '- 같은 거래소 주문 ID는 중복 제거했다. 원본 DB/로그 해시 확인, 읽기 전용 분석.',
        '- 과거 명시적 entry_time이 없으면 대응하는 진입 이벤트 logged_at을 사용했다. 평단은 실제 청산 로그의 평단을 고정했다.',
        '- 같은 수량, 같은 과거 수수료 설정으로 왕복 수수료를 계산하고 기존 손익과 일치하는지 확인했다.',
        '', '## 전략·설정별 결과',
        '금액 단위: USDT. 원래 수익 거래와 손실 거래를 모두 포함한 독립 비교 합계이며 계좌 전체 재현 수익이 아니다.',
        '|기간 / 설정 그룹|건수|기존|A|B|C|', '|---|---:|---:|---:|---:|---:|']
    for group,g in sorted(result['groups'].items(),key=lambda item:min(t['start_t'] for t in result['trades'] if t['group']==item[0])):
        sample=[t for t in result['trades'] if t['group']==group]
        interval=min(t['entry'] for t in sample)+' ~ '+max(t['exit'] for t in sample)
        lines.append(f"|{interval} / {group.split(':')[-1]}|{g['A']['n']}|{g['A']['baseline_net']:+.2f}|{g['A']['candidate_net']:+.2f}|{g['B']['candidate_net']:+.2f}|{g['C']['candidate_net']:+.2f}|")
    lines+=['', '### 가장 많은 동일 설정 64건에서',
        '- B는 손실 거래의 합계 손실을 약 67.70 USDT 줄였다.',
        '- 그러나 원래 수익 거래의 손익이 약 59.82 USDT 나빠져, 순개선은 약 7.88 USDT에 그쳤다.',
        '- 원래 수익 거래 6건이 B에서는 손실로 종료됐다.',
        '- 시간순 앞 32건에서는 B가 기존보다 7.35 USDT 나빴고, 뒤 32건에서는 15.23 USDT 좋았다.',
        '- 따라서 같은 설정 안에서도 성과가 안정적이지 않았다. B를 최적값으로 확정할 근거는 부족하다.',
        '', '## 도움이 된 사례와 회복을 놓친 사례',
        '아래는 B 조건의 예시. 금액은 추정 왕복 수수료 차감 후다.',
        '|진입 KST|방향|실제 종료 손익|조기 청산 손익|차이|조기 청산 시각|',
        '|---|---|---:|---:|---:|---|']
    ranked=sorted(result['trades'],key=lambda t:t['variants']['B']['delta'])
    selected=ranked[-3:]+ranked[:3]+[t for t in result['trades'] if 'v2_' in t['group'] and t['variants']['B']['triggered']]
    for t in selected:
        v=t['variants']['B']
        lines.append(f"|{t['entry']}|{t['side']}|{t['actual_net']:+.2f}|{v['net']:+.2f}|{v['delta']:+.2f}|{v['exit']}|")
    lines+=['', '## 특히 최근 거래',
        '10월 3일 02:16 숏은 02:22에 LONG 14 / SHORT 3이 됐다. A·B·C 모두 여기서 -1.76 USDT에 청산한다.',
        '실제로는 보유를 이어가 03:53에 +3.53 USDT로 종료됐다. 이번 추가 조건은 기존 양수 요소 제한 등과 별개로 작동하므로, 기존 봇이 보유한 순간에도 정리할 수 있다.',
        '최근 5분 청산 방식 3건 합계는 기존 +10.45에서 A·B·C 모두 +5.16 USDT로 감소했다. 표본 3건만으로 일반화할 수는 없다.',
        '', '## 적용 판단',
        '- 아직 실거래에 활성화하지 않는다. 최신 양수 청산 순위 방식(v3)의 실제 거래 표본은 이번 자료에 없다.',
        '- 조기 청산 후 원래 방향으로 즉시 재진입할지, 얼마를 기다릴지에 따라 결과가 달라진다. 이번 실험은 그 효과를 포함하지 않는다.',
        '- 새 조건을 계속 검토한다면, 주문 없이 조건 도달을 기록하며 이후 실제 결과와 비교하는 관찰 단계가 먼저다.',
        '- 지속시간 등 다른 제한을 추가하는 것은 별도 가설이다. 이번 데이터 결과에 맞춰 조건을 계속 늘리지 않았다.',
        '', '## 한계',
        '- 실제 진입과 실제 기존 청산을 기준으로 추가 청산만 끼워 넣는 비교다. 기존 매매 엔진을 모든 틱에서 새로 재현한 전체 백테스트가 아니다.',
        '- 진입을 고정하므로 조기 청산 후 새 진입·쿨다운·복리·포지션 중복은 재현하지 않는다.',
        '- 분 단위 가격과 기록된 최종 평단 사용. 미세 체결 차이, 펀딩비, 추가 슬리피지는 제외했다.',
        '- 버전과 전체 설정을 분리했지만 기록되지 않은 과거 코드 변경까지 구분할 수는 없다.',
        '- 시간순 분할도 이미 살펴본 과거 데이터이며 독립된 미래 검증이 아니다.',
        '- 55건의 제외로 선택 편향이 있을 수 있다. 결과는 현재 자료가 충분한 96건에 한정한다.',
        '', '## 전체 개별 결과',
        '|진입 KST|방향|설정 그룹|기존 USDT|A USDT|B USDT|C USDT|',
        '|---|---|---|---:|---:|---:|---:|']
    for t in sorted(result['trades'],key=lambda t:t['start_t']):
        lines.append(f"|{t['entry']}|{t['side']}|{t['group'].split(':')[-1]}|{t['actual_net']:+.2f}|{t['variants']['A']['net']:+.2f}|{t['variants']['B']['net']:+.2f}|{t['variants']['C']['net']:+.2f}|")
    lines+=['','## 재현','`python work/early_exit_experiment_20261005.py`',
            '`python work/early_exit_experiment_20261005.py --test`',
            '계산 원본 결과: `work/early_exit_experiment_20261005.json`. 실거래 코드·설정·원본 자료는 변경하지 않았다.']
    (ROOT/'EARLY_EXIT_EXPERIMENT_20261005.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


class Tests(unittest.TestCase):
    def test_exact_thresholds_and_both_sides(self):
        for cap in (3,4,5):
            for side in ('LONG','SHORT'):
                own,other=('long_score','short_score') if side=='LONG' else ('short_score','long_score')
                self.assertTrue(triggered(side,{own:cap,other:8},cap))
                self.assertFalse(triggered(side,{own:cap+1,other:8},cap))
                self.assertFalse(triggered(side,{own:cap,other:7},cap))
                self.assertFalse(triggered(side,{own:None,other:8},cap))

    def test_first_hit_and_original_exit_tie(self):
        path=[{'t':1,'long_score':5,'short_score':8},{'t':2,'long_score':3,'short_score':8}]
        self.assertEqual(candidate('LONG',path,5,3)['t'],1)
        self.assertEqual(candidate('LONG',path,3,3)['t'],2)
        self.assertIsNone(candidate('LONG',path,3,2))

    def test_fee_both_directions_and_unaffected(self):
        self.assertAlmostEqual(net('LONG',100,101,1,.05),.8995)
        self.assertAlmostEqual(net('SHORT',100,99,1,.05),.9005)
        self.assertIsNone(candidate('SHORT',[{'t':1,'long_score':7,'short_score':0}],5,2))


if __name__=='__main__':
    if '--test' in sys.argv: unittest.main(argv=[sys.argv[0]])
    else: run()
