"""Offline activity attribution; does not simulate changing the strategy."""
import bisect
from collections import Counter
from datetime import datetime
import json
import statistics as st

import analyze_entry_paths as source

EXTRA = ['atr_activity_score', 'activity_direction_long_score', 'activity_direction_short_score',
         'trade_event_json', 'position_json']


def ts(value):
    return datetime.fromisoformat(value).timestamp()


def avg(values):
    return st.mean(values) if values else None


def fmt(value, digits=2):
    return '-' if value is None else f'{value:.{digits}f}'


def collect(rows):
    entries, unmatched = [], []
    active = None
    seen = set()
    for r in rows:
        event = json.loads(r.get('trade_event_json') or '{}')
        if not event:
            continue
        parts = [(event, False)]
        if event.get('type') == 'LIVE_REVERSAL':
            parts = [(event.get('close_event') or {}, True), (event.get('entry_event') or {}, True)]
        for e, reversal in parts:
            if e.get('status') != 'SENT' or e.get('dry_run'):
                continue
            kind = e.get('type')
            key = (kind, (e.get('response') or {}).get('orderId'), e.get('logged_at'))
            if key in seen:
                continue
            seen.add(key)
            if kind == 'LIVE_ORDER':
                side = e.get('position_side')
                if side not in ('LONG', 'SHORT'):
                    continue
                entry_at = e.get('entry_time') or e.get('logged_at')
                activity = (e.get('score_context') or {}).get('activity_score')
                active = {'timestamp':entry_at, 't':ts(entry_at), 'group':r['group'],
                          'side':side, 'activity':activity, 'reversal':reversal,
                          'row_t':r['t'], 'bonus':r['activity_direction_'+side.lower()+'_score'],
                          'closed':False, 'price':e.get('entry_price') or e['price']}
                entries.append(active)
            elif kind == 'LIVE_CLOSE':
                reason = None
                if active is None or active['side'] != e.get('side') or active['group'] != r['group']:
                    reason = 'no matching same-config entry'
                elif e.get('entry_time') and abs(ts(e['entry_time'])-active['t'])>120:
                    reason = 'entry timestamp mismatch'
                if reason:
                    unmatched.append({'time':r['timestamp'], 'reason':reason})
                    active = None
                    continue
                end = ts(e.get('exit_time') or e['logged_at'])
                active.update(closed=True, hours=(end-active['t'])/3600, pnl=e.get('pnl_pct'),
                              net=e.get('estimated_realized_pnl'), exit_reason=e.get('reason'),
                              close_time=e.get('exit_time') or e['logged_at'])
                active = None
    return entries, unmatched


def main():
    source.FIELDS.extend(k for k in EXTRA if k not in source.FIELDS)
    rows, files = source.load()
    latest = rows[-1]['group']
    entries, unmatched = collect(rows)
    times = [r['t'] for r in rows]
    for e in entries:
        i=bisect.bisect_left(times,e['row_t'])
        for minutes in (15,60,240):
            j=bisect.bisect_left(times,e['row_t']+minutes*60)
            if j>=len(rows) or times[j]-e['row_t']-minutes*60>120:
                continue
            path=rows[i:j+1]
            if any(q['group']!=e['group'] for q in path) or any(b['t']-a['t']>180 for a,b in zip(path,path[1:])):
                continue
            direction=1 if e['side']=='LONG' else -1
            e['return_'+str(minutes)]=direction*(rows[j]['price']/rows[i]['price']-1)*100
    current=[e for e in entries if e['group']==latest]
    latest_rows=[r for r in rows if r['group']==latest]
    lines=['# 활동 점수별 진입·방향 전환·보유시간', '',
           f"자료: {rows[0]['timestamp']} ~ {rows[-1]['timestamp']} (UTC).",
           f'아래 표는 최신 저장 설정 {latest}의 {len(latest_rows):,}개 관측값만 사용했습니다.', '',
           '## 먼저 알아둘 점',
           '- 여기서 활동 점수는 ATR 활동 점수 0~3입니다. 청산 활동 점수 0~6 및 청산 추가점수와 다릅니다.',
           '- 현재 일봉이 양봉이면 롱에, 음봉이면 숏에 ATR 활동 점수가 붙습니다. 진입 방향과 같지 않을 수도 있습니다.',
           '- 일반 진입은 LIVE_ORDER, 즉시 방향 전환은 LIVE_REVERSAL 안의 새 진입으로 구분했습니다. 일반 진입에는 재진입과 별도 청산 후 반대 진입도 포함됩니다.',
           '- 방향 전환 성적은 새 포지션을 연 뒤 종료할 때까지의 성적입니다. 전환 직전에 닫은 포지션의 손실을 중복 합산하지 않습니다.',
           '- 종료 거래만 수익률·보유시간에 넣습니다. 열린 거래와 연결할 수 없는 청산을 손실/0시간으로 처리하지 않습니다.',
           '- 수익률은 로그의 가격 수익률(레버리지 미반영, 비용 차감 전), 순손익은 로그의 추정 수수료 차감 USDT입니다. 거래소의 확정 체결 손익 검증은 아닙니다.',
           '- 같은 전략 버전/저장 설정이라도 기록되지 않은 코드 변경과 시장 시기의 차이는 남아 있습니다. 활동 점수의 인과 효과를 뜻하지 않습니다.',
           '- 진입 제한, 점수 제거, 청산 조건은 변경하지 않았습니다. 손실이 없어지는 조건을 보장할 수 없습니다.', '',
           '## 실제 기록된 진입과 종료 성적', '',
           '|종류|진입 시 활동|진입 건수|종료 건수|평균 수익률 %|양수 거래 비율 %|평균 보유시간|중간 보유시간|추정 순손익 합계 USDT|',
           '|---|---:|---:|---:|---:|---:|---|---|---:|']
    results={}
    for reversal in (False,True):
        for activity in range(4):
            subset=[e for e in current if e['reversal']==reversal and e['activity']==activity]
            done=[e for e in subset if e['closed']]
            pnl=[e['pnl'] for e in done if e.get('pnl') is not None]
            durations=[e['hours'] for e in done]
            net=[e['net'] for e in done if e.get('net') is not None]
            label='즉시 방향 전환' if reversal else '일반 진입'
            lines.append(f"|{label}|{activity}|{len(subset)}|{len(done)}|{fmt(avg(pnl))}|{fmt(100*sum(v>0 for v in pnl)/len(pnl) if pnl else None,1)}|{fmt(avg(durations))}시간|{fmt(st.median(durations) if durations else None)}시간|{fmt(sum(net) if net else None)}|")
            results[f'{reversal}_{activity}']={'entries':len(subset),'closed':len(done),'mean_pnl':avg(pnl),'mean_hours':avg(durations)}
    lines += ['', '## 롱과 숏을 나누면', '', '|종류|방향|활동|종료 건수|평균 수익률 %|평균 보유시간|','|---|---|---:|---:|---:|---|']
    for reversal in (False,True):
        for side in ('LONG','SHORT'):
            for activity in range(4):
                done=[e for e in current if e['closed'] and e['reversal']==reversal and e['side']==side and e['activity']==activity]
                if done:
                    lines.append(f"|{'즉시 전환' if reversal else '일반 진입'}|{'롱' if side=='LONG' else '숏'}|{activity}|{len(done)}|{fmt(avg([e['pnl'] for e in done]))}|{fmt(avg([e['hours'] for e in done]))}시간|")
    lines += ['', '## 진입 이후 가격 변화: 실제 청산과 무관한 고정 시간 비교',
              '진입 시 관측 가격부터 롱/숏 방향을 반영했습니다. 비용 미반영이며 시간대가 겹치는 표본도 포함합니다. 괄호는 유효 건수입니다.', '',
              '|종류|활동|15분 뒤 평균 %|1시간 뒤 평균 %|4시간 뒤 평균 %|', '|---|---:|---:|---:|---:|']
    for reversal in (False,True):
        for activity in range(4):
            subset=[e for e in current if e['reversal']==reversal and e['activity']==activity]
            cells=[]
            for m in (15,60,240):
                values=[e['return_'+str(m)] for e in subset if 'return_'+str(m) in e]
                cells.append(f'{fmt(avg(values),3)} ({len(values)}건)')
            lines.append(f"|{'즉시 전환' if reversal else '일반 진입'}|{activity}|"+'|'.join(cells)+'|')
    lines += ['', '## 활동 점수 자체가 유지되는 시간',
              '포지션 보유시간과 별개입니다. 점수가 바뀌어 시작과 끝이 모두 확인된 구간만 집계합니다. 자료 경계나 3분 초과 공백으로 잘린 구간은 제외합니다.', '',
              '|활동|관측 건수|완전한 유지 구간|평균 유지시간|중간 유지시간|','|---|---:|---:|---|---|']
    runs=[]
    start=0
    left_censored=True
    for i in range(1,len(latest_rows)):
        a,b=latest_rows[i-1],latest_rows[i]
        gap=b['t']-a['t']>180
        change=b['atr_activity_score']!=a['atr_activity_score']
        if gap or change:
            if not gap and not left_censored:
                runs.append((a['atr_activity_score'],(b['t']-latest_rows[start]['t'])/3600))
            start=i
            left_censored=gap
    for activity in range(4):
        hours=[v for s,v in runs if s==activity]
        lines.append(f"|{activity}|{sum(r['atr_activity_score']==activity for r in latest_rows)}|{len(hours)}|{fmt(avg(hours))}시간|{fmt(st.median(hours) if hours else None)}시간|")
    lines += ['', '## 시장 활동 점수와 실제 받은 방향 가점의 차이', '',
              '|시장 활동 점수|진입 방향이 받은 활동 가점|진입 건수|','|---|---:|---:|']
    for (activity, bonus), count in sorted(Counter((e['activity'],e['bonus']) for e in current).items()):
        lines.append(f'|{activity}|{bonus}|{count}|')
    lines += ['', '시장 활동이 3점이어도 진입 방향에 가점이 없던 사례가 있습니다. 앞의 표는 시장 활동 수준별 분류이며, 가점 자체의 효과를 분리한 실험이 아닙니다.']
    lines += ['', '## 검증과 해석 한계',
              f'- 최신 설정 진입 기록 {len(current)}건, 종료 연결 {sum(e["closed"] for e in current)}건. 미완료/미연결 진입은 별도 보존했습니다.',
              f'- 전체 기간에서 진입과 같은 설정으로 연결하지 못한 청산 {len(unmatched)}건. 원인과 전체 거래 연결 결과는 JSON에 보존했습니다.',
              '- 평균 보유시간이 길다는 것만으로 좋은 조건은 아닙니다. 오래 버틴 손실 포지션일 수도 있습니다.',
              '- 일부 집단이 작으므로 이번 표만 보고 특정 활동 점수를 금지하거나 제거하면 과적합 위험이 있습니다.',
              '- 활동 점수를 제거한 가상 성과는 계산하지 않았습니다. 이를 검증하려면 모든 후속 진입·전환·청산을 순서대로 재생해야 합니다.']
    (source.ROOT/'ACTIVITY_REVIEW_20260927.md').write_text('\n'.join(lines),encoding='utf-8')
    (source.ROOT/'activity_results.json').write_text(json.dumps({'sources':[str(p) for p in files], 'latest':latest,'summary':results,'entries':entries,'unmatched':unmatched},indent=2),encoding='utf-8')
    print(json.dumps(results,indent=2))
    print('CURRENT',len(current),'CLOSED',sum(e['closed'] for e in current),'UNMATCHED',len(unmatched))


if __name__=='__main__':
    main()
