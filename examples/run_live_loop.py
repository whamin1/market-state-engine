import argparse
import time
from datetime import datetime, timedelta, timezone

from market_state_engine import (
    BinanceFuturesFetcher,
    DailyCandleCache,
    MarketStateEngine,
    MarketStateLogger,
    PaperTrader,
    load_liquidation_data,
)
from market_state_engine.report import build_status_report, send_status_report


KST = timezone(timedelta(hours=9))


def main():
    args = parse_args()

    engine = MarketStateEngine()
    logger = None if args.no_state_log else MarketStateLogger()
    trader = PaperTrader(engine.config, trade_log_path=None if args.no_trade_log else "work/logs/trade_log.jsonl")
    fetcher = BinanceFuturesFetcher()
    daily_cache = DailyCandleCache(args.symbol)
    report_send_times = parse_send_times(args.report_times)
    sent_report_keys = set()
    recent_trade_events = []
    latest_snapshot = None

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
                maybe_send_trade_event(args, trade_event)

            maybe_send_report(args, report_send_times, sent_report_keys, latest_snapshot, recent_trade_events)
        except Exception as exc:
            print(f"live loop error: {exc}")

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

    snapshot = {
        "time": current_time,
        "symbol": symbol,
        "price": current_candle["close"],
        "result": result,
        "trade_event": trade_event,
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
            "trade_event": trade_event,
        }
    )
    return snapshot, trade_event


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--liquda-dir", default="liquda")
    parser.add_argument("--interval-sec", type=int, default=60)
    parser.add_argument("--telegram-report", action="store_true")
    parser.add_argument("--telegram-trades", action="store_true")
    parser.add_argument("--report-times", default="09:10,21:10")
    parser.add_argument("--report-every-hour", action="store_true")
    parser.add_argument("--report-minute", type=int, default=10)
    parser.add_argument("--report-title", default="Market State Check")
    parser.add_argument("--no-state-log", action="store_true")
    parser.add_argument("--no-trade-log", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def maybe_send_report(args, report_send_times, sent_report_keys, latest_snapshot, recent_trade_events):
    if not args.telegram_report:
        return

    now_kst = datetime.now(KST)
    current_hhmm = now_kst.strftime("%H:%M")
    current_hour_key = now_kst.strftime("%Y-%m-%d-%H")
    send_key = now_kst.strftime("%Y-%m-%d") + "-" + current_hhmm

    if args.report_every_hour:
        if now_kst.minute < args.report_minute:
            return
        send_key = current_hour_key
    elif current_hhmm not in report_send_times:
        return

    if send_key in sent_report_keys:
        return

    if latest_snapshot:
        message = build_live_status_report(latest_snapshot, recent_trade_events, args.report_title)
    else:
        message = build_status_report(title=args.report_title)

    print(message)
    send_status_report(message)
    sent_report_keys.add(send_key)


def maybe_send_trade_event(args, trade_event):
    if not args.telegram_trades:
        return

    send_status_report(format_trade_event_message(trade_event))


def build_live_status_report(snapshot, recent_trade_events, title):
    result = snapshot["result"]
    range_info = result.get("range") or {}
    failures = result.get("failure_counts") or {}

    lines = [
        title,
        f"time: {snapshot['time']}",
        f"symbol: {snapshot['symbol']}",
        f"price: {snapshot['price']}",
        "",
        f"state: {result.get('state')}",
        f"signal: {result.get('signal')}",
        f"long_score: {result.get('long_score')}",
        f"short_score: {result.get('short_score')}",
        f"activity_score: {result.get('activity_score')}",
        f"atr: {fmt(result.get('atr'))}",
        "",
        f"range_high: {fmt(range_info.get('high'))}",
        f"range_low: {fmt(range_info.get('low'))}",
        f"range_width_pct: {fmt(range_info.get('width_pct'))}",
        "",
        f"upper_breakout_failure: {failures.get('upper_breakout_failure', 0)}",
        f"lower_breakdown_failure: {failures.get('lower_breakdown_failure', 0)}",
    ]

    if recent_trade_events:
        lines.append("")
        lines.append("recent_trade_events:")
        lines.extend(format_trade_event_line(event) for event in recent_trade_events[-5:])

    reasons = result.get("reasons", [])[-5:]
    if reasons:
        lines.append("")
        lines.append("recent_reasons:")
        lines.extend(f"- {reason}" for reason in reasons)

    return "\n".join(lines)


def format_trade_event_message(event):
    return "\n".join(["Trade Event", format_trade_event_line(event)])


def format_trade_event_line(event):
    event_type = event.get("type")
    side = event.get("side")

    if event_type == "OPEN":
        return f"- OPEN {side} entry={event.get('entry_price')} stop={fmt(event.get('stop_price'))}"
    if event_type == "ADD":
        return f"- ADD {side} price={event.get('price')} total_size={event.get('total_size')}"
    if event_type == "CLOSE":
        return f"- CLOSE {side} exit={event.get('exit_price')} pnl={fmt(event.get('pnl_pct'))}% reason={event.get('exit_reason')}"

    return f"- {event_type} {side}"


def fmt(value):
    if value is None:
        return "None"
    if isinstance(value, float):
        return f"{value:.2f}"
    return value


def parse_send_times(value):
    return {part.strip() for part in value.split(",") if part.strip()}


if __name__ == "__main__":
    main()
