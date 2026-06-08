import csv
from pathlib import Path


def load_liquidation_data(data_dir, symbol=None):
    data_path = Path(data_dir)

    raw_events = []
    for csv_path in sorted(data_path.glob("liquidation_raw_*.csv")):
        raw_events.extend(_read_csv_rows(csv_path))

    hourly_history = []
    for csv_path in sorted(data_path.glob("liquidation_1h_*.csv")):
        hourly_history.extend(_read_csv_rows(csv_path))

    return {
        "symbol": symbol,
        "raw_events": raw_events,
        "hourly_history": hourly_history,
    }


def _read_csv_rows(csv_path):
    with open(csv_path, newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        return [_normalize_time_fields(row) for row in reader]


def _normalize_time_fields(row):
    normalized = dict(row)

    for key in ("event_time_kst", "window_start_kst", "window_end_kst"):
        value = normalized.get(key)
        if value and "+" not in value:
            normalized[key] = value.replace(" ", "T") + "+09:00"

    return normalized
