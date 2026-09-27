"""Read-only event study. No strategy or live database changes."""
import bisect
from contextlib import closing
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics as st
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
DOWNLOADS = Path('C:/Users/chlgh/Downloads')
FIELDS = ['timestamp','symbol','price','long_score','short_score','strategy_version',
          'strategy_config_json','body_long_score','body_short_score','volume_long_score',
          'volume_short_score','liquidation_long_score','liquidation_short_score',
          'score_components_json','range_block_trade']

def read(path):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True)) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute('SELECT '+','.join(FIELDS)+" FROM market_state WHERE symbol='BTCUSDT' ORDER BY timestamp")]

def load():
    rows = {}
    sources = [DOWNLOADS/'btc_market_state_20260920_043441.db']
    for r in read(sources[0]):
        rows[r['timestamp']] = r
    for archive in sorted(DOWNLOADS.glob('btc_market_state_delta_*.zip')):
        with zipfile.ZipFile(archive) as z, tempfile.TemporaryDirectory() as tmp:
            raw = z.read('delta.db')
            manifest = json.loads(z.read('manifest.json'))
            assert hashlib.sha256(raw).hexdigest() == manifest['delta_sha256']
            path = Path(tmp)/'delta.db'
            path.write_bytes(raw)
            for r in read(path):
                rows[r['timestamp']] = r
        sources.append(archive)
    result = sorted(rows.values(), key=lambda r:r['timestamp'])
    for r in result:
        r['t'] = datetime.fromisoformat(r['timestamp']).timestamp()
        config = json.dumps(json.loads(r['strategy_config_json'] or '{}'), sort_keys=True)
        r['group'] = r['strategy_version']+':'+hashlib.sha256(config.encode()).hexdigest()[:8]
    return result, sources

def summarize(events):
    out = {'n':len(events)}
    if not events:
        return out
    for h in (15,60,240):
        returns = [e[f'r{h}'] for e in events]
        out[str(h)] = {'mean':st.mean(returns), 'median':st.median(returns),
                      'positive_pct':100*sum(v>0 for v in returns)/len(returns),
                      'over_0.1_pct':100*sum(v>0.1 for v in returns)/len(returns),
                      'score_change':st.mean(e[f'd{h}'] for e in events)}
    out['mfe'] = st.mean(e['mfe'] for e in events)
    out['mae'] = st.mean(e['mae'] for e in events)
    out['peak_score_gain'] = st.mean(e['peak_score_gain'] for e in events)
    out['score_rise_4h_pct'] = 100*sum(e['d240']>0 for e in events)/len(events)
    return out

def spaced(events):
    selected=[]
    for e in events:
        if not selected or e['t']-selected[-1]['t']>=14400:
            selected.append(e)
    return selected

def main():
    rows,sources=load()
    times=[r['t'] for r in rows]
    gaps=[0]
    for i in range(1,len(rows)):
        gaps.append(gaps[-1]+int(times[i]-times[i-1]>180 or rows[i]['group']!=rows[i-1]['group']))
    groups=Counter(r['group'] for r in rows)
    events=[]
    for i,r in enumerate(rows):
        past={m:bisect.bisect_right(times,r['t']-m*60)-1 for m in (15,30,60)}
        future={m:bisect.bisect_left(times,r['t']+m*60) for m in (15,60,240)}
        a,b=past[60],future[240]
        if a<0 or b>=len(rows) or gaps[b]!=gaps[a]:
            continue
        if any(r['t']-m*60-times[j]>120 for m,j in past.items()):
            continue
        if any(times[j]-r['t']-m*60>120 for m,j in future.items()):
            continue
        for side,other,sign in [('long','short',1),('short','long',-1)]:
            score=r[side+'_score']
            cross10=rows[i-1][side+'_score']<10<=score
            cross14=rows[i-1][side+'_score']<14<=score
            if not (9<=score<=11 or cross10 or cross14):
                continue
            change=score-rows[past[15]][side+'_score']
            components=json.loads(r['score_components_json'] or '{}')
            broad=sum((components.get(k) or {}).get(side+'_score',0)>0 for k in ['price_position','body','volume','trend_continuity','range','liquidation','activity_direction'])
            capped=sum((r[k+'_'+side+'_score'] or 0)>=5 for k in ['body','volume','liquidation'])
            path=[sign*(q['price']/r['price']-1)*100 for q in rows[i:b+1]]
            e={'t':r['t'],'timestamp':r['timestamp'],'group':r['group'],'side':side,
               'score':score,'gap':score-r[other+'_score'],'cross10':cross10,'cross14':cross14,
               'flow':'rising' if change>=2 else 'falling' if change<=-2 else 'flat',
               'breadth':broad,'capped':capped,'mfe':max(path),'mae':min(path),
               'peak_score_gain':max(q[side+'_score'] for q in rows[i:b+1])-score,
               'other_delta15':r[other+'_score']-rows[past[15]][other+'_score'],
               'blocked':bool(r['range_block_trade'])}
            for m,j in past.items():
                e[f'past{m}']=score-rows[j][side+'_score']
            for m,j in future.items():
                e[f'r{m}']=sign*(rows[j]['price']/r['price']-1)*100
                e[f'd{m}']=rows[j][side+'_score']-score
            events.append(e)
    summaries={}
    for group in groups:
        ge=[e for e in events if e['group']==group]
        report={}
        for side in ('long','short'):
            se=[e for e in ge if e['side']==side]
            cohorts={'cross10':[e for e in se if e['cross10']],
                     'cross10_gap5':[e for e in se if e['cross10'] and e['gap']>=5 and not e['blocked']],
                     'cross14_gap5':[e for e in se if e['cross14'] and e['gap']>=5 and not e['blocked']]}
            for flow in ('rising','flat','falling'):
                cohorts['near10_'+flow]=[e for e in se if 9<=e['score']<=11 and e['flow']==flow]
            cross=cohorts['cross10_gap5']
            cohorts['cross10_narrow']=[e for e in cross if e['breadth']<=2]
            cohorts['cross10_broad']=[e for e in cross if e['breadth']>=3]
            cohorts['cross10_saturated']=[e for e in cross if e['capped']>=2]
            cohorts['cross10_not_saturated']=[e for e in cross if e['capped']<2]
            cohorts['cross10_other_rising']=[e for e in cross if e['other_delta15']>=2]
            cohorts['cross10_other_not_rising']=[e for e in cross if e['other_delta15']<2]
            midpoint=(min(r['t'] for r in rows if r['group']==group)+max(r['t'] for r in rows if r['group']==group))/2
            report[side]={name:{'all':summarize(es),'nonoverlap':summarize(spaced(es)),
                               'early':summarize(spaced([e for e in es if e['t']<midpoint])),
                               'late':summarize(spaced([e for e in es if e['t']>=midpoint]))} for name,es in cohorts.items()}
        summaries[group]=report
    output={'sources':[str(p) for p in sources], 'rows':len(rows),'start':rows[0]['timestamp'],
            'end':rows[-1]['timestamp'],'groups':dict(groups),'summaries':summaries}
    (ROOT/'entry_paths_results.json').write_text(json.dumps(output,indent=2),encoding='utf-8')
    latest=rows[-1]['group']
    names={'cross10':'10점 상향 돌파','cross10_gap5':'10점 돌파·점수차 5 이상',
           'cross14_gap5':'14점 돌파·점수차 5 이상','near10_rising':'9~11점·상승 중',
           'near10_flat':'9~11점·정체','near10_falling':'9~11점·하락 중',
           'cross10_narrow':'10점 돌파·양수 항목 1~2개','cross10_broad':'10점 돌파·양수 항목 3개 이상',
           'cross10_saturated':'10점 돌파·고배점 집중','cross10_not_saturated':'10점 돌파·고배점 집중 아님',
           'cross10_other_rising':'10점 돌파·반대 점수 상승','cross10_other_not_rising':'10점 돌파·반대 점수 상승 아님'}
    sides={'long':'롱','short':'숏'}
    lines=['# 진입 점수에 도달하는 과정과 이후 가격 분석', '',
           f"자료: {output['start']} ~ {output['end']} (UTC). 중복 제외 {len(rows):,}개 관측값.",
           f'최신 설정 식별값: {latest}. 해당 관측값 {groups[latest]:,}개.', '',
           '## 읽는 방법과 주의점',
           '- 원본 DB는 수정하지 않았고 압축파일의 검증값을 확인했습니다. 같은 시각은 하나로 합쳤습니다.',
           '- 전략 버전과 저장된 전체 설정을 함께 분류했습니다. 설정이 같아도 기록되지 않은 코드 변경은 구분할 수 없습니다.',
           '- 상향 돌파: 직전 관측은 기준 미만, 현재는 기준 이상입니다. 재돌파도 포함됩니다.',
           '- 점수차 5 이상: 해당 방향 점수가 반대보다 5 이상 높고 RANGE 매매 금지가 없는 경우입니다. 실제 재진입 제한까지 재현한 것은 아닙니다.',
           '- 9~11점 구간의 상승/하락: 직전 15분 대비 +2 이상/-2 이하. 나머지는 정체입니다.',
           '- 양수 항목 수에는 가격 위치·몸통·거래량·추세·RANGE 순점수·청산·활동 방향을 셉니다. 서로 독립된 증거 수는 아닙니다.',
           '- 고배점 집중: 몸통·거래량·청산 중 2개 이상이 5점 이상인 임시 분류입니다. 더 오를 수 없다는 뜻이 아닙니다.',
           '- 각 집단과 방향 안에서 최소 4시간 간격으로 선택했습니다. 집단끼리는 겹칠 수 있으므로 건수를 합산하지 마세요.',
           '- 직전 1시간부터 이후 4시간까지 설정이 같고 3분 초과 공백이 없는 자료만 사용했습니다. 비교 시각 오차는 최대 2분입니다.',
           '- 수익률은 롱/숏 방향을 반영한 가격 변화입니다. 레버리지·수수료·펀딩·슬리피지를 제외했으며 실제 봇 수익률이 아닙니다.',
           '- 최대 유리/불리 움직임은 진입 시점부터 4시간 동안 저장된 가격 기준입니다. 분 사이의 고점·저점은 알 수 없습니다.',
           '- 앞/뒤 기간 비교는 사후 점검이며 독립된 미래 검증이나 인과관계 증명이 아닙니다.',
           '- 미래 자료가 부족한 경우는 제외했습니다. OI 신규 정기 수집 자료는 이 파일에 없습니다.', '',
           '## 최신 설정 결과: 집단별 4시간 간격 표본', '',
           '|방향·조건|건수|15분 평균 %|1시간 평균 %|4시간 평균 %|4시간 양수 비율 %|최대 유리 평균 %|최대 불리 평균 %|4시간 점수 변화|',
           '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for side,cohorts in summaries[latest].items():
        for name,modes in cohorts.items():
            s=modes['nonoverlap']
            if s['n']:
                lines.append(f"|{sides[side]} {names[name]}|{s['n']}|{s['15']['mean']:.3f}|{s['60']['mean']:.3f}|{s['240']['mean']:.3f}|{s['240']['positive_pct']:.1f}|{s['mfe']:.3f}|{s['mae']:.3f}|{s['240']['score_change']:.2f}|")
    lines += ['', '## 앞 기간과 뒤 기간 비교', '', '|방향·조건|앞 기간 건수|앞 기간 4시간 평균 %|뒤 기간 건수|뒤 기간 4시간 평균 %|','|---|---:|---:|---:|---:|']
    for side,cohorts in summaries[latest].items():
        for name,modes in cohorts.items():
            a,b=modes['early'],modes['late']
            if a['n'] and b['n']:
                lines.append(f"|{sides[side]} {names[name]}|{a['n']}|{a['240']['mean']:.3f}|{b['n']}|{b['240']['mean']:.3f}|")
    lines += ['', '전체 설정별 수치와 간격 제한 전 결과는 entry_paths_results.json에 보존했습니다. 매매 코드는 바꾸지 않았습니다.']
    (ROOT/'ENTRY_PATHS_20260927.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({k:v for k,v in output.items() if k!='summaries'},indent=2))
    print('LATEST',latest)
    for side,cohorts in summaries[latest].items():
        print(side)
        for name,modes in cohorts.items():
            s=modes['nonoverlap']
            if s['n']:
                print(name,s['n'],'4h mean',round(s['240']['mean'],3),'win',round(s['240']['positive_pct'],1),'MFE',round(s['mfe'],3),'MAE',round(s['mae'],3),'score',round(s['240']['score_change'],2))

if __name__=='__main__':
    main()
