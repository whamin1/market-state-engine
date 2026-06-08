from __future__ import annotations

import csv
import json
import os
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
import websocket

from tall6 import send_telegram_message


# =========================
# 설정
# =========================
SYMBOLS = [
    "ETHUSDT",
    "BTCUSDT",
    "XRPUSDT",
    "SOLUSDT"
]

MIN_USD_SIZE = 1000.0
REST_CACHE_TTL_SEC = 10
REST_TIMEOUT_SEC = 20
WS_PING_INTERVAL_SEC = 20
WS_PING_TIMEOUT_SEC = 10

KST = timezone(timedelta(hours=9))


# =========================
# 시간 / 파일명 유틸
# =========================
def now_kst() -> datetime:
    return datetime.now(KST)


def unix_to_kst(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=KST)


def to_iso_kst(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def date_str_from_dt_kst(dt: datetime) -> str:
    return dt.strftime("%Y_%m_%d")


def get_raw_csv_path(dt: datetime | None = None) -> str:
    dt = dt or now_kst()
    return f"liquidation_raw_{date_str_from_dt_kst(dt)}.csv"


def get_5m_csv_path(dt: datetime | None = None) -> str:
    dt = dt or now_kst()
    return f"liquidation_5m_{date_str_from_dt_kst(dt)}.csv"


def get_1h_csv_path(dt: datetime | None = None) -> str:
    dt = dt or now_kst()
    return f"liquidation_1h_{date_str_from_dt_kst(dt)}.csv"


def safe_float(x, default=0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def floor_window_start_kst(dt: datetime, step_sec: int) -> datetime:
    epoch_sec = int(dt.timestamp())
    floored = (epoch_sec // step_sec) * step_sec
    return datetime.fromtimestamp(floored, tz=KST)


# =========================
# CSV 헤더
# =========================
RAW_HEADER = [
    "event_time_kst",
    "symbol",
    "side",
    "liq_type",
    "avg_price",
    "qty",
    "usd_size",
    "oi",
    "funding_rate",
]

AGG_5M_HEADER = [
    "window_start_kst",
    "window_end_kst",
    "symbol",
    "short_liq_usd",
    "long_liq_usd",
    "short_liq_count",
    "long_liq_count",
    "max_short_liq_usd",
    "max_long_liq_usd",
    "oi_open_5m",
    "oi_close_5m",
    "oi_change_5m",
    "oi_change_pct_5m",
]

AGG_1H_HEADER = [
    "window_start_kst",
    "window_end_kst",
    "symbol",
    "short_liq_usd_1h",
    "long_liq_usd_1h",
    "short_liq_count_1h",
    "long_liq_count_1h",
    "max_short_liq_usd_1h",
    "max_long_liq_usd_1h",
    "oi_open_1h",
    "oi_close_1h",
    "oi_change_1h",
    "oi_change_pct_1h",
]


def ensure_csv_with_header(path: str, header: list[str]) -> None:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)


def ensure_daily_files_exist(dt: datetime | None = None) -> None:
    dt = dt or now_kst()
    ensure_csv_with_header(get_raw_csv_path(dt), RAW_HEADER)
    ensure_csv_with_header(get_5m_csv_path(dt), AGG_5M_HEADER)
    ensure_csv_with_header(get_1h_csv_path(dt), AGG_1H_HEADER)


# =========================
# REST 캐시
# =========================
class RestMetricCache:
    def __init__(self, ttl_sec: int = 10):
        self.ttl_sec = ttl_sec
        self._cache: dict[str, dict] = {}

    def get_metrics(self, symbol: str) -> tuple[float, float]:
        now_ts = time.time()
        cached = self._cache.get(symbol)

        if cached and (now_ts - cached["ts"] < self.ttl_sec):
            return cached["oi"], cached["funding"]

        # 기본값: REST 실패해도 청산 데이터 저장은 계속
        prev_oi = cached["oi"] if cached else 0.0
        prev_funding = cached["funding"] if cached else 0.0

        oi = self._fetch_open_interest(symbol, default=prev_oi)
        funding = self._fetch_funding(symbol, default=prev_funding)

        self._cache[symbol] = {
            "ts": now_ts,
            "oi": oi,
            "funding": funding,
        }
        return oi, funding

    def _fetch_open_interest(self, symbol: str, default: float = 0.0) -> float:
        url = "https://fapi.binance.com/fapi/v1/openInterest"
        try:
            r = requests.get(url, params={"symbol": symbol}, timeout=REST_TIMEOUT_SEC)
            r.raise_for_status()
            return safe_float(r.json().get("openInterest"), default)
        except Exception as e:
            send_telegram_message(f"⚠️ openInterest fetch failed: {symbol} / {e}")
            return default

    def _fetch_funding(self, symbol: str, default: float = 0.0) -> float:
        url = "https://fapi.binance.com/fapi/v1/premiumIndex"
        try:
            r = requests.get(url, params={"symbol": symbol}, timeout=REST_TIMEOUT_SEC)
            r.raise_for_status()
            return safe_float(r.json().get("lastFundingRate"), default)
        except Exception as e:
            send_telegram_message(f"⚠️ funding fetch failed: {symbol} / {e}")
            return default


# =========================
# 집계기
# =========================
class LiquidationAggregator:
    def __init__(self):
        self.current_5m_start: datetime | None = None
        self.current_1h_start: datetime | None = None
        self.current_5m = defaultdict(self._empty_bucket)
        self.current_1h = defaultdict(self._empty_bucket)

    @staticmethod
    def _empty_bucket() -> dict:
        return {
            "BUY_usd": 0.0,   # BUY = 숏 청산
            "SELL_usd": 0.0,  # SELL = 롱 청산
            "BUY_count": 0,
            "SELL_count": 0,
            "BUY_max": 0.0,
            "SELL_max": 0.0,
            "oi_open": None,
            "oi_close": None,
        }

    def process_event(self, event_dt_kst: datetime, symbol: str, side: str, usd_size: float, oi: float) -> None:
        self._roll_windows_if_needed(event_dt_kst)
        self._accumulate(self.current_5m[symbol], side, usd_size, oi)
        self._accumulate(self.current_1h[symbol], side, usd_size, oi)

    def _accumulate(self, bucket: dict, side: str, usd_size: float, oi: float) -> None:
        if bucket["oi_open"] is None:
            bucket["oi_open"] = oi

        bucket["oi_close"] = oi

        if side == "BUY":
            bucket["BUY_usd"] += usd_size
            bucket["BUY_count"] += 1
            bucket["BUY_max"] = max(bucket["BUY_max"], usd_size)
        elif side == "SELL":
            bucket["SELL_usd"] += usd_size
            bucket["SELL_count"] += 1
            bucket["SELL_max"] = max(bucket["SELL_max"], usd_size)

    def _roll_windows_if_needed(self, event_dt_kst: datetime) -> None:
        next_5m_start = floor_window_start_kst(event_dt_kst, 300)
        next_1h_start = floor_window_start_kst(event_dt_kst, 3600)

        if self.current_5m_start is None:
            self.current_5m_start = next_5m_start
        if self.current_1h_start is None:
            self.current_1h_start = next_1h_start

        while self.current_5m_start is not None and next_5m_start > self.current_5m_start:
            self._flush_5m_window(self.current_5m_start)
            self.current_5m_start += timedelta(minutes=5)

        while self.current_1h_start is not None and next_1h_start > self.current_1h_start:
            self._flush_1h_window(self.current_1h_start)
            self.current_1h_start += timedelta(hours=1)

    def flush_all(self) -> None:
        if self.current_5m_start is not None:
            self._flush_5m_window(self.current_5m_start)
        if self.current_1h_start is not None:
            self._flush_1h_window(self.current_1h_start)

    def _flush_5m_window(self, start_dt: datetime) -> None:
        if not self.current_5m:
            return

        ensure_daily_files_exist(start_dt)
        path = get_5m_csv_path(start_dt)
        end_dt = start_dt + timedelta(minutes=5)

        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            for symbol, b in sorted(self.current_5m.items()):
                oi_open = b["oi_open"] or 0.0
                oi_close = b["oi_close"] or 0.0
                oi_change = oi_close - oi_open
                oi_change_pct = (oi_change / oi_open) if oi_open > 0 else 0.0

                writer.writerow([
                    to_iso_kst(start_dt),
                    to_iso_kst(end_dt),
                    symbol,
                    round(b["BUY_usd"], 6),
                    round(b["SELL_usd"], 6),
                    b["BUY_count"],
                    b["SELL_count"],
                    round(b["BUY_max"], 6),
                    round(b["SELL_max"], 6),
                    round(oi_open, 8),
                    round(oi_close, 8),
                    round(oi_change, 8),
                    round(oi_change_pct, 8),
                ])

        self.current_5m = defaultdict(self._empty_bucket)

    def _flush_1h_window(self, start_dt: datetime) -> None:
        if not self.current_1h:
            return

        ensure_daily_files_exist(start_dt)
        path = get_1h_csv_path(start_dt)
        end_dt = start_dt + timedelta(hours=1)

        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            for symbol, b in sorted(self.current_1h.items()):
                oi_open = b["oi_open"] or 0.0
                oi_close = b["oi_close"] or 0.0
                oi_change = oi_close - oi_open
                oi_change_pct = (oi_change / oi_open) if oi_open > 0 else 0.0
                writer.writerow([
                    to_iso_kst(start_dt),
                    to_iso_kst(end_dt),
                    symbol,
                    round(b["BUY_usd"], 6),
                    round(b["SELL_usd"], 6),
                    b["BUY_count"],
                    b["SELL_count"],
                    round(b["BUY_max"], 6),
                    round(b["SELL_max"], 6),
                    round(oi_open, 8),
                    round(oi_close, 8),
                    round(oi_change, 8),
                    round(oi_change_pct, 8),
                ])

        self.current_1h = defaultdict(self._empty_bucket)


# =========================
# raw 저장
# =========================
def append_raw_row(
    event_dt_kst: datetime,
    symbol: str,
    side: str,
    liq_type: str,
    avg_price: float,
    qty: float,
    usd_size: float,
    oi: float,
    funding_rate: float,
) -> None:
    ensure_daily_files_exist(event_dt_kst)
    path = get_raw_csv_path(event_dt_kst)

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            to_iso_kst(event_dt_kst),
            symbol,
            side,
            liq_type,
            round(avg_price, 8),
            round(qty, 8),
            round(usd_size, 8),
            round(oi, 8),
            round(funding_rate, 10),
        ])


# =========================
# 메인 수집기
# =========================
class LiquidationCollector:
    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self.metric_cache = RestMetricCache(ttl_sec=REST_CACHE_TTL_SEC)
        self.aggregator = LiquidationAggregator()
        self.ws = None
        self.running = True

    def build_stream_url(self) -> str:
        streams = "/".join([f"{s.lower()}@forceOrder" for s in self.symbols])
        return f"wss://fstream.binance.com/market/stream?streams={streams}"

    def on_open(self, ws):
        msg = (
            f"🚀 liquidation collector started\n"
            f"시간: {to_iso_kst(now_kst())}\n"
            f"심볼: {', '.join(self.symbols)}\n"
            f"URL: {self.build_stream_url()}"
        )
        send_telegram_message(msg)

    def on_error(self, ws, error):
        msg = f"websocket error: {error}"
        send_telegram_message(msg)

    def on_close(self, ws, close_status_code, close_msg):
        msg = f"websocket closed: {close_status_code} / {close_msg}"
        send_telegram_message(msg)

    def on_message(self, ws, message: str):
        try:
            data = json.loads(message)
            order = data.get("data", {}).get("o", {})

            symbol = order.get("s")
            side = order.get("S")  # BUY=숏 청산, SELL=롱 청산
            if not symbol or side not in ("BUY", "SELL"):
                return

            price = safe_float(order.get("p"))
            avg_price = safe_float(order.get("ap"))
            qty = safe_float(order.get("q"))

            use_price = price if price > 0 else avg_price
            usd_size = use_price * qty

            event_ms = data.get("data", {}).get("E") or data.get("E")
            event_ts = (event_ms / 1000.0) if event_ms else time.time()
            event_dt_kst = unix_to_kst(event_ts)

            if usd_size < MIN_USD_SIZE:
                return

            oi, funding_rate = self.metric_cache.get_metrics(symbol)
            liq_type = "short_liquidation" if side == "BUY" else "long_liquidation"

            append_raw_row(
                event_dt_kst=event_dt_kst,
                symbol=symbol,
                side=side,
                liq_type=liq_type,
                avg_price=avg_price,
                qty=qty,
                usd_size=usd_size,
                oi=oi,
                funding_rate=funding_rate,
            )

            self.aggregator.process_event(
                event_dt_kst=event_dt_kst,
                symbol=symbol,
                side=side,
                usd_size=usd_size,
                oi=oi,
            )

        except Exception as e:
            msg = f"on_message parse/process error: {e}"
            send_telegram_message(msg)

    def run(self):
        ensure_daily_files_exist()

        while self.running:
            try:
                url = self.build_stream_url()

                self.ws = websocket.WebSocketApp(
                    url,
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                )

                self.ws.run_forever(
                    ping_interval=WS_PING_INTERVAL_SEC,
                    ping_timeout=WS_PING_TIMEOUT_SEC,
                )

            except Exception as e:
                send_telegram_message(f"❌ liquidation collector run error: {e}")

            if self.running:
                send_telegram_message("🔁 liquidation collector reconnect in 5 sec")
                time.sleep(5)

    def shutdown(self):
        self.running = False

        msg = f"🛑 liquidation collector stopped\n시간: {to_iso_kst(now_kst())}"
        send_telegram_message(msg)

        self.aggregator.flush_all()

        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass


collector: LiquidationCollector | None = None


def handle_exit(signum, frame):
    global collector
    if collector:
        collector.shutdown()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    collector = LiquidationCollector(SYMBOLS)
    collector.run()