import csv
from pathlib import Path

from market_state_engine import Backtester, load_ohlcv_csv


def main():
    csv_path = Path("work/data/sample_ohlcv.csv")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_sample_csv(csv_path)

    candles = load_ohlcv_csv(csv_path)
    summary = Backtester().run(candles, symbol="BTCUSDT")
    print(summary)


def write_sample_csv(csv_path):
    with open(csv_path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
        writer.writeheader()

        for day in range(1, 366):
            writer.writerow(
                {
                    "timestamp": f"2025-01-{((day - 1) % 28) + 1:02d}T00:00:00+00:00",
                    "open": 100,
                    "high": 105,
                    "low": 95,
                    "close": 101 + (day % 5),
                    "volume": 1000 + day,
                }
            )

        rows = [
            {"timestamp": "2026-01-01T00:00:00+00:00", "open": 100, "high": 112, "low": 99, "close": 110, "volume": 2000},
            {"timestamp": "2026-01-02T00:00:00+00:00", "open": 110, "high": 118, "low": 108, "close": 116, "volume": 2200},
            {"timestamp": "2026-01-03T00:00:00+00:00", "open": 116, "high": 117, "low": 101, "close": 103, "volume": 2300},
        ]
        writer.writerows(rows)


if __name__ == "__main__":
    main()
