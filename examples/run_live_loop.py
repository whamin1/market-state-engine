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
    logger = MarketStateLogger()
    trader = PaperTrader(engine.config)
    fetcher = BinanceFuturesFetcher()
    daily_cache = DailyCandleCache(args.symbol)
    report_send_times = parse_send_times(args.report_times)
    sent_report_keys = set()

    while True:
        try:
            run_once(
                symbol=args.symbol,
                liquda_dir=args.liquda_dir,
                engine=engine,
                logger=logger,
                trader=trader,
                fetcher=fetcher,
                daily_cache=daily_cache,
            )
            maybe_send_report(args, report_send_times, sent_report_keys)
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

    logger.log(result, symbol=symbol, current_candle=current_candle, current_time=current_time)
    trade_event = trader.update(result, current_candle=current_candle, current_time=current_time, symbol=symbol)

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


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--liquda-dir", default="liquda")
    parser.add_argument("--interval-sec", type=int, default=60)
    parser.add_argument("--telegram-report", action="store_true")
    parser.add_argument("--report-times", default="09:10,21:10")
    parser.add_argument("--report-title", default="Market State Check")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def maybe_send_report(args, report_send_times, sent_report_keys):
    if not args.telegram_report:
        return

    now_kst = datetime.now(KST)
    current_hhmm = now_kst.strftime("%H:%M")
    send_key = now_kst.strftime("%Y-%m-%d") + "-" + current_hhmm

    if current_hhmm not in report_send_times:
        return

    if send_key in sent_report_keys:
        return

    message = build_status_report(title=args.report_title)
    print(message)
    send_status_report(message)
    sent_report_keys.add(send_key)


def parse_send_times(value):
    return {part.strip() for part in value.split(",") if part.strip()}


if __name__ == "__main__":
    main()
