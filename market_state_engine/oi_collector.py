"""Independent minute OI observations; never used for order decisions."""

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
import math
from pathlib import Path
import sqlite3
import threading
import time
from urllib.request import urlopen

from .oi_report import summarize_oi

LOGGER = logging.getLogger(__name__)
CREATE_OI_TABLE = """CREATE TABLE IF NOT EXISTS oi_snapshots (
    timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
    collected_at TEXT NOT NULL, observed_at TEXT, open_interest REAL,
    status TEXT NOT NULL, error TEXT, source TEXT NOT NULL,
    PRIMARY KEY (timestamp, symbol))"""


def fetch_btc_oi():
    with urlopen('https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT', timeout=5) as response:
        return json.loads(response.read().decode('utf-8'))


class OICollector:
    def __init__(self, db_path, fetch=None):
        self.db_path = Path(db_path)
        self.fetch = fetch or fetch_btc_oi
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.summary = self._empty()

    @staticmethod
    def _empty(status='waiting'):
        return {'source': 'binance_oi_1m', 'fetch_success_verified': False,
                'status': status, 'oi': None, 'observed_at': None, 'changes': {}}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name='btc-oi-recorder', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=6)

    def _run(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.sample_once()
            except Exception:
                # A collector failure must never stop the trading thread.
                LOGGER.exception('OI background collection failed')
            self.stop_event.wait(max(1, 60 - (time.monotonic() - started)))

    def report(self, now=None):
        now = now or datetime.now(timezone.utc)
        with self.lock:
            result = deepcopy(self.summary)
        if result.get('observed_at'):
            age = (now - datetime.fromisoformat(result['observed_at'])).total_seconds()
            if not 0 <= age <= 120:
                result.update(status='stale', changes={})
        return result

    def sample_once(self, now=None):
        received = now or datetime.now(timezone.utc)
        observed = None
        value = None
        error = None
        try:
            payload = self.fetch()
            received = now or datetime.now(timezone.utc)
            value = float(payload['openInterest'])
            observed = datetime.fromtimestamp(int(payload['time']) / 1000, timezone.utc)
            if payload.get('symbol') != 'BTCUSDT' or not math.isfinite(value) or value <= 0:
                raise ValueError('Invalid BTCUSDT OI response')
            if not 0 <= (received-observed).total_seconds() <= 120:
                raise ValueError('OI response timestamp is stale or in the future')
        except Exception as exc:
            value = None
            observed = None
            error = f'{type(exc).__name__}: {exc}'[:300]
            LOGGER.warning('OI fetch failed: %s', error)
        status = 'ok' if error is None else 'error'
        minute = received.replace(second=0, microsecond=0).isoformat()
        summary = self._empty(status)
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.db_path, timeout=0.2)) as c:
                c.execute('PRAGMA journal_mode=WAL')
                c.execute(CREATE_OI_TABLE)
                c.execute('''INSERT INTO oi_snapshots VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(timestamp,symbol) DO UPDATE SET
                    collected_at=excluded.collected_at, observed_at=excluded.observed_at,
                    open_interest=excluded.open_interest, status=excluded.status, error=excluded.error
                    WHERE oi_snapshots.status!='ok' AND excluded.status='ok' ''',
                    (minute, 'BTCUSDT', received.isoformat(), observed.isoformat() if observed else None,
                     value, status, error, 'binance_open_interest'))
                c.commit()
                if status == 'ok':
                    rows = c.execute('''SELECT observed_at,open_interest FROM oi_snapshots
                        WHERE timestamp>=? AND timestamp<=? AND symbol='BTCUSDT' AND status='ok'
                        ORDER BY timestamp''', ((received-timedelta(minutes=65)).isoformat(), received.isoformat())).fetchall()
                    summary = summarize_oi([{'symbol': 'BTCUSDT', 'event_time_kst': at, 'oi': oi}
                                            for at, oi in rows], 'BTCUSDT', received)
                    summary.update(source='binance_oi_1m', fetch_success_verified=True)
                summary['storage_status'] = 'ok'
        except Exception as exc:
            LOGGER.warning('OI storage failed: %s', exc)
            summary = self._empty('storage_error')
            summary['storage_status'] = 'error'
        with self.lock:
            self.summary = summary
        return summary
