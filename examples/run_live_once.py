from datetime import datetime, timezone

from market_state_engine import (
    BinanceFuturesFetcher,
    DailyCandleCache,
    MarketStateEngine,
    MarketStateLogger,
    PaperTrader,
    load_liquidation_data,
)


def main():
    symbol = "BTCUSDT"

    engine = MarketStateEngine()
    logger = MarketStateLogger()
    trader = PaperTrader(engine.config)
    fetcher = BinanceFuturesFetcher()
    daily_cache = DailyCandleCache(symbol)

    daily_cache.refresh_if_needed(
        now=datetime.now(timezone.utc),
        fetcher=fetcher.fetch_completed_daily_candles,
    )

    completed_daily_candles = daily_cache.get_completed_candles()
    current_candle = fetcher.fetch_current_daily_candle(symbol)
    current_time = datetime.now(timezone.utc).isoformat()
    liquidation_data = load_liquidation_data("liquda", symbol=symbol)

    result = engine.update(
        completed_daily_candles,
        current_candle=current_candle,
        current_time=current_time,
        liquidation_data=liquidation_data,
    )

    logger.log(result, symbol=symbol, current_candle=current_candle, current_time=current_time)
    trade_event = trader.update(result, current_candle=current_candle, current_time=current_time, symbol=symbol)

    print(result)
    print(trade_event)


if __name__ == "__main__":
    main()
