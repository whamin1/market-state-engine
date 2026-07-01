import argparse
import time
from datetime import datetime, timedelta, timezone

from market_state_engine import (
    BinanceFuturesFetcher,
    DailyCandleCache,
    LiveTrader,
    MarketStateEngine,
    MarketStateLogger,
    PaperTrader,
    load_liquidation_data,
)
from market_state_engine.env_loader import load_env_file
from market_state_engine.report import build_status_report, send_status_report


KST = timezone(timedelta(hours=9))


def main():
    args = parse_args()

    engine = MarketStateEngine()
    logger = None if args.no_state_log else MarketStateLogger()
    trader = build_trader(args, engine.config)
    fetcher = BinanceFuturesFetcher()
    daily_cache = DailyCandleCache(args.symbol)
    report_send_times = parse_send_times(args.report_times)
    sent_report_keys = set()
    recent_trade_events = []
    latest_snapshot = None
    error_notification_times = {}

    while True:
        try:
            latest_snapshot, trade_event = run_once(
                symbol=args.symbol,
                liquda_dir=args.liquda_dir,
                engine=engine,
                logger=logger,
                trader=trader,
                fetcher=fetcher,
                daily_cache=daily_cache,
            )
            if trade_event:
                recent_trade_events.append(trade_event)
                recent_trade_events = recent_trade_events[-10:]
                if isinstance(trader, LiveTrader):
                    trader.record_event(trade_event)
                maybe_send_trade_event(args, trade_event)

            maybe_send_report(args, report_send_times, sent_report_keys, latest_snapshot, recent_trade_events)
        except Exception as exc:
            print(f"live loop error: {exc}")
            maybe_send_loop_error(args, exc, error_notification_times)
            if should_halt_on_error(args, trader):
                maybe_send_halt_notice(args, exc)
                break

        if args.once:
            break

        time.sleep(args.interval_sec)


def run_once(symbol, liquda_dir, engine, logger, trader, fetcher, daily_cache):
    current_time = datetime.now(timezone.utc).isoformat()

    daily_cache.refresh_if_needed(
        now=current_time,
        fetcher=fetcher.fetch_completed_daily_candles,
    )

    completed_daily_candles = daily_cache.get_completed_candles()
    current_candle = fetcher.fetch_current_daily_candle(symbol)
    liquidation_data = load_liquidation_data(liquda_dir, symbol=symbol)

    result = engine.update(
        completed_daily_candles,
        current_candle=current_candle,
        current_time=current_time,
        liquidation_data=liquidation_data,
    )

    if logger:
        logger.log(result, symbol=symbol, current_candle=current_candle, current_time=current_time)

    trade_event = trader.update(result, current_candle=current_candle, current_time=current_time, symbol=symbol)
    position_snapshot = get_position_snapshot(trader, symbol, current_candle["close"])
    account_snapshot = trader.get_account_snapshot(current_candle["close"])

    snapshot = {
        "time": current_time,
        "symbol": symbol,
        "price": current_candle["close"],
        "mode": "LIVE" if isinstance(trader, LiveTrader) else "PAPER",
        "order_mode": get_order_mode(trader),
        "result": result,
        "trade_event": trade_event,
        "position": position_snapshot,
        "account": account_snapshot,
    }

    print(
        {
            "time": current_time,
            "symbol": symbol,
            "price": current_candle["close"],
            "long_score": result["long_score"],
            "short_score": result["short_score"],
            "activity_score": result["activity_score"],
            "state": result["state"],
            "signal": result["signal"],
            "order_mode": get_order_mode(trader),
            "trade_event": trade_event,
        }
    )
    return snapshot, trade_event


def get_position_snapshot(trader, symbol, current_price):
    if isinstance(trader, LiveTrader):
        return trader.get_position_snapshot(symbol, current_price)
    return trader.get_position_snapshot(current_price)


def get_order_mode(trader):
    if not isinstance(trader, LiveTrader):
        return "PAPER"
    if trader.enabled and not trader.dry_run:
        return "REAL_ORDER"
    return "DRY_RUN"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--liquda-dir", default="liquda")
    parser.add_argument("--interval-sec", type=int, default=60)
    parser.add_argument("--telegram-report", action="store_true")
    parser.add_argument("--telegram-trades", action="store_true")
    parser.add_argument("--report-times", default="09:10,21:10")
    parser.add_argument("--report-every-hour", action="store_true")
    parser.add_argument("--report-every-4-hours", action="store_true")
    parser.add_argument("--report-interval-hours", type=int, default=None)
    parser.add_argument("--report-minute", type=int, default=10)
    parser.add_argument("--report-title", default="Market State Check")
    parser.add_argument("--report-detail", choices=["compact", "full"], default="compact")
    parser.add_argument("--no-state-log", action="store_true")
    parser.add_argument("--no-trade-log", action="store_true")
    parser.add_argument("--paper-state-path", default="work/state/paper_trader_state.json")
    parser.add_argument("--live-state-path", default="work/state/live_trader_state.json")
    parser.add_argument("--live-trade-log-path", default="work/logs/live_trade_log.jsonl")
    parser.add_argument("--trader", choices=["paper", "live"], default="paper")
    parser.add_argument("--live-confirm", action="store_true")
    parser.add_argument("--halt-on-error", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def build_trader(args, config):
    if args.trader == "paper":
        return PaperTrader(
            config,
            trade_log_path=None if args.no_trade_log else "work/logs/trade_log.jsonl",
            state_path=args.paper_state_path,
        )

    env = load_env_file(".env")
    enabled = args.live_confirm and env.get("LIVE_TRADING_ENABLED", "").lower() == "true"
    return LiveTrader(
        config,
        dry_run=not enabled,
        enabled=enabled,
        state_path=args.live_state_path,
        trade_log_path=args.live_trade_log_path,
    )


def maybe_send_report(args, report_send_times, sent_report_keys, latest_snapshot, recent_trade_events):
    if not args.telegram_report:
        return

    now_kst = datetime.now(KST)
    current_hhmm = now_kst.strftime("%H:%M")
    current_hour_key = now_kst.strftime("%Y-%m-%d-%H")
    send_key = now_kst.strftime("%Y-%m-%d") + "-" + current_hhmm
    interval_hours = get_report_interval_hours(args)

    if interval_hours:
        if now_kst.minute < args.report_minute:
            return
        if now_kst.hour % interval_hours != 0:
            return
        send_key = current_hour_key
    elif current_hhmm not in report_send_times:
        return

    if send_key in sent_report_keys:
        return

    if latest_snapshot:
        message = build_live_status_report(latest_snapshot, recent_trade_events, args.report_title, args.report_detail)
    else:
        message = build_status_report(title=args.report_title)

    print(message)
    if send_status_report(message):
        sent_report_keys.add(send_key)


def maybe_send_trade_event(args, trade_event):
    if not args.telegram_trades:
        return

    send_status_report(format_trade_event_message(trade_event))


def maybe_send_loop_error(args, exc, error_notification_times):
    if not (args.telegram_report or args.telegram_trades):
        return

    error_key = str(exc)
    now = datetime.now(timezone.utc)
    previous_time = error_notification_times.get(error_key)
    if previous_time and (now - previous_time).total_seconds() < 1800:
        return

    message = "\n".join(
        [
            "Market State Error",
            f"time_kst: {now.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S')} KST",
            f"error: {error_key}",
        ]
    )
    if send_status_report(message):
        error_notification_times[error_key] = now


def should_halt_on_error(args, trader):
    if args.continue_on_error:
        return False
    if args.halt_on_error:
        return True
    return isinstance(trader, LiveTrader) and trader.enabled and not trader.dry_run


def maybe_send_halt_notice(args, exc):
    if not (args.telegram_report or args.telegram_trades):
        return

    now = datetime.now(timezone.utc)
    message = "\n".join(
        [
            "Market State Bot Halted",
            f"time_kst: {now.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S')} KST",
            "reason: unexpected error",
            f"error: {exc}",
        ]
    )
    send_status_report(message)


def get_report_interval_hours(args):
    if args.report_interval_hours:
        return args.report_interval_hours
    if args.report_every_4_hours:
        return 4
    if args.report_every_hour:
        return 1
    return None


def build_live_status_report(snapshot, recent_trade_events, title, report_detail="compact"):
    result = snapshot["result"]
    range_info = result.get("range") or {}
    position = snapshot.get("position") or {}
    account = snapshot.get("account") or {}
    mode = snapshot.get("mode", "PAPER")
    order_mode = snapshot.get("order_mode", "PAPER")
    decision = build_decision_snapshot(result, snapshot.get("trade_event"), position, mode)
    time_utc = parse_datetime(snapshot["time"])
    time_kst = time_utc.astimezone(KST)

    lines = [
        title,
        "",
        "mode:",
        f"- trader: {mode}",
        f"- order_mode: {order_mode}",
        "",
        f"price: {snapshot['price']}",
        "",
        "position:",
        f"- status: {position.get('status')}",
        f"- side: {position.get('side')}",
        f"- entry: {fmt(position.get('entry'))}",
        f"- stop: {fmt(position.get('stop'))}",
        f"- holding_time: {format_holding_time(position.get('entry_time'), time_utc)}",
        f"- unrealized_pnl_pct: {fmt_pct(position.get('unrealized_pnl_pct'))}",
        "",
        "decision:",
        f"- state: {decision['state']}",
        f"- new_signal: {decision['new_signal']}",
        f"- order_action: {decision['order_action']}",
        f"- reason: {decision['reason']}",
        "",
        "scores:",
        f"- long_score: {result.get('long_score')}",
        f"- short_score: {result.get('short_score')}",
        f"- activity_score: {result.get('activity_score')}",
        f"- atr: {fmt(result.get('atr'))}",
        "",
        "capital_status:",
        f"- start: {fmt(account.get('start_balance'))}",
        f"- current: {fmt(account.get('equity'))}",
        f"- change: {fmt_signed(account_change(account))}",
        f"- change_pct: {fmt_pct(account_change_pct(account))}",
        "",
        "range_15d:",
        f"- high: {fmt(range_info.get('high'))}",
        f"- low: {fmt(range_info.get('low'))}",
        f"- width_pct: {fmt(range_info.get('width_pct'))}",
        f"- breakout_level: {fmt(range_info.get('breakout_level'))}",
        f"- breakdown_level: {fmt(range_info.get('breakdown_level'))}",
    ]

    if recent_trade_events:
        lines.append("")
        lines.append("recent_trade_events:")
        lines.extend(format_trade_event_line(event) for event in recent_trade_events[-5:])

    lines.append("")
    lines.extend(build_score_reason_summary(result.get("reasons", []), report_detail))

    return "\n".join(lines)


def format_trade_event_message(event):
    return "\n".join(["Trade Event", format_trade_event_line(event)])


def format_trade_event_line(event):
    event_type = event.get("type")
    side = event.get("side")

    if event_type == "OPEN":
        return (
            f"- {format_event_time(event.get('entry_time', event.get('logged_at')))} "
            f"OPEN {side} entry={event.get('entry_price')} stop={fmt(event.get('stop_price'))}"
        )
    if event_type == "ADD":
        return (
            f"- {format_event_time(event.get('time', event.get('logged_at')))} "
            f"ADD {side} price={event.get('price')} total_size={event.get('total_size')}"
        )
    if event_type == "CLOSE":
        return (
            f"- {format_event_time(event.get('exit_time', event.get('logged_at')))} "
            f"CLOSE {side} exit={event.get('exit_price')} pnl={fmt(event.get('pnl_pct'))}% "
            f"reason={event.get('exit_reason')}"
        )
    if event_type == "LIVE_ORDER":
        order_mode = "DRY_RUN" if event.get("dry_run") else "REAL_ORDER"
        return (
            f"- {format_event_time(event.get('logged_at'))} "
            f"LIVE ENTRY {event.get('position_side', side)} mode={order_mode} status={event.get('status')} "
            f"price={event.get('price')} qty={event.get('quantity')} notional={event.get('notional_usdt')}"
        )
    if event_type == "LIVE_TRAILING_START":
        return (
            f"- {format_event_time(event.get('logged_at'))} "
            f"TRAILING START {side} price={event.get('price')} stop={fmt(event.get('stop_price'))}"
        )
    if event_type == "LIVE_TRAILING_STOP_HIT":
        return (
            f"- {format_event_time(event.get('logged_at'))} "
            f"TRAILING STOP HIT {side} price={event.get('price')} stop={fmt(event.get('stop_price'))}"
        )
    if event_type == "LIVE_POSITION_CLOSED":
        return (
            f"- {format_event_time(event.get('logged_at'))} "
            f"LIVE POSITION CLOSED {side} exit={event.get('exit_price')} reason={event.get('reason')}"
        )
    if event_type in ("LIVE_CLOSE", "LIVE_PARTIAL_CLOSE"):
        label = "LIVE PARTIAL CLOSE" if event_type == "LIVE_PARTIAL_CLOSE" else "LIVE CLOSE"
        return (
            f"- {format_event_time(event.get('logged_at'))} "
            f"{label} {side} exit={event.get('exit_price')} qty={event.get('quantity')} "
            f"pnl={fmt(event.get('pnl_pct'))}% reason={event.get('reason')} "
            f"status={event.get('status')}"
        )

    return f"- {format_event_time(event.get('logged_at'))} {event_type} {side}"


def fmt(value):
    if value is None:
        return "None"
    if isinstance(value, float):
        return f"{value:.2f}"
    return value


def fmt_pct(value):
    if value is None:
        return "None"
    return f"{value:.2f}%"


def fmt_signed(value):
    if value is None:
        return "None"
    if isinstance(value, float):
        return f"{value:+.2f}"
    return value


def account_change(account):
    start = account.get("start_balance")
    equity = account.get("equity")
    if start is None or equity is None:
        return None
    return equity - start


def account_change_pct(account):
    start = account.get("start_balance")
    change = account_change(account)
    if not start or change is None:
        return None
    return change / start * 100


def format_holding_time(entry_time, current_time):
    if not entry_time:
        return "None"

    entry_dt = parse_datetime(entry_time)
    elapsed = current_time - entry_dt
    total_minutes = max(int(elapsed.total_seconds() // 60), 0)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours}h {minutes}m"


def format_event_time(value):
    if not value:
        return "time=None"

    return parse_datetime(value).astimezone(KST).strftime("%m-%d %H:%M")


def parse_datetime(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_decision_snapshot(result, trade_event, position, mode="PAPER"):
    if trade_event:
        return {
            "state": result.get("state"),
            "new_signal": result.get("signal"),
            "order_action": trade_event.get("type"),
            "reason": format_trade_event_line(trade_event).lstrip("- "),
        }

    if position.get("status") == "OPEN":
        return {
            "state": result.get("state"),
            "new_signal": result.get("signal"),
            "order_action": "NONE",
            "reason": f"already in {mode.lower()} {position.get('side')} position",
        }

    return {
        "state": result.get("state"),
        "new_signal": result.get("signal"),
        "order_action": "NONE",
        "reason": f"no valid {mode.lower()} trade action",
    }


def build_score_reason_summary(reasons, report_detail):
    grouped = {
        "LONG": [],
        "SHORT": [],
        "ACTIVITY": [],
        "BLOCK": [],
    }

    for reason in reasons:
        score = extract_reason_score(reason)
        label = simplify_reason_label(reason)

        if "LONG" in reason and score is not None:
            grouped["LONG"].append((label, score))
        elif "SHORT" in reason and score is not None:
            grouped["SHORT"].append((label, score))
        elif reason.startswith("atr_score") and score is not None:
            grouped["ACTIVITY"].append((label, score))
        elif "forced to 0" in reason or "HOLD" in reason or "skipped" in reason:
            grouped["BLOCK"].append((label, None))

    lines = ["score_summary:"]
    for section in ("LONG", "SHORT", "ACTIVITY"):
        items = grouped[section]
        if not items:
            continue
        total = sum(score for _, score in items)
        lines.append(f"{section}: +{total}")
        for label, score in items:
            lines.append(f"- {label}: +{score}")

    if report_detail == "full" and grouped["BLOCK"]:
        lines.append("NOTES:")
        for label, _ in grouped["BLOCK"][-5:]:
            lines.append(f"- {label}")

    if len(lines) == 1:
        lines.append("- no score contribution")

    return lines


def simplify_reason_label(reason):
    if reason.startswith("price_position"):
        return reason.replace("price_position ", "price ")
    if reason.startswith("body_score"):
        return "body"
    if reason.startswith("volume_score"):
        return "volume"
    if reason.startswith("trend_continuity"):
        return "trend"
    if reason.startswith("range upper breakout"):
        return "range breakout"
    if reason.startswith("range lower breakdown"):
        return "range breakdown"
    if reason.startswith("range position"):
        return reason.split(" LONG ")[0].split(" SHORT ")[0].replace("range position ", "range ")
    if reason.startswith("atr_score"):
        return "atr"
    if reason.startswith("activity_direction"):
        return "activity direction"
    if reason.startswith("liquidation_score short_liq"):
        return "short liquidation"
    if reason.startswith("liquidation_score long_liq"):
        return "long liquidation"
    return reason


def extract_reason_score(reason):
    marker = "+"
    if marker not in reason:
        return None

    tail = reason.split(marker)[-1].split()[0]
    try:
        return int(tail)
    except ValueError:
        return None


def parse_send_times(value):
    return {part.strip() for part in value.split(",") if part.strip()}


if __name__ == "__main__":
    main()
