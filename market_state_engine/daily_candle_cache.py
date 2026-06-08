import csv
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


KST = timezone(timedelta(hours=9))


class DailyCandleCache:
    def __init__(self, symbol, csv_path=None, refresh_hour_kst=9, refresh_minute_kst=2):
        self.symbol = symbol
        self.csv_path = Path(csv_path or f"work/data/daily_candles_{symbol}.csv")
        self.refresh_hour_kst = refresh_hour_kst
        self.refresh_minute_kst = refresh_minute_kst
        self.completed_daily_candles = []
        self.last_refresh_date_kst = None

        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self):
        if not self.csv_path.exists():
            self.completed_daily_candles = []
            return []

        with open(self.csv_path, newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            self.completed_daily_candles = [self._parse_row(row) for row in reader]

        return self.get_completed_candles()

    def get_completed_candles(self):
        return list(self.completed_daily_candles)

    def save_atomic(self, candles):
        tmp_path = self.csv_path.with_suffix(self.csv_path.suffix + ".tmp")

        with open(tmp_path, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
            writer.writeheader()
            for candle in candles:
                writer.writerow(
                    {
                        "timestamp": candle["timestamp"],
                        "open": candle["open"],
                        "high": candle["high"],
                        "low": candle["low"],
                        "close": candle["close"],
                        "volume": candle["volume"],
                    }
                )

        loaded = self._load_from_path(tmp_path)
        self._validate_candles(loaded)
        os.replace(tmp_path, self.csv_path)
        self.completed_daily_candles = loaded
        return self.get_completed_candles()

    def refresh_if_needed(self, now=None, fetcher=None):
        now_kst = self._to_kst(now or datetime.now(timezone.utc))
        refresh_time = now_kst.replace(
            hour=self.refresh_hour_kst,
            minute=self.refresh_minute_kst,
            second=0,
            microsecond=0,
        )

        if now_kst < refresh_time:
            return False

        if self.last_refresh_date_kst == now_kst.date():
            return False

        if fetcher is None:
            return False

        new_candles = fetcher(self.symbol)
        self._validate_candles(new_candles)
        self.save_atomic(new_candles[-365:])
        self.last_refresh_date_kst = now_kst.date()
        return True

    def _load_from_path(self, path):
        with open(path, newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            return [self._parse_row(row) for row in reader]

    def _parse_row(self, row):
        return {
            "timestamp": row["timestamp"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }

    def _validate_candles(self, candles):
        if not candles:
            raise ValueError("daily candle cache cannot be empty")

        required_keys = {"timestamp", "open", "high", "low", "close", "volume"}
        for candle in candles:
            missing_keys = required_keys - set(candle)
            if missing_keys:
                raise ValueError(f"daily candle missing keys: {sorted(missing_keys)}")

    def _to_kst(self, value):
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        return value.astimezone(KST)
