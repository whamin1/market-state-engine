"""Presentation only: never query the exchange or modify trading state."""

from datetime import datetime, timedelta, timezone
import re


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _number(value, signed=False):
    return "N/A" if value is None else format(value, "+,.2f" if signed else ",.2f")


def _difference(left, right):
    return None if left is None or right is None else left - right


def _duration(start, end):
    try:
        minutes = max(0, int((_time(end) - _time(start)).total_seconds() // 60))
    except (ValueError, TypeError):
        return "N/A"
    days, hours = minutes // 1440, (minutes % 1440) // 60
    return f"{days}d {hours}h" if days else (f"{hours}h {minutes % 60}m" if hours else f"{minutes}m")


def _entry_reasons(reasons, side):
    labels = {"price_position": "장기 가격 위치", "body_score": "봉 몸통", "volume_score": "거래량",
              "trend_continuity": "추세", "range_break_score": "RANGE 돌파/이탈",
              "range_position_score": "RANGE 내부 위치", "liquidation_score": "청산 불균형",
              "liquidation_activity_bonus": "청산 활성도", "activity_direction": "ATR 방향 가점"}
    contributions = {}
    for reason in reasons:
        match = re.search(r"(?:^|\s)\+(\d+)(?:\s|$)", reason)
        if side not in reason.split() or not match or int(match[1]) <= 0:
            continue
        label = labels.get(reason.split()[0], reason)
        contributions[label] = contributions.get(label, 0) + int(match[1])
    return [f"- {label}: +{score}" for label, score in contributions.items()] or ["- 기록 없음"]


def format_live_trade_report(event):
    n, diff = _number, _difference
    context = event.get("score_context") or {}
    long_score, short_score = context.get("long_score"), context.get("short_score")
    entering = event["type"] == "LIVE_ORDER"
    side = event.get("position_side", event.get("side")) if entering else event.get("side")
    dry_run = event.get("dry_run") or str(event.get("status", "")).startswith("DRY_RUN")
    time = event.get("entry_time" if entering else "exit_time") or event.get("logged_at")
    try:
        display_time = _time(time).astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M KST")
    except (ValueError, TypeError):
        display_time = "N/A"
    lines = [f"{'🟢' if entering else '🔴'} {side} {'진입' if entering else '청산'}",
             f"모드: {'DRY_RUN' if dry_run else 'REAL_ORDER'} / 상태: {event.get('status', '거래소 종료 감지')}",
             f"시간: {display_time} (봇 관측 시각)"]
    if entering:
        lines.extend([f"진입가(판단): {n(event.get('entry_price', event.get('price')))}", "",
                      "[진입 당시 점수]", f"LONG {n(long_score)} / SHORT {n(short_score)}",
                      f"Spread {n(diff(long_score, short_score), True)} / Activity {n(context.get('activity_score'))}",
                      "", "[포지션]", f"손절가: {n(event.get('stop_price'))}",
                      f"설정 마진: {n(event.get('margin_usdt'))} USDT / 레버리지: {n(event.get('leverage'))}x"])
        quantity, price = event.get("quantity"), event.get("price")
        notional = float(quantity) * price if quantity is not None and price is not None else None
        lines.extend([f"주문 수량 기준 포지션(추정): {n(notional)} USDT", "", "[진입 근거]"])
        lines.extend(_entry_reasons(context.get("reasons") or [], side))
    else:
        entry_long, entry_short = event.get("entry_long_score"), event.get("entry_short_score")
        before, after = diff(entry_long, entry_short), diff(long_score, short_score)
        lines.extend([f"진입가: {n(event.get('entry_price'))} / 청산가(관측): {n(event.get('exit_price'))}",
                      f"수익률(가격 기준·추정): {n(event.get('pnl_pct'), True)}%",
                      f"실현손익(수수료 차감·추정): {n(event.get('estimated_realized_pnl'), True)} USDT",
                      f"추정 수수료: {n(event.get('estimated_fees'))} USDT",
                      f"보유시간(관측): {_duration(event.get('entry_time'), time)}",
                      f"청산 이유: {event.get('reason')}", "", "[점수 변화]",
                      f"진입 LONG {n(entry_long)} / SHORT {n(entry_short)} / Spread {n(before, True)}",
                      f"청산 LONG {n(long_score)} / SHORT {n(short_score)} / Spread {n(after, True)}",
                      f"변화 LONG {n(diff(long_score, entry_long), True)} / SHORT {n(diff(short_score, entry_short), True)}",
                      f"Spread {n(before, True)} → {n(after, True)} (변화 {n(diff(after, before), True)})",
                      f"최고 수익률(관측): {n(event.get('peak_profit_pct'), True)}%"])
    if not dry_run:
        lines.append("체결가·체결시각 확정 보고가 아닙니다. 거래소 실제 결과와 다를 수 있습니다.")
    return "\n".join(lines)
