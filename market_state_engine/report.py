import json
from datetime import datetime, timezone
from pathlib import Path


def build_status_report(
    state_log_path="work/logs/market_state_log.jsonl",
    trade_log_path="work/logs/trade_log.jsonl",
    title="Market State Check",
):
    latest_state = _read_last_jsonl_record(state_log_path)
    trade_events = _read_jsonl_records(trade_log_path)

    if latest_state is None:
        return f"{title}\nNo market state records yet."

    result = latest_state["result"]
    range_info = result.get("range") or {}
    failures = result.get("failure_counts") or {}
    recent_trade_events = trade_events[-5:]
    closed_trades = [event for event in trade_events if event.get("type") == "CLOSE"]
    total_pnl_pct = sum(event.get("pnl_pct", 0.0) for event in closed_trades)

    lines = [
        title,
        f"report_time: {datetime.now(timezone.utc).isoformat()}",
        f"symbol: {latest_state.get('symbol')}",
        f"market_time: {latest_state.get('current_time')}",
        f"price: {latest_state.get('current_price')}",
        "",
        f"state: {result.get('state')}",
        f"signal: {result.get('signal')}",
        f"long_score: {result.get('long_score')}",
        f"short_score: {result.get('short_score')}",
        f"activity_score: {result.get('activity_score')}",
        f"atr: {_fmt(result.get('atr'))}",
        "",
        f"range_high: {_fmt(range_info.get('high'))}",
        f"range_low: {_fmt(range_info.get('low'))}",
        f"range_width_pct: {_fmt(range_info.get('width_pct'))}",
        "",
        f"upper_breakout_failure: {failures.get('upper_breakout_failure', 0)}",
        f"lower_breakdown_failure: {failures.get('lower_breakdown_failure', 0)}",
        "",
        f"trade_events: {len(trade_events)}",
        f"closed_trades: {len(closed_trades)}",
        f"total_pnl_pct: {_fmt(total_pnl_pct)}",
    ]

    if recent_trade_events:
        lines.append("")
        lines.append("recent_trades:")
        for event in recent_trade_events:
            lines.append(_format_trade_event(event))

    reasons = result.get("reasons", [])[-5:]
    if reasons:
        lines.append("")
        lines.append("recent_reasons:")
        lines.extend(f"- {reason}" for reason in reasons)

    return "\n".join(lines)


def send_status_report(message):
    try:
        from liquda.tall6 import send_telegram_message
    except Exception as exc:
        print(f"telegram sender import failed: {exc}")
        return False

    send_telegram_message(message)
    return True


def _read_last_jsonl_record(path):
    records = _read_jsonl_records(path)
    return records[-1] if records else None


def _read_jsonl_records(path):
    jsonl_path = Path(path)
    if not jsonl_path.exists():
        return []

    records = []
    with open(jsonl_path, encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _format_trade_event(event):
    event_type = event.get("type")
    side = event.get("side")

    if event_type == "OPEN":
        return f"- OPEN {side} entry={event.get('entry_price')} stop={_fmt(event.get('stop_price'))}"
    if event_type == "ADD":
        return f"- ADD {side} price={event.get('price')} total_size={event.get('total_size')}"
    if event_type == "CLOSE":
        return f"- CLOSE {side} exit={event.get('exit_price')} pnl={_fmt(event.get('pnl_pct'))}% reason={event.get('exit_reason')}"

    return f"- {event_type} {side}"


def _fmt(value):
    if value is None:
        return "None"
    if isinstance(value, float):
        return f"{value:.2f}"
    return value
