"""Read-only catalogue of uploaded real-order losses. No trading changes."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
DOWNLOADS = Path('C:/Users/chlgh/Downloads')
KST = timezone(timedelta(hours=9))


def ts(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


def local(value):
    return datetime.fromtimestamp(ts(value),KST).strftime('%Y-%m-%d %H:%M') if value else None


def duration(hours):
    if hours is None:
        return '미기록'
    minutes = round(hours*60)
    return f'{minutes//60}h {minutes%60}m'


def number(value, digits=2):
    return '미기록' if value is None else f'{value:+.{digits}f}'


def flatten(event):
    return [e for e in (event.get('close_event'),event.get('entry_event')) if e] if event.get('type')=='LIVE_REVERSAL' else [event]


def identity(e):
    response=e.get('response') or {}
    if response.get('orderId') is not None:
        return (e.get('symbol'),str(response['orderId']),e.get('type'))
    return (e.get('symbol'),e.get('type'),e.get('exit_time') or e.get('logged_at'),
            e.get('side'),e.get('quantity'),e.get('exit_price'))


def reason_group(reason):
    if 'opposite' in reason or 'reversal' in reason:
        return '반대 신호'
    if 'profit protection' in reason:
        return '수익 보호'
    if 'trailing' in reason:
        return '추적 청산'
    if 'stop loss' in reason:
        return '손절 표시'
    return reason or '미기록'


def run():
    sources,unique,counts,conflicts=[],{},Counter(),[]
    paths=sorted(DOWNLOADS.glob('live_trade_log*.jsonl'),key=lambda p:int(re.search(r'\((\d+)\)',p.name)[1]) if '(' in p.name else 0)
    for path in paths:
        content=path.read_bytes()
        sources.append({'path':str(path),'sha256':hashlib.sha256(content).hexdigest()})
        for line_number,line in enumerate(content.decode('utf-8-sig').splitlines(),1):
            if not line.strip():
                continue
            for event in flatten(json.loads(line)):
                counts['raw_events']+=1
                if event.get('status')!='SENT' or event.get('dry_run'):
                    counts['excluded_non_sent']+=1
                    continue
                if event.get('symbol')!='BTCUSDT':
                    counts['excluded_other_symbol']+=1
                    continue
                key=identity(event)
                location={'file':path.name,'line':line_number}
                if key in unique:
                    counts['duplicates']+=1
                    old=unique[key]['event']
                    for field in ('entry_price','exit_price','quantity','pnl_pct','estimated_realized_pnl','reason'):
                        if old.get(field) is not None and event.get(field) is not None and old[field]!=event[field]:
                            conflicts.append({'key':key,'field':field,'old':old[field],'new':event[field]})
                    unique[key]['sources'].append(location)
                    unique[key]['event'].update({k:v for k,v in event.items() if v is not None})
                else:
                    unique[key]={'event':event,'sources':[location]}
    assert not conflicts, conflicts
    closes=[x for x in unique.values() if x['event']['type']=='LIVE_CLOSE']
    orders=[x['event'] for x in unique.values() if x['event']['type']=='LIVE_ORDER']
    rows=[]
    for item in closes:
        e=item['event']
        t=e.get('exit_time') or e['logged_at']
        entry=e.get('entry_time')
        context=e.get('score_context') or {}
        fees_known=e.get('estimated_fees') is not None and e.get('gross_realized_pnl') is not None
        amount=e.get('estimated_realized_pnl')
        pnl=e.get('pnl_pct')
        if fees_known:
            assert abs(e['gross_realized_pnl']-e['estimated_fees']-amount)<1e-6
        expected=(e['exit_price']/e['entry_price']-1)*100*(1 if e['side']=='LONG' else -1)
        assert abs(expected-pnl)<1e-6
        loss=amount<0 if amount is not None else pnl<0
        hours=(ts(t)-ts(entry))/3600 if entry else None
        assert hours is None or hours>=0
        next_orders=[o for o in orders if 0<=ts(o['logged_at'])-ts(e['logged_at'])<=120]
        opposite=[o for o in next_orders if o.get('position_side')!=e['side']]
        rows.append({'exit_kst':local(t),'exit_time':t,'entry_kst':local(entry),'entry_time':entry,
            'side':e['side'],'entry_price':e['entry_price'],'exit_price':e['exit_price'],
            'quantity':e['quantity'],'entry_notional':abs(float(e['quantity']))*e['entry_price'],
            'price_return_pct':pnl,'reported_pnl_usdt':amount,'fees_known':fees_known,
            'gross_usdt':e.get('gross_realized_pnl'),'fees_usdt':e.get('estimated_fees'),
            'loss':loss,'reason':e.get('reason'),'reason_group':reason_group(e.get('reason','')),
            'peak_profit_pct':e.get('peak_profit_pct'),'hold_hours':hours,
            'entry_long_score':e.get('entry_long_score'),'entry_short_score':e.get('entry_short_score'),
            'exit_long_score':context.get('long_score'),'exit_short_score':context.get('short_score'),
            'opposite_entry_within_2m':bool(opposite),'order_id':(e.get('response') or {}).get('orderId'),
            'sources':item['sources']})
    rows.sort(key=lambda r:r['exit_time'])
    losses=sorted([r for r in rows if r['loss']],key=lambda r:r['reported_pnl_usdt'])
    for i,row in enumerate(losses,1):
        row['loss_id']=f'L{i:03}'
    periods={}
    for month in sorted({r['exit_kst'][:7] for r in rows}):
        group=[r for r in rows if r['exit_kst'].startswith(month)]
        periods[month]={'closed':len(group),'losses':sum(r['loss'] for r in group),
            'fee_recorded_net':sum(r['reported_pnl_usdt'] for r in group if r['fees_known']),
            'fee_recorded_count':sum(r['fees_known'] for r in group),
            'legacy_reported_pnl':sum(r['reported_pnl_usdt'] for r in group if not r['fees_known']),
            'legacy_count':sum(not r['fees_known'] for r in group)}
    report={'sources':sources,'counts':dict(counts),'conflicts':conflicts,'unique_types':dict(Counter(x['event']['type'] for x in unique.values())),
            'first_close':rows[0]['exit_kst'],'last_close':rows[-1]['exit_kst'],
            'closed':len(rows),'losses':len(losses),'wins':sum(r['reported_pnl_usdt']>0 for r in rows),
            'flat':sum(r['reported_pnl_usdt']==0 for r in rows),
            'fee_known_loss_count':sum(r['fees_known'] for r in losses),
            'fee_known_losses_sum':sum(r['reported_pnl_usdt'] for r in losses if r['fees_known']),
            'legacy_loss_count':sum(not r['fees_known'] for r in losses),
            'legacy_losses_sum':sum(r['reported_pnl_usdt'] for r in losses if not r['fees_known']),
            'loss_reasons':dict(Counter(r['reason_group'] for r in losses)),
            'loss_peak_recorded':sum(r['peak_profit_pct'] is not None for r in losses),
            'loss_peak_at_least_06':sum(r['peak_profit_pct'] is not None and r['peak_profit_pct']>=0.6 for r in losses),
            'loss_followed_opposite_2m':sum(r['opposite_entry_within_2m'] for r in losses),
            'months':periods,'loss_trades':losses,
            'price_loss_ranking':[r['loss_id'] for r in sorted(losses,key=lambda r:r['price_return_pct'])]}
    fee_rows=[r for r in rows if r['fees_known']]
    winners=[r for r in fee_rows if r['reported_pnl_usdt']>0]
    losers=[r for r in fee_rows if r['reported_pnl_usdt']<0]
    report['fee_recorded_summary']={'count':len(fee_rows),'wins':len(winners),'losses':len(losers),
        'win_rate_pct':100*len(winners)/len(fee_rows),
        'mean_win':sum(r['reported_pnl_usdt'] for r in winners)/len(winners),
        'mean_loss':sum(r['reported_pnl_usdt'] for r in losers)/len(losers),
        'net':sum(r['reported_pnl_usdt'] for r in fee_rows)}
    assert len(unique)==counts['raw_events']-counts['excluded_non_sent']-counts['excluded_other_symbol']-counts['duplicates']
    assert len(rows)==report['wins']+report['losses']+report['flat']
    assert sum(report['loss_reasons'].values())==report['losses']
    (ROOT/'trade_losses_20261005.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    write_report(report)
    for source in sources:
        assert hashlib.sha256(Path(source['path']).read_bytes()).hexdigest()==source['sha256']
    print(json.dumps({k:v for k,v in report.items() if k not in ('sources','loss_trades','price_loss_ranking')},ensure_ascii=False,indent=2))
    print('TOP',json.dumps(losses[:12],ensure_ascii=False,indent=2))


def write_report(r):
    lines=['# 지금까지 올린 실거래 손실 모음','',
        f"첨부·다운로드 거래 로그 {len(r['sources'])}개. 관측된 실제 청산 기간: {r['first_close']} ~ {r['last_close']} KST.",
        f"중복 제거 후 청산 {r['closed']}건: 기록상 수익 {r['wins']}건, 손실 {r['losses']}건, 0 {r['flat']}건.",
        '## 해석 주의',
        '- 업로드된 파일들의 합집합이다. 기간 중 모든 거래가 빠짐없이 수집됐다는 뜻은 아니다.',
        '- SENT인 BTCUSDT 청산만 포함한다. 모의 거래는 제외하고 거래소 주문 ID로 중복 제거했다.',
        '- SENT/주문 응답은 실제 체결 내역 대조가 아니다. 손익은 봇이 기록한 관측 가격 기준 추정치다.',
        '- 수수료 미기록 과거 자료는 비용 차감 여부가 불명확하다. 추정 순손익과 구분한다. 펀딩비는 반영 여부를 확인하지 못했다.',
        '- 시기별 수량·전략·추가 진입 설정이 다르다. 큰 금액 손실이 반드시 더 나쁜 신호를 의미하지는 않는다.',
        '- 원래 로그에 없는 진입 시각·최고 수익·점수는 미기록으로 남겼다. 최고 수익은 분 단위 봇 관측값이다.',
        '- 로그의 청산 이유는 실행 사유이지 손실의 근본 원인을 증명하는 값은 아니다. 전략 수정은 하지 않았다.',
        '', '## 손실 집계',
        f"- 수수료 별도 기록이 있는 손실 {r['fee_known_loss_count']}건의 추정 순손실 합계: {r['fee_known_losses_sum']:+.2f} USDT.",
        f"- 수수료 미기록 손실 {r['legacy_loss_count']}건의 기록 금액 합계: {r['legacy_losses_sum']:+.2f} USDT.",
        '- 위는 손실 거래만의 합계다. 전체 기간 수익 거래를 차감한 계좌 순손익이 아니다.',
        f"- 청산 이유별: {r['loss_reasons']}",
        f"- 손실 중 최고 수익 기록 있음 {r['loss_peak_recorded']}건. 이 중 최고 +0.6% 이상이었다가 손실 종료한 경우 {r['loss_peak_at_least_06']}건.",
        f"- 손실 종료 후 2분 이내 반대 진입 기록: {r['loss_followed_opposite_2m']}건. 그 자체가 손실 원인이라는 뜻은 아니다.",
        '- 반대 진입 간격은 두 이벤트의 logged_at 기준이다. 체결 완료 시간 간격이 아니다.',
        f"- 수수료 기록이 있는 전체 {r['fee_recorded_summary']['count']}건: 승률 {r['fee_recorded_summary']['win_rate_pct']:.1f}%, 평균 이익 {r['fee_recorded_summary']['mean_win']:.2f}, 평균 손실 {r['fee_recorded_summary']['mean_loss']:.2f} USDT. 서로 다른 전략과 거래 규모가 섞인 역사적 요약이다.",
        '', '## 큰 손실 금액 순서 (상위 12건)',
        '|번호|청산 KST|방향|기록 손익 USDT|가격 수익률|비용 기록|청산 이유|최고 수익|보유|',
        '|---|---|---|---:|---:|---|---|---:|---|']
    for t in r['loss_trades'][:12]:
        lines.append(f"|{t['loss_id']}|{t['exit_kst']}|{t['side']}|{number(t['reported_pnl_usdt'])}|{number(t['price_return_pct'])}%|{'왕복 수수료 차감' if t['fees_known'] else '불명'}|{t['reason_group']}|{number(t['peak_profit_pct'])}|{duration(t['hold_hours'])}|")
    lines+=['','## 가격 손실률 순서 (상위 10건)',
        '|번호|청산 KST|방향|가격 수익률|기록 손익 USDT|청산 이유|',
        '|---|---|---|---:|---:|---|']
    lookup={t['loss_id']:t for t in r['loss_trades']}
    for key in r['price_loss_ranking'][:10]:
        t=lookup[key]
        lines.append(f"|{key}|{t['exit_kst']}|{t['side']}|{number(t['price_return_pct'])}%|{number(t['reported_pnl_usdt'])}|{t['reason_group']}|")
    lines+=['','## 월별 관측 건수','|월|청산|손실|비용 기록 있는 전체 거래 순손익|해당 건수|비용 미기록 전체 거래 손익|해당 건수|',
            '|---|---:|---:|---:|---:|---:|---:|']
    for month,p in r['months'].items():
        lines.append(f"|{month}|{p['closed']}|{p['losses']}|{number(p['fee_recorded_net'])}|{p['fee_recorded_count']}|{number(p['legacy_reported_pnl'])}|{p['legacy_count']}|")
    lines+=['','## 손실 거래 전체 상세','점수 표기 순서는 LONG / SHORT. 보유시간과 원본 파일·줄 번호를 함께 남겼다.']
    for t in r['loss_trades']:
        score=lambda a,b: f"{a if a is not None else '미기록'} / {b if b is not None else '미기록'}"
        lines += ['',f"### {t['loss_id']} | {t['exit_kst']} {t['side']} | {number(t['reported_pnl_usdt'])} USDT",
            f"- 진입: {t['entry_kst'] or '미기록'}, 청산: {t['exit_kst']}, 보유: {duration(t['hold_hours'])}.",
            f"- 평단 {t['entry_price']:,.2f} → 청산 {t['exit_price']:,.2f}, 수량 {t['quantity']}, 청산 수량 기준 진입 명목금액 {t['entry_notional']:,.2f} USDT.",
            f"- 가격 수익률 {number(t['price_return_pct'])}%, 최고 수익률 {number(t['peak_profit_pct'])}% (미기록은 알 수 없음).",
            f"- 진입 점수 {score(t['entry_long_score'],t['entry_short_score'])}; 청산 점수 {score(t['exit_long_score'],t['exit_short_score'])}.",
            f"- 청산 사유 원문: `{t['reason']}`.",
            f"- 수수료: {number(t['fees_usdt'])} USDT. {'기록 손익은 추정 수수료 차감 후.' if t['fees_known'] else '비용 차감 여부 불명.'}",
            f"- 2분 이내 반대 진입 기록: {'있음' if t['opposite_entry_within_2m'] else '확인되지 않음'}.",
            '- 원본: '+', '.join(f"{s['file']}:{s['line']}" for s in t['sources'])]
    lines+=['','## 재현','`python work/collect_trade_losses_20261005.py`',
            '상세 구조화 자료: `work/trade_losses_20261005.json`. 원본 파일 해시를 실행 전후 확인하며 수정하지 않는다.']
    (ROOT/'TRADE_LOSSES_20261005.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':
    run()
