"""Read-only weekly diagnosis from market observations and recorded orders."""
import bisect
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import statistics as st

import analyze_entry_paths as src
from analyze_activity import EXTRA, ts, fmt

DB=Path('C:/Users/chlgh/AppData/Local/Temp/delta (2).db')
MANIFEST=Path('C:/Users/chlgh/AppData/Local/Temp/manifest (3).json')
LOG=Path('C:/Users/chlgh/Downloads/live_trade_log (13).jsonl')
KST=timezone(timedelta(hours=9))
COMPONENT_NAMES={'price_position':'장기 가격 위치','body':'몸통','volume':'거래량','trend_continuity':'추세 지속','range':'범위 위치·돌파','liquidation':'청산'}


def kst(t):
    return datetime.fromtimestamp(t,KST).strftime('%m-%d %H:%M')


def stats(events):
    return {'n':len(events),'wins':sum(e['estimated_realized_pnl']>0 for e in events),
            'gross':sum(e['gross_realized_pnl'] for e in events),
            'fees':sum(e['estimated_fees'] for e in events),
            'net':sum(e['estimated_realized_pnl'] for e in events),
            'mean_price_pct':st.mean(e['pnl_pct'] for e in events) if events else None}


def main():
    manifest=json.loads(MANIFEST.read_text())
    assert hashlib.sha256(DB.read_bytes()).hexdigest()==manifest['delta_sha256']
    src.FIELDS.extend(k for k in EXTRA+['indicators_json','account_json','candle_open','candle_direction','position_side','stop_price'] if k not in src.FIELDS)
    rows,files=src.load()
    merged={r['timestamp']:r for r in rows}
    merged.update({r['timestamp']:r for r in src.read(DB)})
    rows=sorted(merged.values(),key=lambda r:r['timestamp'])
    for r in rows:
        r['t']=ts(r['timestamp']); r['ind']=json.loads(r['indicators_json'] or '{}')
        r['config']=json.loads(r['strategy_config_json'])
    times=[r['t'] for r in rows]
    onset=next(r['t'] for r in rows if r['config'].get('entry_min_positive_components')==3)
    def at(t):
        i=bisect.bisect_right(times,t)-1
        return rows[i] if i>=0 and t-times[i]<=120 else None
    raw=[json.loads(l) for l in LOG.read_text(encoding='utf-8-sig').splitlines() if l.strip()]
    events={}
    for e in raw:
        parts=[e.get('close_event'),e.get('entry_event')] if e.get('type')=='LIVE_REVERSAL' else [e]
        for p in parts:
            if p and p.get('status')=='SENT' and not p.get('dry_run'):
                key=(p['type'],(p.get('response') or {}).get('orderId'))
                assert key not in events or events[key]==p
                events[key]=p
    closes=sorted([e for e in events.values() if e['type']=='LIVE_CLOSE'],key=lambda e:e['logged_at'])
    opens={e.get('entry_time',e['logged_at']):e for e in events.values() if e['type']=='LIVE_ORDER'}
    post=[e for e in closes if ts(e['entry_time'])>=onset]
    carry=[e for e in closes if ts(e['entry_time'])<onset<=ts(e['exit_time'])]
    summaries={'all':stats(closes),'post_new_entries':stats(post),'carry':stats(carry),
               'reversal_exit':stats([e for e in closes if 'reversal' in e['reason']]),
               'profit_exit':stats([e for e in closes if 'profit protection' in e['reason']])}
    trades=[]
    for e in closes:
        start,end=ts(e['entry_time']),ts(e.get('exit_time') or e['logged_at'])
        entry=opens.get(e['entry_time'])
        r=at(start)
        if r is None:
            continue
        segment=rows[bisect.bisect_left(times,start):bisect.bisect_right(times,end)]
        direction=1 if e['side']=='LONG' else -1
        side=e['side'].lower(); other='short' if side=='long' else 'long'
        components=json.loads(r['score_components_json'])
        evidence=r['ind'].get('entry_eligibility',{}).get(e['side'])
        details={'entry':kst(start),'exit':kst(end),'side':e['side'],'hours':(end-start)/3600,
                 'entry_time':e['entry_time'],'entry_price':e['entry_price'],'exit_price':e['exit_price'],
                 'pnl_pct':e['pnl_pct'],'net':e['estimated_realized_pnl'],'fees':e['estimated_fees'],
                 'reason':e['reason'],'peak':e.get('peak_profit_pct'),'new_rule':start>=onset,
                 'entry_long':r['long_score'],'entry_short':r['short_score'],
                 'exit_long':(e.get('score_context') or {}).get('long_score'),
                 'exit_short':(e.get('score_context') or {}).get('short_score'),
                 'entry_candle_open':r['candle_open'],'entry_from_day_open_pct':(r['price']/r['candle_open']-1)*100,
                 'raw_atr':r['ind'].get('atr',{}).get('raw_score'),
                 'components':components,'eligibility':evidence,
                 'max_gap_seconds':max([b['t']-a['t'] for a,b in zip(segment,segment[1:])],default=0),
                 'stop_distance_pct':abs(((entry or {}).get('stop_price') or e['entry_price'])/e['entry_price']-1)*100}
        reversals=[q for q in segment if q[other+'_score']>=q['config']['entry_'+other+'_score']+q['config']['opposite_reentry_extra_score'] and q[other+'_score']-q[side+'_score']>=q['config']['entry_score_gap'] and not q['range_block_trade']]
        valid=[q for q in reversals if not q['config'].get('entry_min_positive_components') or q['ind'].get('entry_eligibility',{}).get(other.upper(),{}).get('allowed')]
        details['first_reverse_candidate']=kst(valid[0]['t']) if valid else None
        details['trajectory']=[]
        for label,t in [('진입 1시간 전',start-3600),('진입 15분 전',start-900),('진입',start),('15분 후',start+900),('1시간 후',start+3600),('청산',end)]:
            q=at(t)
            if q and (t<=end or t<start):
                details['trajectory'].append({'label':label,'kst':kst(q['t']),'price':q['price'],'long':q['long_score'],'short':q['short_score'],'price_pnl':direction*(q['price']/e['entry_price']-1)*100})
        trades.append(details)
    # Re-entry after profit: same direction and elapsed time, not a strategy replay.
    reentries=[]
    for previous in closes:
        if 'profit protection' not in previous['reason']:
            continue
        end=ts(previous['exit_time'])
        next_open=min((o for o in opens.values() if ts(o['entry_time'])>end),key=lambda o:o['entry_time'],default=None)
        if next_open and next_open['position_side']==previous['side']:
            close=next((e for e in closes if e['entry_time']==next_open['entry_time']),None)
            reentries.append({'at':kst(ts(next_open['entry_time'])),'minutes':(ts(next_open['entry_time'])-end)/60,
                              'side':previous['side'],'price_from_exit_pct':(next_open['price']/previous['exit_price']-1)*100,
                              'net':close['estimated_realized_pnl'] if close else None})
    snapshot_first=at(ts(closes[0]['exit_time'])-60)
    end_account=json.loads(rows[-1]['account_json'])
    out={'end':kst(times[-1]),'onset':kst(onset),'summary':summaries,'trades':trades,'reentries':reentries,
         'last_account':end_account,'last_position':json.loads(rows[-1]['position_json']),
         'sources':[str(p) for p in files]+[str(DB),str(MANIFEST),str(LOG)]}
    lines=['# 이번주 손실 원인 분석', '',f'자료 종료: {out["end"]} KST. 거래 로그 청산 범위: {kst(ts(closes[0]["exit_time"]))} ~ {kst(ts(closes[-1]["exit_time"]))} KST.',
           '현재 서버 상태가 아니라 첨부 파일 분석입니다. 손익은 봇 로그의 추정 수수료 차감 값이며 확정 체결·펀딩 대사가 아닙니다.',
           f'새 요소 3개 제한 첫 확인: {out["onset"]} KST. 변경 전후를 별도로 집계했습니다.', '',
           '## 1. 숫자로 본 결과', '', '|구분|종료 건수|수익 거래|비용 전 손익 USDT|추정 수수료 USDT|순손익 USDT|','|---|---:|---:|---:|---:|---:|']
    for key,label in [('all','로그 전체'),('post_new_entries','새 규칙 적용 후 진입한 거래'),('carry','이전 진입·적용 후 종료'),('reversal_exit','반대 방향 전환 때문에 청산'),('profit_exit','수익보호 청산')]:
        s=summaries[key]; lines.append(f"|{label}|{s['n']}|{s['wins']}|{s['gross']:+.2f}|{s['fees']:.2f}|{s['net']:+.2f}|")
    lines += ['', '구분별 표는 일부 거래가 중복 포함되므로 행끼리 합산하지 않습니다. 첫 청산은 이 로그보다 앞선 진입이며 시장 DB로 보완했습니다.',
              f'마지막 기록의 포지션 상태: {out["last_position"].get("status")}. 기록상 자본 {end_account.get("equity"):.2f} USDT.', '',
              '## 2. 확인된 손실 구조',
              f"- 수익 거래 한 건의 평균 순이익은 {summaries['profit_exit']['net']/summaries['profit_exit']['n']:.2f} USDT, 손실 거래 한 건의 평균 순손실은 {abs(summaries['reversal_exit']['net']/summaries['reversal_exit']['n']):.2f} USDT입니다.",
              '- 큰 손실 두 건은 진입 점수가 19점·22점이고 양수 요소도 각각 4개였습니다. 높은 점수와 여러 요소만으로 이후 가격의 지속을 보장하지 못했습니다.',
              '- 큰 손실 두 건의 최초 손절 거리는 약 4.2~4.4%였습니다. 반대 점수 조건이 충족되기 전까지 2% 이상의 가격 손실을 허용할 수 있는 상태였습니다.',
              '- 이 표본에서는 반대 신호 전환 청산이 손실의 중심입니다. 그러나 전환을 하지 않았으면 더 좋았다는 반사실 결론은 아닙니다.',
              '- 수익보호로 얻은 작은 이익보다 일부 전환 손실이 큽니다. 승률만으로 성과를 평가하면 안 됩니다.',
              '- 수수료도 손익을 악화시켰지만 비용 전 손익부터 음수라면 수수료만이 원인은 아닙니다.',
              '- 수익보호 후 약 30분 뒤 같은 방향으로 다시 진입하는 패턴을 별도 집계했습니다. 이 제한 시간이 끝났다는 사실이 새로운 추세의 증거는 아닙니다.', '',
              '## 3. 손실이 가장 컸던 거래', '시간은 모두 한국 시간입니다. 가격 수익률은 레버리지·비용 차감 전입니다.']
    for t in sorted(trades,key=lambda t:t['net'])[:3]:
        lines += ['',f"### {t['entry']} {t['side']} 진입 → {t['exit']} 종료",
                  f"- 가격 {t['entry_price']:,.2f} → {t['exit_price']:,.2f}; 가격 수익률 {t['pnl_pct']:+.3f}%; 추정 순손익 {t['net']:+.2f} USDT.",
                  f"- 보유 {t['hours']:.2f}시간. 기록된 최고 수익률 {fmt(t['peak'],3)}%. 최초 손절 거리 {t['stop_distance_pct']:.2f}%.",
                  f"- 진입 점수 LONG {t['entry_long']} / SHORT {t['entry_short']}. 종료 점수 LONG {t['exit_long']} / SHORT {t['exit_short']}.",
                  f"- 진입 가격은 당일 시가 대비 {t['entry_from_day_open_pct']:+.3f}%. ATR 원점수 {t['raw_atr']}. 새 규칙 적용 여부: {'적용 후' if t['new_rule'] else '적용 전'}.",
                  f"- 진입 요소: {', '.join(COMPONENT_NAMES.get(c,c) for c in (t['eligibility'] or {}).get('components',[])) or '당시 개수 기록 없음'}.",
                  f"- 처음 관측된 반대 전환 후보: {t['first_reverse_candidate'] or '관측되지 않음'}. 구간 최대 기록 간격 {t['max_gap_seconds']:.0f}초.",
                  '|시점|KST|가격|LONG|SHORT|진입가 대비 방향 수익률 %|','|---|---|---:|---:|---:|---:|']
        for q in t['trajectory']:
            lines.append(f"|{q['label']}|{q['kst']}|{q['price']:,.1f}|{q['long']}|{q['short']}|{q['price_pnl']:+.3f}|")
    lines += ['', '## 4. 수익보호 후 같은 방향 재진입', '', '|재진입 KST|방향|대기 분|직전 청산가 대비 가격 변화 %|이후 종료 순손익 USDT|','|---|---|---:|---:|---:|']
    for e in reentries:
        lines.append(f"|{e['at']}|{e['side']}|{e['minutes']:.1f}|{e['price_from_exit_pct']:+.3f}|{fmt(e['net'])}|")
    lines += ['', '## 5. 전체 청산 목록', '', '|진입 KST|종료 KST|방향|새 규칙 이후 진입|수익률 %|순손익 USDT|보유 시간|','|---|---|---|---|---:|---:|---:|']
    for t in trades:
        lines.append(f"|{t['entry']}|{t['exit']}|{t['side']}|{'예' if t['new_rule'] else '아니오'}|{t['pnl_pct']:+.3f}|{t['net']:+.2f}|{t['hours']:.2f}|")
    lines += ['', '## 6. 해석과 다음 검증',
              '- 일봉 기반 점수를 매분 판단하는 방식과 짧은 수익보호·재진입·반대 전환의 조합이 반복 매매를 만들었다는 설명은 관측과 부합합니다. 이것이 유일한 원인이라는 인과 검증은 아닙니다.',
              '- 양수 요소 3개도 몸통·거래량·추세처럼 같은 진행 중 일봉에 의존할 수 있습니다. 독립된 추세 확인 3회와 다릅니다.',
              '- 반대 전환을 금지하거나 손절을 더 늦췄을 때의 결과는 이번에 계산하지 않았습니다. 손실이 더 커질 수도 있습니다.',
              '- 시간봉으로 전면 변경하기 전에, 동일한 점수로 진입·전환 판단 시점만 제한하는 가상 재생을 분리해 비교할 수 있습니다. 손절·수익보호 감시는 별도로 유지해야 합니다.',
              '- 사후에 눈에 띈 조건을 바로 실거래에 붙이지 말고, 비용 포함한 시간순 검증을 해야 합니다.',
              '- 원본 자료와 매매 코드는 수정하지 않았습니다. 신규 전략 수익성을 보장하지 않습니다.']
    (src.ROOT/'WEEKLY_FAILURE_20261002.md').write_text('\n'.join(lines),encoding='utf-8')
    (src.ROOT/'weekly_failure_20261002.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
    assert all(abs(e['gross_realized_pnl']-e['estimated_fees']-e['estimated_realized_pnl'])<1e-7 for e in closes)
    print(json.dumps(summaries,indent=2))
    print('TOP',json.dumps([{k:t[k] for k in ('entry','exit','side','net','hours','entry_long','entry_short','exit_long','exit_short','peak','first_reverse_candidate','stop_distance_pct')} for t in sorted(trades,key=lambda t:t['net'])[:3]]))
    print('LAST',out['last_account'],out['last_position'])


if __name__=='__main__':
    main()
