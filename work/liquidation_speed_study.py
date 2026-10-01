"""Offline liquidation window study; never modifies trading code or source data."""
import csv
from collections import Counter
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tarfile
import unittest

import numpy as np
import analyze_entry_paths as src

KST = timezone(timedelta(hours=9))
ARCHIVE = Path('C:/Users/chlgh/Downloads/liquidation_research_20261001_211232.tar.gz')


def stamp(s):
    d = datetime.fromisoformat(s)
    return (d if d.tzinfo else d.replace(tzinfo=KST)).timestamp()


def local(t):
    return datetime.fromtimestamp(t, KST).strftime('%m-%d %H:%M')


class Series:
    def __init__(self, events):
        events = sorted(events)
        self.t = np.array([e[0] for e in events], dtype=float)
        self.v = np.array([e[1] for e in events], dtype=float)
        self.p = np.r_[0., np.cumsum(self.v)]

    def amount(self, start, end):
        # Timestamp precision is seconds: use [start,end), excluding current second.
        a = np.searchsorted(self.t, start, side='left')
        b = np.searchsorted(self.t, end, side='left')
        return self.p[b] - self.p[a]

    def features(self, t, minutes, start, baseline_hours=24):
        t = np.floor(t)
        w = minutes * 60
        current = float(self.amount(t-w, t) / minutes)
        previous = float(self.amount(t-2*w, t-w) / minutes)
        ends = np.arange(t-w, max(start+w, t-baseline_hours*3600)-.1, -w)[::-1]
        history = self.amount(ends-w, ends) / minutes
        delta = np.diff(history)
        # Historical reference ends before the current window begins.
        def z(value, a):
            sd = float(np.std(a, ddof=1)) if len(a)>1 else 0
            return float((value-np.mean(a))/sd) if len(a)*minutes>=360 and sd>1e-9 else None
        a = np.searchsorted(self.t, t-w)
        b = np.searchsorted(self.t, t)
        return {'speed':current, 'previous_speed':previous, 'acceleration':current-previous,
                'rising':current>previous, 'speed_z':z(current, history),
                'acceleration_z':z(current-previous, delta), 'reference_n':len(history),
                'event_count':int(b-a),
                'active_minutes':int(len(np.unique(np.floor((self.t[a:b]-(t-w))/60)))),
                'largest_share':float(max(self.v[a:b],default=0)/(current*minutes)) if current>0 else None}


def load_raw():
    raw, aggregates, counts, duplicate_count = [], [], Counter(), 0
    seen = set()
    invalid = 0
    with tarfile.open(ARCHIVE, 'r:gz') as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.endswith('.csv'):
                continue
            stream=archive.extractfile(member)
            with io.TextIOWrapper(stream, encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    if 'liquidation_raw_' in member.name:
                        counts[row['symbol']]+=1
                        key=tuple(sorted(row.items()))
                        if key in seen:
                            duplicate_count+=1
                            continue
                        seen.add(key)
                        try:
                            t=stamp(row['event_time_kst']); value=float(row['usd_size'])
                            assert np.isfinite(value) and value>=0 and row['side'] in ('BUY','SELL')
                            assert row['liq_type']==('short_liquidation' if row['side']=='BUY' else 'long_liquidation')
                            raw.append((t,value,row['side'],row['symbol']))
                        except (ValueError, AssertionError):
                            invalid+=1
                    elif row['symbol']=='BTCUSDT':
                        aggregates.append(row)
    return sorted(raw), aggregates, dict(counts), duplicate_count, invalid


def summarize(records, minutes, gate):
    kept=[r for r in records if gate(r['features'][str(minutes)])]
    blocked=[r for r in records if not gate(r['features'][str(minutes)])]
    return {'n':len(records),'kept':len(kept),'blocked':len(blocked),
            'loss_blocked':sum(r['net']<0 for r in blocked),
            'win_blocked':sum(r['net']>0 for r in blocked),
            'kept_net':sum(r['net'] for r in kept),
            'blocked_net':sum(r['net'] for r in blocked)}


def main():
    raw, agg, counts, duplicates, invalid=load_raw()
    btc=[r for r in raw if r[3]=='BTCUSDT']
    series={side:Series([(t,v) for t,v,s,_ in btc if s==eventside]) for side,eventside in [('LONG','BUY'),('SHORT','SELL')]}
    start=np.ceil(raw[0][0]/60)*60
    end=np.floor(raw[-1][0]/60)*60
    reconciliation=[]
    for r in agg:
        a,b=stamp(r['window_start_kst']),stamp(r['window_end_kst'])
        if a<start or b>end: continue
        diffs=[float(series[side].amount(a,b))-float(r[key]) for side,key in [('LONG','short_liq_usd'),('SHORT','long_liq_usd')]]
        reconciliation.append({'start':local(a),'diffs':diffs})
    minute_ends=np.arange(start+600,end+1,60)
    coverage={}
    for w in (1,5,10):
        vals=np.concatenate([s.amount(minute_ends-w*60,minute_ends)/w for s in series.values()])
        flips=[]
        for s in series.values():
            speed=s.amount(minute_ends-w*60,minute_ends)/w
            prev=s.amount(minute_ends-2*w*60,minute_ends-w*60)/w
            state=np.sign(speed-prev)
            flips.append(float(np.mean(state[1:]!=state[:-1])*100))
        coverage[str(w)]={'zero_pct':float(np.mean(vals==0)*100),'state_change_pct':float(np.mean(flips)),
                          'speed_p50':float(np.median(vals)),'speed_p95':float(np.percentile(vals,95)),
                          'speed_p99':float(np.percentile(vals,99))}
    src.FIELDS.extend(k for k in ('short_liq_1h','long_liq_1h') if k not in src.FIELDS)
    market,_=src.load()
    mt=np.array([r['t'] for r in market])
    def at(t):
        i=int(np.searchsorted(mt,t,side='right')-1)
        return market[i] if i>=0 and t-mt[i]<=120 else None
    def future(t, minutes, group):
        j=int(np.searchsorted(mt,t+minutes*60))
        i=int(np.searchsorted(mt,t,side='right')-1)
        if j>=len(mt) or mt[j]-t-minutes*60>120: return None
        path=market[i:j+1]
        if any(r['group']!=group for r in path) or np.max(np.diff(mt[i:j+1]),initial=0)>180: return None
        return market[j]['price']
    review=json.loads((src.ROOT/'weekly_failure_20261002.json').read_text())
    trades=[]
    for r in review['trades']:
        t=stamp(r['entry_time'])
        if t<start+6*3600 or t>end: continue
        q=at(t)
        if not q: continue
        # Use the market observation's time, before the actual order was submitted.
        t=q['t']
        f={str(w):series[r['side']].features(t,w,start) for w in (1,5,10)}
        returns={str(h):((p/r['entry_price']-1)*100*(1 if r['side']=='LONG' else -1)) if p else None for h in (15,60,240) for p in [future(t,h,q['group'])]}
        trades.append({**{k:r[k] for k in ('entry','side','net','pnl_pct','peak','new_rule','entry_time')},
                       't':t,'group':q['group'],'features':f,'future_returns':returns,
                       'features_6h':{str(w):series[r['side']].features(t,w,start,6) for w in (1,5,10)},
                       'features_lag60':{str(w):series[r['side']].features(t-60,w,start) for w in (1,5,10)},
                       'liq_score':r['components']['liquidation'][r['side'].lower()+'_score']})
    gates={'increase':lambda f:f['rising'],
           'above_mean':lambda f:f['speed_z'] is not None and f['speed_z']>0,
           'increase_above_mean':lambda f:f['rising'] and f['speed_z'] is not None and f['speed_z']>0}
    assert len({t['group'] for t in trades})==1, 'Split differing strategy configurations before comparing trades'
    comparison={name:{str(w):summarize(trades,w,gate) for w in (1,5,10)} for name,gate in gates.items()}
    for windows in comparison.values():
        for s in windows.values():
            assert s['kept']+s['blocked']==len(trades)
            assert abs(s['kept_net']+s['blocked_net']-sum(t['net'] for t in trades))<1e-8
    # Chronological halves and one-minute lag are diagnostics, not independent validation.
    sensitivity={}
    for name,field in [('baseline_6h','features_6h'),('lag60','features_lag60')]:
        records=[{**r,'features':r[field]} for r in trades]
        sensitivity[name]={str(w):summarize(records,w,gates['increase_above_mean']) for w in (1,5,10)}
    halves={name:{str(w):summarize(records,w,gates['increase_above_mean']) for w in (1,5,10)} for name,records in [('early',trades[:len(trades)//2]),('late',trades[len(trades)//2:])]}
    # An independent descriptive grid, greedily spaced four hours, with common times for all windows.
    observations=[]; last=-1e30
    for q in market:
        t=q['t']
        if t<start+24*3600 or t>end or t-last<14400: continue
        side='LONG' if q['long_score']>=q['short_score'] else 'SHORT'
        own=q[side.lower()+'_score']; other=q['short_score' if side=='LONG' else 'long_score']
        component=json.loads(q['score_components_json'])['liquidation'][side.lower()+'_score']
        if own<10 or own-other<5 or component!=6 or q['range_block_trade']: continue
        prices={str(h):future(t,h,q['group']) for h in (15,60,240)}
        if any(v is None for v in prices.values()): continue
        last=t
        observations.append({'at':local(t),'group':q['group'],'side':side,
                             'features':{str(w):series[side].features(t,w,start) for w in (1,5,10)},
                             'returns':{h:(p/q['price']-1)*100*(1 if side=='LONG' else -1) for h,p in prices.items()}})
    grid=[]
    for group in sorted({r['group'] for r in observations}):
        for w in (1,5,10):
            for keep in (True,False):
                selected=[r for r in observations if r['group']==group and gates['increase_above_mean'](r['features'][str(w)])==keep]
                grid.append({'group':group,'minutes':w,'pass':keep,'n':len(selected),
                             'mean_returns':{str(h):float(np.mean([r['returns'][str(h)] for r in selected])) if selected else None for h in (15,60,240)}})
    # Compare raw reconstruction to the actual stored rolling-hour inputs.
    deltas=[]
    for q in market:
        t=q['t']
        if start+3600<=t<=end:
            for side,key in [('LONG','short_liq_1h'),('SHORT','long_liq_1h')]:
                value=float(series[side].amount(np.floor(t)-3600,np.floor(t)))
                stored=q[key]
                if stored is not None: deltas.append(abs(value-stored)/max(abs(stored),1))
    quality={'symbols':counts,'btc_rows':len(btc),'exact_duplicate_rows':duplicates,'invalid_rows':invalid,
             'start':local(start),'end':local(end),'aggregate_windows':len(reconciliation),
             'aggregate_mismatches':sum(max(abs(x) for x in r['diffs'])>.01 for r in reconciliation),
             'max_all_symbol_silence_minutes':float(np.max(np.diff([r[0] for r in raw]))/60),
             'rolling1h_relative_error_p50':float(np.median(deltas)),
             'rolling1h_relative_error_p95':float(np.percentile(deltas,95))}
    # Entry following a reversal close in <=120 seconds is a reversal, not a new independent trade.
    for t in trades:
        previous=[r for r in review['trades'] if r['entry_time']!=t['entry_time'] and
                  0<=stamp(t['entry_time'])-(stamp(r['entry_time'])+r['hours']*3600)<=120 and
                  r['side']!=t['side'] and 'reversal' in r['reason']]
        t['entry_kind']='reversal' if previous else 'normal'
    by_kind={kind:{str(w):summarize([t for t in trades if t['entry_kind']==kind],w,gates['increase_above_mean']) for w in (1,5,10)} for kind in ('normal','reversal')}
    result={'quality':quality,'coverage':coverage,'trades':trades,'comparison':comparison,'by_kind':by_kind,
            'sensitivity':sensitivity,'halves':halves,'observations':observations,'grid':grid}
    (src.ROOT/'liquidation_speed_results.json').write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    write_report(result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('trades','observations')},ensure_ascii=False,indent=2))
    print('TRADES',json.dumps([{k:r[k] for k in ('entry','side','net','features')} for r in trades],ensure_ascii=False))


def write_report(r):
    q=r['quality']; lines=['# 청산 속도 1분·5분·10분 비교','',
        f"자료 범위: {q['start']} ~ {q['end']} KST. BTC 원본 {q['btc_rows']:,}행.",
        '## 먼저 읽는 결론',
        '- 이번 실제 거래 11건에서는 5분 구간이 가장 유망한 점검 후보였습니다. 실거래 최적값으로 확정한 것은 아닙니다.',
        '- 증가 여부만 보면 작은 청산 재발도 통과합니다. 과거 평균 대비 크기를 함께 봐야 했습니다.',
        '- 1분은 무발생과 단발 사건에 민감했습니다. 10분은 과거 청산 증가가 남아 5분 감소를 가리는 사례가 있었습니다.',
        '- 5분에서도 수익 거래를 놓쳤고 큰 청산 증가 후 손실 사례가 남았습니다. 청산 가속은 가격 상승/하락의 보장이 아닙니다.',
        '- 별도 4시간 간격 관측에서는 통과군도 평균 수익률이 음수였습니다. 예측력이 검증됐다는 결론은 내릴 수 없습니다.',
        '## 검증 범위와 한계',
        '- 실제 종료된 거래 중 원본 청산 기록과 과거 기준이 있는 거래만 비교했습니다. 미래 수익은 특징 계산에 쓰지 않았습니다.',
        '- 속도=구간 청산 금액/분. 가속=최근 속도-직전 같은 길이 구간 속도. LONG은 숏 청산, SHORT는 롱 청산입니다.',
        '- 표준화=(현재 속도-과거 평균)/과거 표준편차. 기준은 현재 구간 시작 전 최대 24시간의 겹치지 않는 동일 길이 구간입니다. 최소 6시간 필요.',
        '- 가속 표준화도 과거 속도 차이의 평균·표준편차를 사용했습니다. 원본 초 단위 시간의 현재 초는 제외했습니다.',
        '- 실제 수집 도착 시각은 CSV에 없으므로 당시 데이터 도착 지연을 정확히 재현할 수 없습니다. 1분 지연 민감도를 별도로 계산했습니다.',
        '- 청산 공백은 무발생인지 연결 끊김인지 확정할 수 없습니다. 수집 정상 가정에서 무기록 구간은 0입니다.',
        f"- 전체 수집 심볼 기준 가장 긴 무기록 간격은 {q['max_all_symbol_silence_minutes']:.2f}분입니다. 하트비트가 없어 정상 수집을 확정할 수 없습니다.",
        '- 원본에는 다른 심볼도 들어 있습니다. 이번 계산은 BTCUSDT만 사용했습니다.',
        f"- 완전히 같은 원본 행 {q['exact_duplicate_rows']}개, 잘못된 행 {q['invalid_rows']}개. 완전 일치 행만 중복 제거했습니다. 주문 ID가 없어 확정 중복 판별은 제한됩니다.",
        f"- 완성 5분 집계 {q['aggregate_windows']}개와 원본 재합산 비교: 차이 0.01달러 초과 {q['aggregate_mismatches']}개.",
        f"- DB 1시간 합계 대비 상대오차 중앙값 {q['rolling1h_relative_error_p50']:.3%}, 95백분위 {q['rolling1h_relative_error_p95']:.3%}. 경계초 제외·기록 시차 등의 영향이 가능합니다.",
        '- 수집 코드가 사용하는 금액은 주문 가격(p 우선)×원주문 수량(q)입니다. 정확한 체결 청산 금액 또는 전시장 총액으로 해석하지 않습니다.',
        '- 로컬 수집 코드에는 1,000달러 미만 제외 설정이 있습니다. GCP 당시 코드와의 동일성은 확인하지 않았으며, 0은 전체 시장 청산 부재가 아니라 이 자료의 무기록을 뜻합니다.',
        '- Binance forceOrder는 제한된 청산 스냅샷 스트림입니다. 모든 청산 체결의 완전한 원장이 아닙니다.',
        '  출처: https://github.com/binance/binance-futures-connector-python/blob/main/binance/websocket/um_futures/websocket_client.py',
        '', '## 구간의 안정성','|구간|방향별 무발생 비율|매분 증가/보합/감소 상태 변경률|분당 금액 중앙값|95백분위|',
        '|---|---:|---:|---:|---:|']
    for w,c in r['coverage'].items():
        lines.append(f"|{w}분|{c['zero_pct']:.1f}%|{c['state_change_pct']:.1f}%|{c['speed_p50']:.0f}|{c['speed_p95']:.0f}|")
    lines+=['','무발생은 매매 실패가 아니라 해당 방향 청산 기록이 없는 구간입니다. 변경률이 낮다고 예측력이 높은 것은 아닙니다.',
            '## 실제 거래를 사후 선별한 결과',
            '이 표는 새로운 전략 백테스트가 아닙니다. 차단 이후 포지션과 다음 주문이 달라지는 효과, 대체 진입은 계산하지 않았습니다. 손익은 기존 거래의 추정 수수료 차감 값입니다.',
            '|검토 조건|구간|통과 거래|걸러낸 손실|놓친 수익|통과 거래 손익 USDT|','|---|---|---:|---:|---:|---:|']
    names={'increase':'직전보다 속도 증가','above_mean':'과거 평균보다 높은 속도','increase_above_mean':'증가 + 과거 평균 초과'}
    for name,bywindow in r['comparison'].items():
        for w,s in bywindow.items():
            lines.append(f"|{names[name]}|{w}분|{s['kept']}/{s['n']}|{s['loss_blocked']}|{s['win_blocked']}|{s['kept_net']:+.2f}|")
    lines+=['','## 거래별 직전 속도','속도는 달러/분. 괄호는 직전 같은 길이 구간 대비 변화량입니다.',
            '|진입 KST|방향|청산 점수|1분 속도(변화)|5분 속도(변화)|10분 속도(변화)|순손익 USDT|','|---|---|---:|---:|---:|---:|---:|']
    for t in r['trades']:
        fields=[f"{t['features'][w]['speed']:,.0f} ({t['features'][w]['acceleration']:+,.0f})" for w in ('1','5','10')]
        lines.append('|'+ '|'.join([t['entry'],t['side'],str(t['liq_score'])]+fields+[f"{t['net']:+.2f}"] )+'|')
    lines+=['','## 큰 손실의 구체적인 해석',
            '- 9/28 18:44 SHORT: 5분 속도는 직전 약 53,236달러/분에서 4,114달러/분으로 약 92% 감소했습니다. 하지만 최근 1분에는 단일 청산 사건이 생겨 증가로 보였고, 10분에도 앞선 큰 사건이 남아 증가로 보였습니다. 최종 손익 -15.08 USDT.',
            '- 9/30 22:31 LONG: 직전 5분 0에서 최근 5분 1,321달러/분으로 증가했지만, 과거 평균보다 작은 규모였습니다. 증가만 검사하면 통과하고, 평균 대비 크기도 보면 차단됩니다. 최종 손익 -14.53 USDT.',
            '- 9/28 09:21 LONG: 5분 청산 속도가 약 409,418달러/분으로 크게 증가했고 평균도 크게 초과했지만 -7.10 USDT 손실입니다. 신규 청산이 강해도 진입 방향의 가격 지속은 보장되지 않습니다.',
            '- 9/28 15:18 SHORT: 최근 1·5·10분 청산이 모두 0이어도 +1.79 USDT 수익이었습니다. 청산 증가를 모든 진입의 필수 조건으로 만들면 이런 거래도 잃습니다.',
            '- 위 해석은 당시 기록과 최종 결과의 연결입니다. 차단했을 때 이후 주문들이 어떻게 달라지는지는 별도 재생이 필요합니다.']
    lines+=['','## 민감도: 증가 + 평균 초과 조건',
            '|비교|구간|통과 거래|걸러낸 손실|놓친 수익|통과 거래 손익|','|---|---|---:|---:|---:|---:|']
    for name,groups in {**r['sensitivity'],**r['halves']}.items():
        label={'baseline_6h':'평균 기준 6시간','lag60':'정보 1분 지연','early':'앞쪽 거래 절반','late':'뒤쪽 거래 절반'}[name]
        for w,s in groups.items():
            lines.append(f"|{label}|{w}분|{s['kept']}/{s['n']}|{s['loss_blocked']}|{s['win_blocked']}|{s['kept_net']:+.2f}|")
    lines+=['','## 일반 진입과 방향 전환 분리',
            '반대 신호 청산 후 120초 이내 반대 방향 진입을 전환으로 분류했습니다. 증가+평균 초과 조건입니다.',
            '|유형|구간|통과|걸러낸 손실|놓친 수익|통과 거래 손익|','|---|---|---:|---:|---:|---:|']
    for kind,groups in r['by_kind'].items():
        for w,s in groups.items():
            lines.append(f"|{'방향 전환' if kind=='reversal' else '일반 진입'}|{w}분|{s['kept']}/{s['n']}|{s['loss_blocked']}|{s['win_blocked']}|{s['kept_net']:+.2f}|")
    lines+=['','## 표준화 점수와 단발 사건 영향',
            '5분 기준. 0 초과는 과거 평균 초과입니다. 정규분포를 가정한 확률이 아니며 큰 양수라도 수익을 보장하지 않습니다.',
            '|진입 KST|방향|속도 표준화|가속 표준화|최대 사건 비중|발생한 분 수/5|순손익|','|---|---|---:|---:|---:|---:|---:|']
    for t in r['trades']:
        f=t['features']['5']
        fmt=lambda v:'자료 없음' if v is None else f'{v:+.2f}'
        share='없음' if f['largest_share'] is None else f"{f['largest_share']:.1%}"
        lines.append(f"|{t['entry']}|{t['side']}|{fmt(f['speed_z'])}|{fmt(f['acceleration_z'])}|{share}|{f['active_minutes']}|{t['net']:+.2f}|")
    lines+=['','## 실제 매매와 별개인 관측: 최소 4시간 간격',
            '자기 방향 청산 6점·총점 10 이상·점수 차이 5 이상·RANGE 거래 금지 아님인 시점을 시간순으로 4시간 이상 띄워 선택했습니다. 모든 창에서 같은 시점을 사용합니다. 양수 요소 수 등 전체 매매 규칙을 재현한 것은 아닙니다.',
            '전략 버전+설정 해시별 분리. 가격 수익률은 방향 반영, 비용 전. 4시간 뒤 자료가 있는 공통 표본만 포함합니다.',
            '|설정 그룹|구간|증가+평균초과|표본|15분 수익률|1시간 수익률|4시간 수익률|','|---|---|---|---:|---:|---:|---:|']
    for g in r['grid']:
        f=lambda x:'자료 없음' if x is None else f'{x:+.3f}%'
        lines.append('|'+ '|'.join([g['group'],str(g['minutes']), '충족' if g['pass'] else '미충족',str(g['n'])]+[f(g['mean_returns'][str(h)]) for h in (15,60,240)])+'|')
    lines+=['','## 결론을 읽을 때',
            '- 이 짧은 기간의 결과로 1분·5분·10분 중 실거래 최적 구간을 확정할 수 없습니다.',
            '- 큰 청산이 최근 구간에 한 번 들어오면 속도와 가속이 동시에 높아집니다. 청산의 연속성·최대 한 사건의 비중도 JSON에 저장했습니다.',
            '- 원본 DB, 점수 계산, 진입·청산 조건, 주문 코드는 수정하지 않았습니다.',
            '- 속도가 약한 진입을 막았을 때 순이익이 개선되는지는 독립된 추가 기간과 전체 전략 재생이 필요합니다.']
    lines+=['','## 재현과 검증',
            '- 분석 코드: work/liquidation_speed_study.py. 세부 결과: work/liquidation_speed_results.json.',
            '- 경계 시간, 미래 데이터 배제, 속도 단위/0 처리, 과거 표본 수, 롱·숏 분리 단위 테스트 5개 통과.',
            '- 실제 거래 선별 전후 건수 및 손익 합계 일치 검증 통과. 원본 5분 집계 666개 대조 통과.',
            '- 운영 코드 변경이 없어 기존 매매 테스트 전체는 이번 분석에서 재실행하지 않았습니다.']
    (src.ROOT/'LIQUIDATION_SPEED_20261002.md').write_text('\n'.join(lines),encoding='utf-8')


class StudyTests(unittest.TestCase):
    def test_boundaries(self):
        s=Series([(0,10),(60,20),(120,30)])
        self.assertEqual(s.amount(60,120),20)
        self.assertEqual(s.amount(0,120),30)

    def test_no_future_leak(self):
        a=Series([(x,float(x%300+1)) for x in range(0,90000,60)])
        b=Series([(x,float(x%300+1)) for x in range(0,90000,60)]+[(90001,1e12)])
        self.assertEqual(a.features(90000,5,0),b.features(90000,5,0))

    def test_scale_and_zero(self):
        s=Series([(300,100),(360,200)])
        f=s.features(600,5,0)
        self.assertEqual(f['speed'],60)
        self.assertEqual(f['previous_speed'],0)
        self.assertIsNone(f['speed_z'])
        z=Series([]).features(90000,5,0)
        self.assertEqual(z['speed'],0)
        self.assertIsNone(z['speed_z'])

    def test_reference_count(self):
        s=Series([(x,1.) for x in range(0,100000,60)])
        self.assertEqual(s.features(99900,5,0)['reference_n'],288)
        self.assertEqual(s.features(99900,10,0)['reference_n'],144)

    def test_direction_is_separate(self):
        long=Series([(60,120)])
        short=Series([(60,40)])
        self.assertEqual(long.features(120,1,0)['speed'],120)
        self.assertEqual(short.features(120,1,0)['speed'],40)


if __name__=='__main__':
    main()
