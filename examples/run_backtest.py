from market_state_engine import Backtester


def main():
    candles = []

    for day in range(1, 366):
        candles.append(
            {
                "timestamp": f"2025-01-{((day - 1) % 28) + 1:02d}T00:00:00+00:00",
                "open": 100,
                "high": 105,
                "low": 95,
                "close": 101 + (day % 5),
                "volume": 1000 + day,
            }
        )

    test_candles = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "open": 100, "high": 112, "low": 99, "close": 110, "volume": 2000},
        {"timestamp": "2026-01-02T00:00:00+00:00", "open": 110, "high": 118, "low": 108, "close": 116, "volume": 2200},
        {"timestamp": "2026-01-03T00:00:00+00:00", "open": 116, "high": 117, "low": 101, "close": 103, "volume": 2300},
    ]
    candles.extend(test_candles)

    backtester = Backtester()
    summary = backtester.run(candles, symbol="BTCUSDT")
    print(summary)


if __name__ == "__main__":
    main()
