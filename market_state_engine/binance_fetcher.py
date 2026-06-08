import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import urlopen


BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"


class BinanceFuturesFetcher:
    def __init__(self, base_url=BINANCE_FUTURES_BASE_URL, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def fetch_current_daily_candle(self, symbol):
        candles = self.fetch_klines(symbol=symbol, interval="1d", limit=1)
        return candles[-1]

    def fetch_completed_daily_candles(self, symbol, limit=365):
        candles = self.fetch_klines(symbol=symbol, interval="1d", limit=limit + 1)
        return candles[:-1][-limit:]

    def fetch_klines(self, symbol, interval="1d", limit=365):
        params = urlencode({"symbol": symbol, "interval": interval, "limit": limit})
        url = f"{self.base_url}/fapi/v1/klines?{params}"

        with urlopen(url, timeout=self.timeout) as response:
            raw_klines = json.loads(response.read().decode("utf-8"))

        return [parse_binance_kline(kline) for kline in raw_klines]


def parse_binance_kline(kline):
    open_time_ms = int(kline[0])
    return {
        "timestamp": datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).isoformat(),
        "open": float(kline[1]),
        "high": float(kline[2]),
        "low": float(kline[3]),
        "close": float(kline[4]),
        "volume": float(kline[5]),
    }
