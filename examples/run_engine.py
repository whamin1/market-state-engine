from market_state_engine import (
    DailyCandleCache,
    MarketStateEngine,
    MarketStateLogger,
    PaperTrader,
    load_liquidation_data,
)


def main():
    engine = MarketStateEngine()
    logger = MarketStateLogger()
    trader = PaperTrader(engine.config)
    daily_cache = DailyCandleCache("BTCUSDT")

    seed_daily_candles = []
    for day in range(1, 366):
        seed_daily_candles.append(
            {
                "timestamp": f"2025-day-{day}",
                "open": 100,
                "high": 105,
                "low": 95,
                "close": 101 + (day % 5),
                "volume": 1000 + day,
            }
        )

    daily_cache.save_atomic(seed_daily_candles)
    completed_daily_candles = daily_cache.get_completed_candles()

    current_candle = {
        "timestamp": "2026-06-07T00:00:00+00:00",
        "open": 100,
        "high": 112,
        "low": 99,
        "close": 110,
        "volume": 600,
    }

    liquidation_data = load_liquidation_data("liquda", symbol="BTCUSDT")

    current_time = "2026-04-27T19:00:00+09:00"
    result = engine.update(
        completed_daily_candles,
        current_candle=current_candle,
        current_time=current_time,
        liquidation_data=liquidation_data,
    )

    logger.log(
        result,
        symbol="BTCUSDT",
        current_candle=current_candle,
        current_time=current_time,
    )

    trade_event = trader.update(
        result,
        current_candle=current_candle,
        current_time=current_time,
        symbol="BTCUSDT",
    )

    print(result)
    print(trade_event)


if __name__ == "__main__":
    main()
