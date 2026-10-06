"""Predeclared 5/15/30-minute exit confirmation study; offline and observational."""
from bisect import bisect_right
import hashlib
import json
from pathlib import Path
import sys
import unittest

import analyze_entry_paths as source
from early_exit_experiment_20261005 import ts,kst,net,summary,DB,MANIFEST
from collect_trade_losses_20261005 import identity,flatten

ROOT=Path(__file__).resolve().parent
VARIANTS={'immediate':('immediate',0)}
VARIANTS.update({f'{mode}_{minutes}':(mode,minutes) for mode in ('recheck','continuous','continuous_price') for minutes in (5,15,30)})
LABELS={'immediate':'기존 A: 즉시', 'recheck':'시간 뒤 재확인',
        'continuous':'점수 조건 연속 유지', 'continuous_price':'연속 유지 + 가격 악화'}


def warning(side,row):
    own,other=('long_score','short_score') if side=='LONG' else ('short_score','long_score')
    return row.get(own) is not None and row.get(other) is not None and row[own]<=3 and row[other]>=8


def adverse(side,current,reference):
    return current<reference if side=='LONG' else current>reference


def first_exit(side,path,mode,minutes):
    anchor=None;last=None
    for row in path:
        if last is not None and (row['t']-last['t']>180 or row.get('group')!=last.get('group')):
            return None  # A gap cannot prove continuous confirmation.
        last=row
        active=warning(side,row)
        if mode=='immediate':
            if active: return {'row':row,'warning':row}
            continue
        if anchor is None:
            if active: anchor=row
            continue
        if mode!='recheck' and not active:
            anchor=None
            continue
        if row['t']-anchor['t']<minutes*60:
            continue
        if not active:
            anchor=None
            continue
        if mode=='continuous_price' and not adverse(side,row['price'],anchor['price']):
            continue
        return {'row':row,'warning':anchor}
    return None


def label(key):
    mode,minutes=VARIANTS[key]
    return LABELS[mode]+(f' {minutes}분' if minutes else '')


def run():
    prior=json.loads((ROOT/'early_exit_experiment_20261005.json').read_text(encoding='utf-8'))
    manifest=json.loads(MANIFEST.read_text(encoding='utf-8'))
    digest=hashlib.sha256(DB.read_bytes()).hexdigest()
    assert digest==manifest['delta_sha256']
    source.FIELDS[:]=['timestamp','symbol','price','long_score','short_score','strategy_version','strategy_config_json']
    historical,files=source.load()
    merged={r['timestamp']:r for r in historical}
    merged.update({r['timestamp']:r for r in source.read(DB)})
    rows=sorted([r for r in merged.values() if ts(r['timestamp'])<=ts(manifest['until'])],key=lambda r:r['timestamp'])
    for r in rows:
        r['t']=ts(r['timestamp']);r['config']=json.loads(r['strategy_config_json'])
        r['group']=r['strategy_version']+':'+hashlib.sha256(json.dumps(r['config'],sort_keys=True).encode()).hexdigest()[:8]
    times=[r['t'] for r in rows]
    events={};hashes={}
    for path in sorted(source.DOWNLOADS.glob('live_trade_log*.jsonl')):
        raw=path.read_bytes();hashes[str(path)]=hashlib.sha256(raw).hexdigest()
        for line in raw.decode('utf-8-sig').splitlines():
            if line.strip():
                for e in flatten(json.loads(line)):
                    if e.get('type')=='LIVE_CLOSE' and e.get('status')=='SENT' and e.get('symbol')=='BTCUSDT':
                        events[identity(e)]=e
    closed={ts(e.get('exit_time') or e['logged_at']):e for e in events.values()}
    trades=[]
    for old in prior['trades']:
        close=closed[old['end_t']];quantity=abs(float(close['quantity']))
        a=bisect_right(times,old['start_t']);b=bisect_right(times,old['end_t']-2)
        path=rows[a:b]
        assert all(r['group']==old['group'] for r in path)
        item={k:old[k] for k in ('entry','exit','start_t','end_t','side','actual_net','group')}
        item['variants']={}
        for name,(mode,minutes) in VARIANTS.items():
            hit=first_exit(old['side'],path,mode,minutes)
            row=hit['row'] if hit else None
            amount=net(old['side'],close['entry_price'],row['price'],quantity,
                       row['config']['futures_taker_fee_rate_pct']) if hit else old['actual_net']
            item['variants'][name]={'triggered':bool(hit),'net':amount,'delta':amount-old['actual_net'],
                'exit':kst(row['t']) if hit else old['exit'],'end_t':row['t'] if hit else old['end_t'],
                'warning_time':kst(hit['warning']['t']) if hit else None,
                'wait_minutes':(row['t']-hit['warning']['t'])/60 if hit else None,
                'long':row['long_score'] if hit else None,'short':row['short_score'] if hit else None,
                'price_change_from_warning_pct':(row['price']/hit['warning']['price']-1)*100 if hit else None}
            if name=='continuous_price_5':
                # Execution robustness check, not another optimized signal threshold.
                next_index=bisect_right(times,row['t']) if hit else None
                next_row=rows[next_index] if hit and next_index<len(rows) else None
                if next_row and next_row['t']<old['end_t']-2:
                    assert next_row['group']==old['group'] and next_row['t']-row['t']<=180
                    delayed=net(old['side'],close['entry_price'],next_row['price'],quantity,
                                next_row['config']['futures_taker_fee_rate_pct'])
                else:
                    delayed=old['actual_net']
                item['delayed_execution_net']=delayed
        assert abs(item['variants']['immediate']['net']-old['variants']['A']['net'])<1e-7
        for name in VARIANTS:
            assert item['variants'][name]['end_t']>=item['variants']['immediate']['end_t']
        trades.append(item)
    groups={}
    for group in sorted({t['group'] for t in trades}):
        sample=sorted([t for t in trades if t['group']==group],key=lambda t:t['start_t'])
        split=len(sample)//2
        groups[group]={'n':len(sample),'start':sample[0]['entry'],'end':max(t['exit'] for t in sample),
            'results':{key:summary(sample,key) for key in VARIANTS},
            'earlier':{key:summary(sample[:split],key) for key in VARIANTS},
            'later':{key:summary(sample[split:],key) for key in VARIANTS}}
        groups[group]['price_5_next_observation_net']=sum(t['delayed_execution_net'] for t in sample)
    out={'eligible':len(trades),'variants':VARIANTS,'warning':'opposite >= 8 AND own <= 3',
         'groups':groups,'trades':trades,'source_manifest':manifest,
         'limitations':['Frozen actual entries and original exits; no re-entry/portfolio/funding/slippage simulation.',
            'Exploratory reused historical data, time splits are not held-out validation.',
            'Price deterioration means any adverse move from warning price, not a tuned minimum.',
            'No current v3 strategy trades in this export.']}
    for path,h in hashes.items(): assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==h
    assert hashlib.sha256(DB.read_bytes()).hexdigest()==digest
    (ROOT/'exit_confirmation_experiment_20261005.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    write_report(out)
    for group,g in groups.items():
        print(group,g['n'],g['start'],g['end'])
        for key,s in g['results'].items():
            print(key, 'net',round(s['candidate_net'],2),'delta',round(s['delta'],2),
                  'hits',s['triggered'],'won_to_loss',s['winners_turned_loss'],
                  'early_delta',round(g['earlier'][key]['delta'],2),'late_delta',round(g['later'][key]['delta'],2))
    return out


def write_report(out):
    lines=['# 점수 지속과 가격 확인: 조기 청산 실험','',
        '실거래 코드·설정은 변경하지 않았다. 이번에는 반대로 진입하지 않고 정리 후 관망하는 독립 거래 비교다.',
        '', '## 요약',
        '- 가장 큰 동일 설정 64건: 5분 연속 유지 + 가격 악화가 기존 -38.65에서 +5.67 USDT로 개선됐다. 16건 발동, 12건 개선, 4건 악화였다.',
        '- 손실 거래에서 53.68 USDT를 줄였지만 원래 수익 거래에서 9.36 USDT를 잃었다. 수익→손실 2건이 남았다.',
        '- 최대 단일 손실은 -24.62에서 -11.97 USDT로 감소했다. 미래 최대 손실을 보장하는 값은 아니다.',
        '- 같은 64건의 시간순 앞 절반 +28.51, 뒤 절반 +15.81 USDT 개선. 독립적인 미래 검증은 아니다.',
        '- 다른 11건 설정에서는 +11.60 USDT 개선, 8건 설정에서는 +0.81 개선, 10건 및 최근 3건 설정에서는 발동 없음으로 동일했다.',
        '- 이번 탐색의 관찰 후보이지 검증된 실거래 규칙이 아니다. 최적값을 선택한 뒤에는 새로운 데이터로 조건을 고정해 확인해야 한다.',
        '', '## 먼저 고정한 조건',
        '- 경고: 반대 점수 8 이상 AND 보유 방향 점수 3 이하. 모든 실험에서 동일하다.',
        '- 대기 시간: 5분, 15분, 30분. 세 시간과 아래 세 방식을 사전에 정해 총 9개를 계산했다.',
        '- 비교 기준: 기존 실제 청산과 이전 A안(즉시 조기 청산).',
        '- 재확인: 첫 경고부터 정해진 시간이 지난 뒤 현재 조건을 확인한다. 중간에 풀렸어도 종료 시점에 충족하면 정리한다. 종료 시점에 풀렸으면 새 경고를 기다린다.',
        '- 연속 유지: 매 관측에서 경고 조건이 유지돼야 한다. 중간에 해제되면 시간을 다시 센다.',
        '- 연속 유지 + 가격: 위 연속 조건과 함께 첫 경고 가격보다 불리한 가격이어야 한다. LONG은 더 낮고 SHORT는 더 높아야 한다. 가격이 버티면 기다린다.',
        '- 가격 악화 폭은 별도 최적화 없이 0보다 큰 불리한 변화로 정했다. 작은 가격 흔들림도 포함하는 한계가 있다.',
        '- 기존 실제 청산 시점이 먼저 오면 기존 결과를 사용한다. 확인 중 손절이나 익절을 미루는 실험은 아니다.',
        '', '## 표본과 해석',
        '- 이전과 동일한 96건, 수익·손실 모두 포함. 버전/설정별 분리. 151건 중 추가 진입, 자료 부족, 전략 변경 등 55건 제외.',
        '- 진입과 수량은 실제 기록 그대로 고정했다. 평균 단가와 왕복 추정 수수료 반영. 펀딩비·추가 슬리피지 미반영.',
        '- 매분 관측 기준이므로 관측 사이 신호 해제나 가격 변동은 확인할 수 없다. 3분 초과 공백은 연속성을 인정하지 않는다.',
        '- 재진입과 복리·포지션 중복은 계산하지 않았다. 금액 합계는 계좌 전체 재현 손익이 아니다.',
        '- 같은 과거 데이터에서 여러 실험을 수행한 탐색 결과다. 시간순 앞/뒤 분할도 새 자료 검증이 아니다.',
        '- 새 v3 청산 배점 이후 실거래 표본은 이 자료에 없다.',
        '']
    for group,g in sorted(out['groups'].items(),key=lambda item:item[1]['start']):
        lines += [f"## {g['start']} ~ {g['end']} / {g['n']}건",f"설정: `{group}`",'',
            f"기존 실제 손익 합계: {g['results']['immediate']['baseline_net']:+.2f} USDT.",
            '|조건|손익 USDT|기존 대비|발동|개선/악화 건수|수익→손실 건수|앞 절반 개선액|뒤 절반 개선액|',
            '|---|---:|---:|---:|---|---:|---:|---:|']
        for key,s in g['results'].items():
            lines.append(f"|{label(key)}|{s['candidate_net']:+.2f}|{s['delta']:+.2f}|{s['triggered']}|{s['improved']} / {s['worsened']}|{s['winners_turned_loss']}|{g['earlier'][key]['delta']:+.2f}|{g['later'][key]['delta']:+.2f}|")
        lines.append(f"- 5분 연속 + 가격 조건의 체결을 다음 관측까지 늦춘 보조 비교: {g['price_5_next_observation_net']:+.2f} USDT. 그 전에 실제 기존 종료가 오면 기존 결과를 사용했다. 정확한 실체결 모형은 아니다.")
    lines+=['','## 개별 결과','상세 96건 × 10조건은 동명의 JSON에 모두 저장했다. 원본 DB와 거래 로그는 수정하지 않았다.',
            '','## 재현과 테스트','`python work/exit_confirmation_experiment_20261005.py`',
            '`python work/exit_confirmation_experiment_20261005.py --test`',
            '현재 코드에 실험 청산 규칙을 추가하거나 활성화하지 않았다.']
    (ROOT/'EXIT_CONFIRMATION_EXPERIMENT_20261005.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


class Tests(unittest.TestCase):
    def row(self,minute,active=True,price=100):
        return {'t':minute*60,'long_score':0 if active else 10,'short_score':8 if active else 0,'price':price,'group':'x'}

    def test_wait_and_recovery_reset(self):
        path=[self.row(m,m!=2) for m in range(6)]
        self.assertEqual(first_exit('LONG',path,'recheck',5)['row']['t'],300)
        self.assertIsNone(first_exit('LONG',path,'continuous',5))
        path.extend(self.row(m) for m in (6,7,8))
        self.assertEqual(first_exit('LONG',path,'continuous',5)['row']['t'],480)

    def test_price_holds_then_deteriorates(self):
        path=[self.row(m,price=101 if m else 100) for m in range(6)]
        self.assertIsNone(first_exit('LONG',path,'continuous_price',5))
        path.append(self.row(6,price=99))
        self.assertEqual(first_exit('LONG',path,'continuous_price',5)['row']['t'],360)

    def test_short_symmetry_and_missing_scores(self):
        path=[{**self.row(m,price=100+m),'long_score':8,'short_score':0} for m in range(6)]
        self.assertIsNotNone(first_exit('SHORT',path,'continuous_price',5))
        self.assertFalse(warning('LONG',{'long_score':None,'short_score':8}))

    def test_gap_and_version_are_not_confirmation(self):
        self.assertIsNone(first_exit('LONG',[self.row(0),self.row(5)],'continuous',5))
        self.assertIsNone(first_exit('LONG',[self.row(0),{**self.row(1),'group':'y'}],'continuous',5))

    def test_immediate_and_no_warning(self):
        self.assertEqual(first_exit('LONG',[self.row(0)],'immediate',0)['row']['t'],0)
        self.assertIsNone(first_exit('LONG',[self.row(0,False)],'immediate',0))

    def test_equal_price_is_not_adverse_and_future_does_not_change_first_hit(self):
        path=[self.row(m) for m in range(6)]
        self.assertIsNone(first_exit('LONG',path,'continuous_price',5))
        path.append(self.row(6,price=99))
        hit=first_exit('LONG',path,'continuous_price',5)
        self.assertEqual(first_exit('LONG',path+[self.row(7,False,102)],'continuous_price',5),hit)

    def test_recheck_restarts_when_due_condition_is_false(self):
        path=[self.row(m,m!=5) for m in range(11)]
        self.assertIsNone(first_exit('LONG',path,'recheck',5))
        path.append(self.row(11))
        self.assertEqual(first_exit('LONG',path,'recheck',5)['row']['t'],660)


if __name__=='__main__':
    if '--test' in sys.argv: unittest.main(argv=[sys.argv[0]])
    else: run()
