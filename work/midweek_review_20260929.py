"""Read-only audit of supplied research export and trade events."""
import bisect
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import statistics as st

import analyze_entry_paths as source
import analyze_activity as activity

DB = Path('C:/Users/chlgh/AppData/Local/Temp/delta (1).db')
MANIFEST = Path('C:/Users/chlgh/AppData/Local/Temp/manifest (2).json')
LOG = Path('C:/Users/chlgh/Downloads/live_trade_log (12).jsonl')
KST = timezone(timedelta(hours=9))


def kst(t):
    return datetime.fromtimestamp(t,KST).strftime('%m-%d %H:%M')


def main():
    import sqlite3
    manifest=json.loads(MANIFEST.read_text())
    assert hashlib.sha256(DB.read_bytes()).hexdigest()==manifest['delta_sha256']
    extras=activity.EXTRA+['indicators_json','account_json','decision','action']
    source.FIELDS.extend(k for k in extras if k not in source.FIELDS)
    old,files=source.load()
    new=source.read(DB)
    combined={r['timestamp']:r for r in old}
    combined.update({r['timestamp']:r for r in new})
    rows=sorted(combined.values(),key=lambda r:r['timestamp'])
    for r in rows:
        r['t']=activity.ts(r['timestamp'])
        r['config']=json.loads(r['strategy_config_json'] or '{}')
        r['ind']=json.loads(r['indicators_json'] or '{}')
        r['group']=r['strategy_version']+':'+hashlib.sha256(json.dumps(r['config'],sort_keys=True).encode()).hexdigest()[:8]
    post=[r for r in rows if r['config'].get('entry_min_positive_components')==3]
    start,end=post[0]['t'],post[-1]['t']
    previous_group=next(r['group'] for r in reversed(rows) if r['t']<start)
    entries,unmatched=activity.collect(rows)
    lookup={r['t']:r for r in rows}
    for e in entries:
        e['raw']=lookup[e['row_t']]['ind'].get('atr',{}).get('raw_score')
    after=[e for e in entries if e['t']>=start]
    closed=[e for e in after if e['closed']]
    # Cross-check top-level database events against the separately supplied JSONL.
    def flatten(e):
        return [e.get('close_event',{}),e.get('entry_event',{})] if e.get('type')=='LIVE_REVERSAL' else [e]
    def order_key(e):
        return e.get('type'),(e.get('response') or {}).get('orderId')
    log_events=[json.loads(l) for l in LOG.read_text(encoding='utf-8-sig').splitlines() if l.strip()]
    log_keys={order_key(e) for event in log_events for e in flatten(event)}
    db_events=[e for r in post for e in flatten(json.loads(r['trade_event_json'] or '{}')) if e.get('type') in ('LIVE_ORDER','LIVE_CLOSE')]
    missing=[order_key(e) for e in db_events if order_key(e) not in log_keys]
    closes=[e for e in db_events if e['type']=='LIVE_CLOSE']
    carry=[e for e in closes if activity.ts(e.get('entry_time') or e['logged_at'])<start]
    # Validate all recorded eligibility values against six base components.
    names=('price_position','body','volume','trend_continuity','range','liquidation')
    mismatch=0
    blocked=[]
    for r in post:
        components=json.loads(r['score_components_json'])
        for side in ('LONG','SHORT'):
            actual=[k for k in names if (components.get(k) or {}).get(side.lower()+'_score',0)>0]
            stored=r['ind'].get('entry_eligibility',{}).get(side,{})
            mismatch+=int(stored.get('count')!=len(actual) or stored.get('allowed')!=(len(actual)>=3))
        side={'ENTER_LONG':'LONG','ENTER_SHORT':'SHORT'}.get(r['decision'])
        # Recorder may store state rather than signal; use the same numeric candidate thresholds.
        for side in ('LONG','SHORT'):
            other='SHORT' if side=='LONG' else 'LONG'
            pos=json.loads(r['position_json'] or '{}')
            if pos.get('status')=='OPEN' and pos.get('side')==side:
                continue
            threshold=r['config']['entry_'+side.lower()+'_score']+(r['config']['opposite_reentry_extra_score'] if pos.get('status')=='OPEN' else 0)
            if r[side.lower()+'_score']<threshold or r[side.lower()+'_score']-r[other.lower()+'_score']<r['config']['entry_score_gap'] or r['range_block_trade']:
                continue
            eligibility=r['ind']['entry_eligibility'][side]
            if not eligibility['allowed']:
                blocked.append({'t':r['t'],'side':side,'kind':'전환' if pos.get('status')=='OPEN' else '신규',
                                'price':r['price'],'count':eligibility['count'],'score':r[side.lower()+'_score']})
    episodes=[]
    for b in blocked:
        if not episodes or b['t']-episodes[-1]['last']>180 or (b['kind'],b['side'])!=(episodes[-1]['kind'],episodes[-1]['side']):
            episodes.append(dict(b,last=b['t'],observations=1))
        else:
            episodes[-1]['last']=b['t']; episodes[-1]['observations']+=1
    times=[r['t'] for r in rows]
    for e in episodes:
        i=bisect.bisect_left(times,e['t'])
        for m in (15,60,240):
            j=bisect.bisect_left(times,e['t']+m*60)
            if j>=len(rows) or times[j]-e['t']-m*60>120:
                continue
            path=rows[i:j+1]
            if any(b['t']-a['t']>180 for a,b in zip(path,path[1:])) or any(r['group']!=rows[i]['group'] for r in path):
                continue
            e['return_'+str(m)]=(1 if e['side']=='LONG' else -1)*(rows[j]['price']/e['price']-1)*100
    with closing(sqlite3.connect(DB.as_uri()+'?mode=ro&immutable=1',uri=True)) as c:
        c.row_factory=sqlite3.Row
        oi=[dict(r) for r in c.execute('SELECT * FROM oi_snapshots ORDER BY timestamp')]
    ot=[activity.ts(r['timestamp']) for r in oi]
    gaps=[(a,b) for a,b in zip(ot,ot[1:]) if b-a>60]
    ages=[activity.ts(r['collected_at'])-activity.ts(r['observed_at']) for r in oi if r['observed_at']]
    oi_summary={'rows':len(oi),'statuses':dict(Counter(r['status'] for r in oi)),
                'first':kst(ot[0]),'last':kst(ot[-1]),'expected_minutes':int((ot[-1]-ot[0])/60)+1,
                'gaps':gaps,'max_observation_age_sec':max(ages),'min_oi':min(r['open_interest'] for r in oi),
                'max_oi':max(r['open_interest'] for r in oi),'latest_oi':oi[-1]['open_interest']}
    before=[e for e in entries if e['group']==previous_group and start-(end-start)<=e['t']<start and e['closed'] and activity.ts(e['close_time'])<start]
    regimes={}
    for name,window in [('before',[r for r in rows if start-(end-start)<=r['t']<start and r['group']==previous_group]),('after',post)]:
        regimes[name]={'observations':len(window),'price_start':window[0]['price'],'price_end':window[-1]['price'],
                       'price_change_pct':(window[-1]['price']/window[0]['price']-1)*100,
                       'observed_range_pct':(max(r['price'] for r in window)-min(r['price'] for r in window))/window[0]['price']*100,
                       'raw_atr_counts':dict(Counter(r['ind'].get('atr',{}).get('raw_score') for r in window))}
    out={'start_kst':kst(start),'end_kst':kst(end),'hours':(end-start)/3600,'post_rows':len(post),
         'entries':after,'closed_new_net':sum(e['net'] for e in closed),'closed_new_fees':sum(e['estimated_fees'] for e in closes if activity.ts(e.get('entry_time') or e['logged_at'])>=start),
         'carry_net':sum(e['estimated_realized_pnl'] for e in carry),'all_post_realized':sum(e['estimated_realized_pnl'] for e in closes),
         'account_start':json.loads(post[0]['account_json']),'account_end':json.loads(post[-1]['account_json']),
         'position_end':json.loads(post[-1]['position_json']), 'eligibility_mismatches':mismatch,
         'log_missing_orders':missing,'blocked_observations':len(blocked),'blocked_episodes':episodes,
         'raw_post':dict(Counter(r['ind'].get('atr',{}).get('raw_score') for r in post)),
         'oi':oi_summary,'before_equal_window':before,'market_regimes':regimes}
    lines=['# 9월 29일 중간 점검', '',f'첨부 자료 기준 마지막 시각: {kst(end)} KST. 현재 서버에 접속한 결과는 아닙니다.',
           f'새 제한 첫 확인: {kst(start)} KST. 이후 {(end-start)/3600:.2f}시간, {len(post):,}개 관측값.',
           '원본 파일은 읽기 전용으로 열었습니다. 델타 DB의 SHA-256 검증값과 manifest를 대조했습니다.', '',
           '## 1. 적용 이후 진입·전환·손익',
           f'- 양수 요소 개수와 허용 여부를 6개 본점수로 다시 계산한 불일치: {mismatch}건.',
           f'- 적용 후 DB 주문 이벤트 중 별도 거래 JSONL에서 찾지 못한 주문: {len(missing)}건.',
           f'- 적용 후 신규 포지션 {len(after)}건. 종료 {len(closed)}건, 미종료 {len(after)-len(closed)}건.',
           f'- 적용 후 새로 진입하고 종료된 거래의 추정 순손익: {out["closed_new_net"]:+.4f} USDT. 추정 수수료 {out["closed_new_fees"]:.4f} USDT 포함.',
           f'- 적용 전에 들어가 적용 후 종료한 이월 포지션 순손익: {out["carry_net"]:+.4f} USDT. 새 진입 제한의 성과에 섞지 않았습니다.',
           f'- 기간 중 모든 청산의 추정 순손익: {out["all_post_realized"]:+.4f} USDT.', '',
           '|진입 KST|종류|방향|ATR 원점수|요소 개수|상태|가격 수익률 %|추정 순손익 USDT|보유시간|',
           '|---|---|---|---:|---:|---|---:|---:|---|']
    for e in after:
        count=lookup[e['row_t']]['ind']['entry_eligibility'][e['side']]['count']
        lines.append(f"|{kst(e['t'])}|{'즉시 전환' if e['reversal'] else '일반 진입'}|{e['side']}|{e['raw']}|{count}|{'종료' if e['closed'] else '보유 중'}|{activity.fmt(e.get('pnl'),3)}|{activity.fmt(e.get('net'),4)}|{activity.fmt(e.get('hours'))}시간|")
    lines += ['',f'마지막 보유 포지션: {out["position_end"].get("side")}, 미실현 가격 수익률 {out["position_end"].get("unrealized_pnl_pct"):.3f}%, 미실현 손익 {out["position_end"].get("unrealized_pnl_usdt"):.4f} USDT.',
              f'기록상 자본: {out["account_start"]["equity"]:.4f} → {out["account_end"]["equity"]:.4f} USDT. 이월 포지션 평가손익도 포함합니다.',
              '주문 응답에는 접수 상태가 포함되어 있습니다. 이 수치는 봇이 기록한 추정 손익이며 거래소 확정 체결·펀딩까지 대사한 값이 아닙니다.', '',
              '## 2. 차단 관측',
              f'총점·점수차·RANGE 조건은 충족하지만 요소가 부족한 관측 {len(blocked)}개를 연속 구간 {len(episodes)}개로 묶었습니다.',
              '3분 이내 이어지는 같은 방향·종류의 관측을 한 구간으로 셉니다. 잠깐 조건이 풀린 구간을 함께 묶을 수 있습니다.',
              '실제 거절된 주문 건수가 아닙니다. 재진입 쿨다운 등 다른 제한까지 배제한 단독 원인 판정도 아닙니다.',
              '아래 가격 변화는 차단된 방향으로 가정한 단순 관측입니다. 서로 시간대가 겹칠 수 있으며 실제로 손실을 피한 금액을 뜻하지 않습니다.', '',
              '|시작 KST|종류|방향|관측 수|최초 요소|최초 총점|15분 가격 %|1시간 가격 %|4시간 가격 %|',
              '|---|---|---|---:|---:|---:|---:|---:|---:|']
    for e in episodes:
        lines.append(f"|{kst(e['t'])}|{e['kind']}|{e['side']}|{e['observations']}|{e['count']}|{e['score']}|{activity.fmt(e.get('return_15'),3)}|{activity.fmt(e.get('return_60'),3)}|{activity.fmt(e.get('return_240'),3)}|")
    lines += ['', '## 3. ATR 원점수 3·4·5·6 분석',
              f'새 제한 적용 후 관측 원점수 분포: {out["raw_post"]}. 원점수 3~6 사례는 이번 적용 후 기간에 없습니다.',
              '따라서 이전 최신 설정 구간만 따로 아래에 표시합니다. 새 규칙의 효과 검증에 이전 결과를 섞지 않습니다.',
              '원점수는 진입 시 DB 지표에서 읽고, 종료까지 연결된 거래만 집계했습니다. 3~6은 모두 실제 활동 가점 최대 3점 구간입니다.', '',
              '|이전 규칙 진입 종류|원점수|종료 건수|평균 가격 수익률 %|평균 보유시간|추정 순손익 합계 USDT|',
              '|---|---:|---:|---:|---|---:|']
    raw_stats=[]
    for rev in (False,True):
        for raw in (3,4,5,6):
            es=[e for e in entries if e['group']==previous_group and e['closed'] and e['reversal']==rev and e['raw']==raw and activity.ts(e['close_time'])<start]
            data={'reversal':rev,'raw':raw,'n':len(es),'mean_pnl':activity.avg([e['pnl'] for e in es]),'net':sum(e['net'] for e in es)}
            raw_stats.append(data)
            lines.append(f"|{'즉시 전환' if rev else '일반 진입'}|{raw}|{len(es)}|{activity.fmt(data['mean_pnl'],3)}|{activity.fmt(activity.avg([e['hours'] for e in es]))}시간|{activity.fmt(data['net']) if es else '-'}|")
    out['raw_before_stats']=raw_stats
    lines += ['', '## 4. OI 정기 수집',
              f'- 기간: {oi_summary["first"]} ~ {oi_summary["last"]} KST.',
              f'- 기록 {len(oi):,}개 / 기간 내 분 단위 예상 {oi_summary["expected_minutes"]:,}개.',
              f'- 상태 분포: {oi_summary["statuses"]}. 1분 초과 시각 공백 {len(gaps)}개.',
              f'- 조회 시각 대비 관측 시각의 최대 지연: {max(ages):.3f}초.',
              f'- 마지막 OI: {oi_summary["latest_oi"]:,.3f}. 이 첨부 기간에서는 정기 수집이 확인됩니다.',
              '- 이 파일 마지막 시각 이후 서버 상태까지 보증하는 것은 아닙니다.', '',
              '## 5. 변경 전후 비교의 한계',
              f'- 적용 전 동일 길이 기간에서 시작·종료가 모두 확인된 이전 설정 거래: {len(before)}건, 추정 순손익 {sum(e["net"] for e in before):+.4f} USDT.',
              f'- 적용 후 시작·종료 모두 확인된 거래: {len(closed)}건, 추정 순손익 {out["closed_new_net"]:+.4f} USDT.',
              f'- 비교 기간 BTC 가격 변화: 적용 전 {regimes["before"]["price_change_pct"]:+.3f}%, 적용 후 {regimes["after"]["price_change_pct"]:+.3f}%.',
              f'- 비교 기간 관측 최고·최저 가격 폭(시작가 대비): 적용 전 {regimes["before"]["observed_range_pct"]:.3f}%, 적용 후 {regimes["after"]["observed_range_pct"]:.3f}%. 분 사이 변동은 포함하지 못합니다.',
              '- 기간 길이가 같아도 시장 방향·변동성·종료까지 걸린 시간은 다릅니다. 늦게 진입해 아직 열린 거래는 종료 성적에서 빠지는 편향도 있습니다.',
              '- ATR 원점수 3~6은 적용 후 한 번도 없으므로 높은 활동 구간의 개선 여부는 아직 검증 불가입니다.',
              '- 새 규칙도 1시간 간격 반대 방향 전환 손실을 완전히 방지하지 못했습니다. 요소 3개는 독립된 증거 3개가 아닙니다.',
              '- 이번 결과만으로 규칙을 다시 바꾸거나 수익성이 개선됐다고 결론내리지 않습니다. 매매 코드는 변경하지 않았습니다.']
    assert len(oi)==len(set((r['timestamp'],r['symbol']) for r in oi))
    assert all(r['symbol']=='BTCUSDT' and r['open_interest']>0 for r in oi)
    assert len(db_events)==len(set(order_key(e) for e in db_events))
    assert abs(out['all_post_realized']-(out['account_end']['realized_pnl']-out['account_start']['realized_pnl']))<1e-6
    assert abs(out['all_post_realized']-out['closed_new_net']-out['carry_net'])<1e-6
    (source.ROOT/'MIDWEEK_20260929.md').write_text('\n'.join(lines),encoding='utf-8')
    (source.ROOT/'midweek_20260929.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in out.items() if k not in ('entries','blocked_episodes','before_equal_window')},indent=2))
    print('BLOCKS',len(episodes),Counter(e['kind'] for e in episodes),'POST ENTRIES',len(after),'CLOSED',len(closed))


if __name__=='__main__':
    main()
