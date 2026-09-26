"""Observational OI reporting from existing liquidation rows; no API calls."""

from bisect import bisect_right
from datetime import datetime, timedelta, timezone
import math


def parse_time(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone(timedelta(hours=9)))


def summarize_oi(raw_events, symbol, now, tolerance_seconds=120):
    observations = {}
    for row in raw_events:
        if row.get('symbol') != symbol:
            continue
        try:
            at = parse_time(row.get('event_time_kst'))
            value = float(row.get('oi'))
        except (ValueError, TypeError, OverflowError):
            continue
        if now - timedelta(minutes=65) <= at <= now and math.isfinite(value) and value > 0:
            observations[at] = value
    times = sorted(observations)
    result = {'source': 'liquidation_raw_csv', 'fetch_success_verified': False,
              'observed_at': None, 'oi': None, 'status': 'missing', 'changes': {}}
    if times:
        latest = times[-1]
        result.update(observed_at=latest.isoformat(), oi=observations[latest],
                      status='observed' if (now-latest).total_seconds() <= tolerance_seconds else 'stale')
    for minutes in (5, 15, 60):
        target = now - timedelta(minutes=minutes)
        index = bisect_right(times, target) - 1
        item = {'delta': None, 'pct': None, 'baseline_at': None, 'status': 'missing'}
        if index >= 0:
            at = times[index]
            item['baseline_at'] = at.isoformat()
            if result['status'] == 'observed' and (target-at).total_seconds() <= tolerance_seconds:
                delta = result['oi'] - observations[at]
                item.update(delta=delta, pct=delta / observations[at] * 100, status='observed')
            else:
                item['status'] = 'stale'
        result['changes'][str(minutes)] = item
    return result


def format_hourly_score_oi(snapshot, expires_at):
    result = snapshot.get('result') or {}
    oi = snapshot.get('oi_report') or {}
    lines = ['시간별 점수·OI 관측', f"symbol: {snapshot.get('symbol')}",
             f"가격: {snapshot.get('price')}",
             f"LONG {result.get('long_score')} / SHORT {result.get('short_score')} / 활동 {result.get('activity_score')}"]
    if snapshot.get('time'):
        lines.append(f"시장 시각: {parse_time(snapshot['time']).astimezone(timezone(timedelta(hours=9))):%m-%d %H:%M} KST")
    independent = oi.get('source') == 'binance_oi_1m'
    lines.extend(['', '[OI 변화 · 1분 정기 수집]' if independent else '[OI 변화 · 기존 CSV 관측값]'])
    if independent:
        labels = {'waiting': '수집 준비 중', 'error': '조회 실패', 'storage_error': '저장 실패',
                  'stale': '자료 지연', 'observed': '정상', 'missing': '자료 없음'}
        lines.append(f"수집 상태: {labels.get(oi.get('status'), oi.get('status'))}")
    if oi.get('oi') is not None:
        lines.append(f"마지막 OI: {oi['oi']:,.3f}")
        lines.append(f"기록 시각: {parse_time(oi['observed_at']).astimezone(timezone(timedelta(hours=9))):%m-%d %H:%M:%S} KST")
    for minutes, label in ((5, '5분'), (15, '15분'), (60, '1시간')):
        item = (oi.get('changes') or {}).get(str(minutes)) or {}
        if item.get('pct') is None:
            lines.append(f'{label}: 판단 불가 (자료 부족 또는 지연)')
        else:
            lines.append(f"{label}: {item['delta']:+,.3f} ({item['pct']:+.3f}%)")
    position = snapshot.get('position') or {}
    if position.get('status') == 'OPEN':
        pnl = position.get('unrealized_pnl_pct')
        lines.extend(['', f"보유: {position.get('side')} / 수익률: {pnl:+.2f}%" if pnl is not None else f"보유: {position.get('side')}"])
    note = ('청산과 무관하게 1분마다 수집합니다. 비교 기록이 쌓이기 전에는 판단 불가입니다.' if independent
            else 'OI는 청산 발생 때 기록된 관측값입니다. 조회 성공 여부는 확인되지 않아 참고용입니다.')
    lines.extend(['', note,
                  f"시간별 알림 종료: {parse_time(expires_at).astimezone(timezone(timedelta(hours=9))):%m-%d %H:%M} KST (기존 알림 자동 복귀)"])
    return '\n'.join(lines)
