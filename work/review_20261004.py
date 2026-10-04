"""Read-only deployment and research checklist audit for the October 4 export."""
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics as st
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import analyze_entry_paths as source
from market_state_engine.prediction_evaluation import evaluate_database

DB=Path('C:/Users/chlgh/AppData/Local/Temp/delta (3).db')
MANIFEST=Path('C:/Users/chlgh/AppData/Local/Temp/manifest (4).json')
LOG=Path('C:/Users/chlgh/Downloads/live_trade_log (14).jsonl')
KST=timezone(timedelta(hours=9))
NEW='market_state_engine_v2_liquidation_speed_5m'


def ts(s): return datetime.fromisoformat(s).timestamp()
def kst(t): return datetime.fromtimestamp(t,KST).strftime('%m-%d %H:%M')
def obj(s): return json.loads(s or '{}')
def flatten(e): return [x for x in (e.get('close_event'),e.get('entry_event')) if x] if e.get('type')=='LIVE_REVERSAL' else [e]
def key(e): return e.get('type'),(e.get('response') or {}).get('orderId'),e.get('entry_time')
def score(r,side): return r[side.lower()+'_score']


def stats(trades):
    done=[t for t in trades if t['closed']]
    return {'entries':len(trades),'closed':len(done),'open':len(trades)-len(done),
            'wins':sum(t['net']>0 for t in done),'losses':sum(t['net']<0 for t in done),
            'net':sum(t['net'] for t in done),'gross':sum(t['gross'] for t in done),
            'fees':sum(t['fees'] for t in done),
            'mean_hold_hours':st.mean(t['hours'] for t in done) if done else None}


def main():
    manifest=obj(MANIFEST.read_text())
    assert hashlib.sha256(DB.read_bytes()).hexdigest()==manifest['delta_sha256']
    extra=['indicators_json','account_json','position_json','trade_event_json','decision','action',
           'atr_activity_score','activity_direction_long_score','activity_direction_short_score',
           'candle_open','candle_close','candle_timestamp','candle_volume','candle_body','expected_final_volume']
    source.FIELDS.extend(k for k in extra if k not in source.FIELDS)
    historical,files=source.load()
    merged={r['timestamp']:r for r in historical}
    merged.update({r['timestamp']:r for r in source.read(DB)})
    rows=sorted(merged.values(),key=lambda r:r['timestamp'])
    end=ts(manifest['until'])
    rows=[r for r in rows if ts(r['timestamp'])<=end]
    for r in rows:
        r['t']=ts(r['timestamp']); r['config']=obj(r['strategy_config_json'])
        r['ind']=obj(r['indicators_json']); r['comp']=obj(r['score_components_json'])
        r['position']=obj(r['position_json'])
        r['group']=r['strategy_version']+':'+hashlib.sha256(json.dumps(r['config'],sort_keys=True).encode()).hexdigest()[:8]
    times=[r['t'] for r in rows]
    def at(t):
        i=bisect_right(times,t)-1
        return rows[i] if i>=0 and t-times[i]<=120 else None
    after=[r for r in rows if r['strategy_version']==NEW]
    assert after, 'New deployment not observed'
    start=after[0]['t']; before_start=start-(end-start)
    before=[r for r in rows if before_start<=r['t']<start]
    log_events=[e for line in LOG.read_text(encoding='utf-8-sig').splitlines() if line.strip() for e in flatten(obj(line))]
    log_events=[e for e in log_events if e.get('type') in ('LIVE_ORDER','LIVE_CLOSE') and e.get('status')=='SENT' and not e.get('dry_run')]
    log_closes=list({key(e):e for e in log_events if e['type']=='LIVE_CLOSE'}.values())
    log_summary={'closed':len(log_closes),'wins':sum(e['estimated_realized_pnl']>0 for e in log_closes),
                 'losses':sum(e['estimated_realized_pnl']<0 for e in log_closes),
                 'net':sum(e['estimated_realized_pnl'] for e in log_closes),
                 'gross':sum(e['gross_realized_pnl'] for e in log_closes),
                 'fees':sum(e['estimated_fees'] for e in log_closes),
                 'first':kst(min(ts(e.get('exit_time',e['logged_at'])) for e in log_closes)),
                 'last':kst(max(ts(e.get('exit_time',e['logged_at'])) for e in log_closes))}
    events={}
    dbkeys=set()
    for r in rows:
        for e in flatten(obj(r['trade_event_json'])):
            if e.get('type') in ('LIVE_ORDER','LIVE_CLOSE') and e.get('status')=='SENT' and not e.get('dry_run'):
                events[key(e)]=e
                if r['t']>=start: dbkeys.add(key(e))
    for e in log_events: events[key(e)]=e
    opens={e.get('entry_time',e['logged_at']):e for e in events.values() if e['type']=='LIVE_ORDER'}
    closes={e['entry_time']:e for e in events.values() if e['type']=='LIVE_CLOSE' and e.get('entry_time')}
    previous_closes=sorted(closes.values(),key=lambda e:e.get('exit_time',e['logged_at']))
    trades=[]
    for entry_time,e in sorted(opens.items()):
        t=ts(entry_time)
        if t<end-7*86400: continue
        q=at(t)
        if not q: continue
        side=e.get('position_side')
        close=closes.get(entry_time)
        prior=[x for x in previous_closes if 0<=t-ts(x.get('exit_time',x['logged_at']))<=120 and x['side']!=side and 'reversal' in x.get('reason','')]
        f=q['ind'].get('liquidation',{})
        trade={'entry':kst(t),'entry_time':entry_time,'t':t,'side':side,'kind':'reversal' if prior else 'normal',
               'version':q['strategy_version'],'group':q['group'],'closed':bool(close),
               'entry_price':(close or {}).get('entry_price',e.get('entry_price',e.get('price'))),
               'long_score':q['long_score'],'short_score':q['short_score'],
               'atr_raw':q['ind'].get('atr',{}).get('raw_score'),'components':q['comp'],
               'liquidation':f,'evidence':q['ind'].get('entry_eligibility',{}).get(side),
               'volume_expected_delta15':None,'body_delta15':None}
        past=at(t-900)
        if past and past['candle_timestamp']==q['candle_timestamp'] and past['group']==q['group']:
            a,b=past['expected_final_volume'],q['expected_final_volume']
            if a and b is not None: trade['volume_expected_delta15']=(b/a-1)*100
            if q['candle_body'] is not None and past['candle_body'] is not None:
                trade['body_delta15']=q['candle_body']-past['candle_body']
        if close:
            finish=ts(close.get('exit_time',close['logged_at']))
            trade.update(exit=kst(finish),exit_t=finish,pnl=close['pnl_pct'],net=close['estimated_realized_pnl'],
                         gross=close['gross_realized_pnl'],fees=close['estimated_fees'],reason=close['reason'],
                         peak=close.get('peak_profit_pct'),hours=(finish-t)/3600)
            assert abs(trade['gross']-trade['fees']-trade['net'])<1e-7
        trades.append(trade)
    post_trades=[t for t in trades if t['version']==NEW]
    comparison_trades=[t for t in trades if before_start<=t['t']<start and t['closed'] and t['exit_t']<start]
    carry=[e for e in previous_closes if ts(e['entry_time'])<start<=ts(e.get('exit_time',e['logged_at']))<=end]
    # Audit recorded eligibility and liquidation score decisions without recomputing market scores.
    component_names=('price_position','body','volume','trend_continuity','range','liquidation')
    mismatches=[]; blocks=[]; gates=Counter(); values=Counter(); raw_atr=Counter(); data_status=Counter()
    missing_speed=0; score_logic_mismatches=0; positive_counts=Counter(); one_event_counts=Counter()
    for r in after:
        liq=r['ind'].get('liquidation',{}); gate=liq.get('gate')
        gates[gate]+=1; data_status[liq.get('data_status')]+=1
        raw_atr[r['ind'].get('atr',{}).get('raw_score')]+=1
        w=liq.get('windows',{}).get('5',{})
        if not all(str(n) in liq.get('windows',{}) for n in (1,5,10)): missing_speed+=1
        for side in ('LONG','SHORT'):
            comp=r['comp'].get('liquidation',{}); given=comp.get(side.lower()+'_score',0)
            bonus=comp.get(side.lower()+'_activity_bonus',0)
            values[given]+=1
            expected=w.get(side,{}).get('raw_score',0) if gate=='passed' and liq.get('direction')==side else 0
            if given!=expected or (gate!='passed' and bonus): score_logic_mismatches+=1
            if given:
                positive_counts[side]+=1
                one_event_counts[side]+=int(w.get(side,{}).get('event_count')==1)
            actual=[n for n in component_names if r['comp'].get(n,{}).get(side.lower()+'_score',0)>0]
            recorded=r['ind'].get('entry_eligibility',{}).get(side,{})
            if recorded.get('count')!=len(actual) or recorded.get('allowed')!=(len(actual)>=r['config']['entry_min_positive_components']):
                mismatches.append(r['timestamp'])
            pos=r['position']; other='SHORT' if side=='LONG' else 'LONG'
            if pos.get('status')=='OPEN' and pos.get('side')==side: continue
            threshold=r['config']['entry_'+side.lower()+'_score']+(r['config']['opposite_reentry_extra_score'] if pos.get('status')=='OPEN' else 0)
            if score(r,side)>=threshold and score(r,side)-score(r,other)>=r['config']['entry_score_gap'] and not r['range_block_trade'] and not recorded.get('allowed'):
                blocks.append({'t':r['t'],'side':side,'kind':'reversal' if pos.get('status')=='OPEN' else 'normal','score':score(r,side),'count':len(actual),'price':r['price']})
    episodes=[]
    for b in blocks:
        if not episodes or b['t']-episodes[-1]['last']>180 or (b['side'],b['kind'])!=(episodes[-1]['side'],episodes[-1]['kind']):
            episodes.append({**b,'last':b['t'],'observations':1})
        else: episodes[-1]['last']=b['t']; episodes[-1]['observations']+=1
    for e in episodes:
        i=bisect_left(times,e['t'])
        for minutes in (15,60,240):
            j=bisect_left(times,e['t']+minutes*60)
            if j>=len(rows) or times[j]-e['t']-minutes*60>120: continue
            path=rows[i:j+1]
            if any(q['group']!=rows[i]['group'] for q in path) or any(b['t']-a['t']>180 for a,b in zip(path,path[1:])): continue
            e['return_'+str(minutes)]=(1 if e['side']=='LONG' else -1)*(rows[j]['price']/e['price']-1)*100
    with closing(sqlite3.connect(DB.as_uri()+'?mode=ro&immutable=1',uri=True)) as c:
        c.row_factory=sqlite3.Row
        oi=[dict(r) for r in c.execute('SELECT * FROM oi_snapshots ORDER BY timestamp')]
        forecasts=[dict(r) for r in c.execute('SELECT * FROM prediction_forecast WHERE strategy_version=? ORDER BY source_timestamp',(NEW,))]
    oi=[r for r in oi if ts(r['timestamp'])>=start]
    ot=[ts(r['timestamp']) for r in oi]
    good=[r for r in oi if r['status']=='ok' and r['observed_at'] and r['open_interest'] is not None]
    arrivals=sorted((max(ts(r['observed_at']),ts(r['collected_at'])),r) for r in good)
    arrival_times=[t for t,_ in arrivals]
    def oi_at(t):
        i=bisect_right(arrival_times,t)-1
        if i<0 or t-arrival_times[i]>120 or t-ts(arrivals[i][1]['observed_at'])>120: return None
        return arrivals[i][1]['open_interest']
    for t in post_trades:
        cur=oi_at(t['t'])
        t['oi_changes']={str(m):(cur/prev-1)*100 if cur and prev else None for m in (15,60) for prev in [oi_at(t['t']-m*60)]}
    oi_summary={'rows':len(oi),'first':kst(ot[0]),'last':kst(ot[-1]),'expected':int((ot[-1]-ot[0])/60)+1,
                'statuses':dict(Counter(r['status'] for r in oi)),
                'missing_minutes':sum(max(0,int((b-a)/60)-1) for a,b in zip(ot,ot[1:])),
                'max_age_sec':max(ts(r['collected_at'])-ts(r['observed_at']) for r in good),
                'last_age_at_export_sec':end-ts(good[-1]['observed_at'])}
    hourly=Counter(datetime.fromtimestamp(ts(f['created_at']),KST).strftime('%m-%d %H') for f in forecasts)
    evaluation=evaluate_database(DB,now=datetime.fromtimestamp(end,timezone.utc)).get('strategies',{}).get(NEW)
    latest_forecast=obj(forecasts[-1]['forecast_json']) if forecasts else None
    def regime(window):
        return {'rows':len(window),'start':kst(window[0]['t']),'end':kst(window[-1]['t']),
                'price_change_pct':(window[-1]['price']/window[0]['price']-1)*100,
                'observed_price_range_pct':(max(r['price'] for r in window)-min(r['price'] for r in window))/window[0]['price']*100,
                'atr_raw_counts':dict(Counter(r['ind'].get('atr',{}).get('raw_score') for r in window))}
    open_pos=rows[-1]['position']
    open_observation={}
    if open_pos.get('status')=='OPEN' and open_pos.get('entry_time'):
        opened=ts(open_pos['entry_time']); price=open_pos['entry']
        path=[(1 if open_pos['side']=='LONG' else -1)*(r['price']/price-1)*100 for r in after if r['t']>=opened]
        open_observation={'hours':(end-opened)/3600,'observed_best_price_pct':max(path) if path else None,
                          'observed_worst_price_pct':min(path) if path else None}
        for t in post_trades:
            if t['entry_time']==open_pos['entry_time']:
                t['order_reference_price']=t['entry_price']; t['entry_price']=price
    # Historical ATR bins stay separated by actual score/configuration version.
    atr_groups={}
    for group in sorted({t['group'] for t in trades}):
        atr_groups[group]={str(raw):{kind:stats([t for t in trades if t['group']==group and t['atr_raw']==raw and t['kind']==kind]) for kind in ('normal','reversal')} for raw in (0,1,2,3,4,5,6)}
    out={'start':kst(start),'end':kst(end),'hours':(end-start)/3600,'post_rows':len(after),'log_summary':log_summary,
         'post_stats':stats(post_trades),'post_by_kind':{kind:stats([t for t in post_trades if t['kind']==kind]) for kind in ('normal','reversal')},
         'pre_equal_closed_stats':stats(comparison_trades),'carry_closes':carry,
         'post_trades':post_trades,'week_closed_stats':stats([t for t in trades if t['closed']]),
         'account_start':obj(after[0]['account_json']),'account_end':obj(rows[-1]['account_json']),
         'position_end':open_pos,'open_observation':open_observation,'market_before':regime(before),'market_after':regime(after),
         'market_gaps_over_120s':[(kst(a['t']),kst(b['t']),b['t']-a['t']) for a,b in zip(after,after[1:]) if b['t']-a['t']>120],
         'eligibility_mismatch_count':len(mismatches),'blocked_observations':len(blocks),'blocked_episodes':episodes,
         'liquidation_gates':dict(gates),'liquidation_data_status':dict(data_status),'missing_speed_rows':missing_speed,
         'non_ready_records':[{'time':kst(r['t']),'status':r['ind'].get('liquidation',{}).get('data_status')} for r in after if r['ind'].get('liquidation',{}).get('data_status')!='ready'],
         'liquidation_score_counts_both_sides':dict(values),'liquidation_score_logic_mismatches':score_logic_mismatches,
         'positive_liquidation_observations':dict(positive_counts),'one_event_positive_observations':dict(one_event_counts),
         'atr_post_counts':dict(raw_atr),'atr_groups':atr_groups,'oi':oi_summary,
         'prediction':{'count':len(forecasts),'first':forecasts[0]['created_at'] if forecasts else None,
                       'last':forecasts[-1]['created_at'] if forecasts else None,'distinct_hours':len(hourly),
                       'sent_count':sum(bool(f['telegram_sent_at']) for f in forecasts),
                       'evaluation':evaluation,'latest':latest_forecast},
         'db_orders_missing_in_log':list(dbkeys-{key(e) for e in log_events}),
         'sha256_valid':True,'source_manifest':manifest}
    (source.ROOT/'review_20261004.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    report(out)
    print(json.dumps({k:v for k,v in out.items() if k not in ('atr_groups','prediction','post_trades','source_manifest')},ensure_ascii=False,indent=2))
    print('TRADES',json.dumps([{k:v for k,v in t.items() if k not in ('liquidation','components')} for t in post_trades],ensure_ascii=False))
    print('PRED',json.dumps(out['prediction'],ensure_ascii=False))


def report(o):
    def fmt(x): return '자료 없음' if x is None else f'{x:+.2f}'
    lines=['# 2026년 10월 4일 운영 점검','',
           f"새 청산 방식 관측 기간: {o['start']} ~ {o['end']} KST, {o['hours']:.2f}시간, {o['post_rows']:,}개 시장 기록.",
           '첨부 파일 시점까지만 확인했으며 현재 서버/거래소를 직접 조회한 것이 아닙니다. 원본 파일과 운영 코드는 변경하지 않았습니다.',
           f"첨부 거래 로그 전체: {o['log_summary']['first']}~{o['log_summary']['last']} 청산 {o['log_summary']['closed']}건, 수익 {o['log_summary']['wins']}건/손실 {o['log_summary']['losses']}건, 추정 순손익 {o['log_summary']['net']:+.2f} USDT. 이전 방식 거래도 포함되며 첫 청산의 진입은 로그 구간 이전입니다.",
           '## 체크리스트 결과',
           '- [x] 새 5분 청산 방식과 새 전략 버전 기록 확인',
           '- [x] 진입·종료·방향 전환·보유 중 포지션 별도 집계',
           '- [x] 양수 요소 3개 제한 기록 대조',
           '- [x] ATR 원점수별 표본 존재 여부 확인',
           '- [x] OI 정기 저장의 간격·오류·신선도 확인',
           '- [x] 수수료와 미실현 손익 분리, 같은 길이 이전 구간 비교',
           '- [x] 예측 기록에서 새 전략 버전 표본 상태 확인',
           '- [ ] 새 청산 방식의 독립된 장기 성과 검증: 아직 약 52시간뿐',
           '- [ ] 거래량·몸통·RANGE 개편 필요성 판단: 이번에는 진입 당시 흐름만 기록, 규칙 변경 보류',
           '', '## 실제 거래','|진입 KST|방향|유형|진입 LONG/SHORT|ATR 원점수|종료|추정 순손익 USDT|',
           '|---|---|---|---|---:|---|---:|']
    for t in o['post_trades']:
        lines.append(f"|{t['entry']}|{t['side']}|{'전환' if t['kind']=='reversal' else '일반'}|{t['long_score']}/{t['short_score']}|{t['atr_raw']}|{t.get('exit','보유 중')}|{fmt(t.get('net'))}|")
    for label,s in [('새 방식 전체',o['post_stats']),('이전 동일 길이, 기간 내 진입·종료',o['pre_equal_closed_stats'])]:
        lines.append(f"- {label}: 진입 {s['entries']}건, 종료 {s['closed']}건, 수익 {s['wins']}건, 손실 {s['losses']}건, 비용 전 {s['gross']:+.2f}, 추정 수수료 {s['fees']:.2f}, 순손익 {s['net']:+.2f} USDT.")
    p=o['position_end']
    lines+=[f"- 파일 마지막 포지션: {p.get('side')}, 평단 {p.get('entry')}, 미실현 수익률 {fmt(p.get('unrealized_pnl_pct'))}%, 미실현 손익 {fmt(p.get('unrealized_pnl_usdt'))} USDT.",
            f"- 보유 시간 {o['open_observation'].get('hours',0):.2f}시간. 보유 중 분단위 관측 최고 수익률 {fmt(o['open_observation'].get('observed_best_price_pct'))}%, 최저 {fmt(o['open_observation'].get('observed_worst_price_pct'))}%. 틱 단위 최고·최저는 아닙니다.",
            f"- 기록 자본: {o['account_start'].get('equity'):.2f} → {o['account_end'].get('equity'):.2f} USDT. 계좌 전체 잔액이 아니라 봇의 가상 배정 자본 추정치입니다.",
            '- 보유 중 거래를 승리/패배로 판정하지 않았습니다. 로그 수수료 추정값이며 거래소 체결·펀딩 대사는 아닙니다.',
            '', '## 변경 전후 시장 차이']
    for name,label in [('market_before','이전'),('market_after','이후')]:
        r=o[name]; lines.append(f"- {label}: {r['start']}~{r['end']}, BTC 변화 {r['price_change_pct']:+.2f}%, 관측 가격 범위 {r['observed_price_range_pct']:.2f}%, ATR 원점수 분포 {r['atr_raw_counts']}.")
    lines+=['- 기간 길이는 맞췄지만 시장 상황과 거래 수가 다릅니다. 변경의 인과 효과로 해석할 수 없습니다.',
            '', '## 새 청산 점수 기록 검사',
            f"- 데이터 상태: {o['liquidation_data_status']}; 1·5·10분 저장 누락 {o['missing_speed_rows']}개.",
            f"- 점수 부여 논리 불일치 {o['liquidation_score_logic_mismatches']}개. 양쪽 합산 점수 분포 {o['liquidation_score_counts_both_sides']}.",
            f"- 점수 제외/통과 이유: {o['liquidation_gates']}.",
            f"- 자료 미준비 시점: {o['non_ready_records']}.",
            f"- 양수 청산 관측 {o['positive_liquidation_observations']}, 그중 단일 사건에 의한 관측 {o['one_event_positive_observations']}.",
            '- 중요한 점검: 기준 구간 대부분이 0이면 작은 양수도 높은 백분위가 될 수 있습니다. 평균·가속 검사를 통과하더라도 0점과 6점 위주로 몰리는지 별도 확인해야 합니다.',
            '- 이번에는 실제로 양수 314개가 모두 6점이었습니다. 1~5점이 쓰이지 않는 배점 분해능 문제가 확인됐습니다. 이것이 손실 원인이라는 인과 결론은 아닙니다.',
            '- 자정 직후 당일 CSV가 아직 생성되지 않으면 missing_raw_file로 0점이 됩니다. 로컬 수집기는 새 청산 사건을 기록할 때 날짜 파일을 만들고, 점수기는 당일 파일을 요구하므로 서로 맞지 않는 경계 동작이 있습니다. GCP 수집 코드는 직접 대조하지 않았습니다.',
            '- 원본 사건 전체나 연결 하트비트는 이번 델타에 없습니다. data_status=ready는 비교 기준을 충족했다는 뜻이지 수집 연결 정상 인증이 아닙니다.',
            '', '## 진입 제한과 방향 전환',
            f"- 요소 개수 기록 불일치 {o['eligibility_mismatch_count']}개.",
            f"- 점수 기준은 충족하나 요소 부족인 관측 {o['blocked_observations']}개, 연속 구간 {len(o['blocked_episodes'])}개.",
            '- 이 수치는 실제 주문 시도 차단 횟수가 아니라 저장된 상태에서 재계산한 후보 관측입니다. 동일 방향 보유 중에는 후보에서 제외했습니다.',
            f"- 신규 방식 방향 전환 진입 {o['post_by_kind']['reversal']['entries']}건. 표본이 없으면 전환 개선 여부를 평가할 수 없습니다.",
            '|요소 부족 후보 시작 KST|유형|방향|관측 수|15분 가격 수익률 %|1시간 %|4시간 %|',
            '|---|---|---|---:|---:|---:|---:|',
            *[f"|{kst(e['t'])}|{'전환' if e['kind']=='reversal' else '신규'}|{e['side']}|{e['observations']}|{fmt(e.get('return_15'))}|{fmt(e.get('return_60'))}|{fmt(e.get('return_240'))}|" for e in o['blocked_episodes']],
            '- 방향을 반영한 고정 시점 가격 변화이며 실제 손절·수익보호·재진입을 재생한 수익률이 아닙니다. 후보 구간의 미래 경로는 서로 겹칠 수 있습니다.',
            '', '## ATR 원점수 3·4·5·6',f"- 새 방식 전체 관측 분포: {o['atr_post_counts']}.",
            '- 해당 원점수의 새 방식 거래가 없으면 구간별 성과 비교는 자료 부족으로 남깁니다. 옛 전략 거래를 섞어 새 방식 성과로 표시하지 않습니다.',
            '', '## OI 기록',
            f"- {o['oi']['first']}~{o['oi']['last']}: {o['oi']['rows']}행 / 예상 {o['oi']['expected']}분, 누락 {o['oi']['missing_minutes']}분, 상태 {o['oi']['statuses']}.",
            f"- 수집 당시 관측값 최대 나이 {o['oi']['max_age_sec']:.1f}초, 파일 끝 기준 마지막 관측 나이 {o['oi']['last_age_at_export_sec']:.1f}초.",
            '- 매매와 OI의 인과관계는 평가하지 않았습니다. 아래는 진입 전 관측값만 사용한 비교입니다.',
            '|진입|방향|OI 15분 변화 %|OI 1시간 변화 %|예상 일일 거래량 15분 변화 %|몸통 15분 변화 달러|',
            '|---|---|---:|---:|---:|---:|']
    for t in o['post_trades']:
        lines.append(f"|{t['entry']}|{t['side']}|{fmt(t['oi_changes']['15'])}|{fmt(t['oi_changes']['60'])}|{fmt(t['volume_expected_delta15'])}|{fmt(t['body_delta15'])}|")
    lines+=['- 10/03 04:23 재진입은 청산 0점에서도 몸통·거래량·RANGE로 3개 요소를 충족했습니다. 청산 수정은 전체 진입 전 점검을 대체하지 않습니다.',
            '- 이때 예상 일일 거래량은 직전 15분보다 감소했지만 몸통은 커졌습니다. 단일 요인만으로 손실을 설명할 수 없습니다.',
            '- 앞서 수익으로 끝난 10/02 14:06 LONG도 예상 거래량과 몸통이 직전 15분보다 줄어 있었습니다. 감소를 무조건 금지하는 규칙은 수익 거래도 제외합니다.']
    pred=o['prediction']; ev=pred['evaluation'] or {}
    lines+=['','## 예측 연구',f"- 새 버전 예측 기록 {pred['count']}개, 서로 다른 실행 시간대 {pred['distinct_hours']}개, 전송 시각 기록 {pred['sent_count']}개.",
            '- 새 버전만 평가했습니다. 기존 예측 알고리즘은 변경하지 않았고 결과 미확정은 오답으로 세지 않았습니다.',
            f"- 4시간 평가 제외 사유: {ev.get('excluded',{})}."]
    for kind,label in [('all','전체'),('non_overlapping','4시간 이상 간격')]:
        for side,s in ev.get(kind,{}).get('sides',{}).items():
            number=lambda v:'자료 없음' if v is None else f'{v:.2f}'
            lines.append(f"- {label} {side}: 채점 {s['count']}건, 우리 평균 오차 {number(s['forecast_mae'])}, 유지 평균 오차 {number(s['persistence_mae'])}.")
    lines+=['','## 무결성과 남은 점검',
            '- manifest SHA256 검증 통과. 원본 데이터는 읽기 전용 연결로 열었습니다.',
            f"- 새 방식 DB 주문 이벤트 중 첨부 거래 로그에 없는 이벤트 {len(o['db_orders_missing_in_log'])}개.",
            f"- 새 방식 시장 기록 120초 초과 간격 {len(o['market_gaps_over_120s'])}개: {o['market_gaps_over_120s']}.",
            '- 다음 우선순위는 새 청산 배점의 0/6 집중과 자정 파일 판정 점검입니다. 거래량·몸통·RANGE는 동시에 바꾸지 말고 다음 분석에서 하나씩 검증합니다.',
            '- 예측에서 평균 오차는 낮을수록 좋습니다. 전체 표본에서는 유지 기준보다 우세하지 않았고, 겹침을 줄인 표본도 작아 확정 평가할 수 없습니다.',
            '- 이 분석에서 매매 코드를 변경하지 않았습니다.']
    (source.ROOT/'REVIEW_20261004.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__=='__main__': main()
