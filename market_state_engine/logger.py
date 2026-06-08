import json
from datetime import datetime, timezone
from pathlib import Path


class MarketStateLogger:
    def __init__(self, log_path="work/logs/market_state_log.jsonl"):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, result, symbol=None, current_candle=None, current_time=None):
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "current_time": current_time,
            "current_price": current_candle.get("close") if current_candle else None,
            "result": result,
        }

        with open(self.log_path, "a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

        return record
