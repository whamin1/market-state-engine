import json
from datetime import datetime, timezone
from pathlib import Path


class PaperTrader:
    def __init__(self, config, trade_log_path="work/logs/trade_log.jsonl"):
        self.config = config
        self.trade_log_path = Path(trade_log_path) if trade_log_path else None
        if self.trade_log_path:
            self.trade_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.position = None

    def update(self, result, current_candle, current_time=None, symbol=None):
        current_price = current_candle["close"]
        atr = result.get("atr")

        if self.position is None:
            if result["signal"] == "ENTER_LONG":
                return self._open_position("LONG", current_price, atr, current_time, symbol, result)
            if result["signal"] == "ENTER_SHORT":
                return self._open_position("SHORT", current_price, atr, current_time, symbol, result)
            return None

        self._update_trailing_stop(current_price, atr)

        stop_event = self._check_stop_loss(current_price, current_time, symbol)
        if stop_event:
            return stop_event

        add_event = self._check_add_entry(result, current_price, current_time, symbol)
        if add_event:
            return add_event

        if self.position["side"] == "LONG" and result["signal"] == "ENTER_SHORT":
            partial_event = self._check_partial_take_profit(current_price, current_time, symbol, "opposite SHORT signal", result)
            if partial_event:
                return partial_event
            return self._close_position(current_price, current_time, symbol, "opposite SHORT signal", result)

        if self.position["side"] == "SHORT" and result["signal"] == "ENTER_LONG":
            partial_event = self._check_partial_take_profit(current_price, current_time, symbol, "opposite LONG signal", result)
            if partial_event:
                return partial_event
            return self._close_position(current_price, current_time, symbol, "opposite LONG signal", result)

        return None

    def _open_position(self, side, entry_price, atr, current_time, symbol, result):
        stop_price = None
        if atr is not None:
            if side == "LONG":
                stop_price = entry_price - atr * self.config.atr_stop_multiplier
            else:
                stop_price = entry_price + atr * self.config.atr_stop_multiplier

        self.position = {
            "symbol": symbol,
            "side": side,
            "entry_time": current_time,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "trailing_stop_price": None,
            "trailing_active": False,
            "best_price": entry_price,
            "remaining_size": 1.0,
            "total_size": 1.0,
            "add_count": 0,
            "entry_score": result["long_score"] if side == "LONG" else result["short_score"],
            "last_add_score": result["long_score"] if side == "LONG" else result["short_score"],
            "partial_taken": False,
            "entry_signal": result["signal"],
            "entry_reasons": result["reasons"],
        }

        event = {
            "type": "OPEN",
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": side,
            "entry_time": current_time,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "remaining_size": self.position["remaining_size"],
        }
        self._write_event(event)
        return event

    def _check_add_entry(self, result, current_price, current_time, symbol):
        if self.position["add_count"] >= self.config.max_add_entries:
            return None

        if self.position["side"] == "LONG":
            current_score = result["long_score"]
            required_signal = "ENTER_LONG"
        else:
            current_score = result["short_score"]
            required_signal = "ENTER_SHORT"

        if result["signal"] != required_signal:
            return None

        if current_score < self.position["last_add_score"] + self.config.add_entry_score_increase:
            return None

        self.position["add_count"] += 1
        self.position["last_add_score"] = current_score
        self.position["remaining_size"] += self.config.add_entry_size
        self.position["total_size"] += self.config.add_entry_size

        event = {
            "type": "ADD",
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol or self.position["symbol"],
            "side": self.position["side"],
            "time": current_time,
            "price": current_price,
            "score": current_score,
            "add_size": self.config.add_entry_size,
            "total_size": self.position["total_size"],
            "remaining_size": self.position["remaining_size"],
            "reason": f"score increased by {self.config.add_entry_score_increase}",
        }
        self._write_event(event)
        return event

    def _close_position(self, exit_price, current_time, symbol, exit_reason, result=None, close_size=None):
        position = self.position
        close_size = position["remaining_size"] if close_size is None else close_size
        pnl_pct = self._calculate_pnl_pct(position["side"], position["entry_price"], exit_price)

        event = {
            "type": "CLOSE",
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol or position["symbol"],
            "side": position["side"],
            "entry_time": position["entry_time"],
            "entry_price": position["entry_price"],
            "exit_time": current_time,
            "exit_price": exit_price,
            "pnl_pct": pnl_pct,
            "close_size": close_size,
            "remaining_size_after": max(position["remaining_size"] - close_size, 0.0),
            "exit_reason": exit_reason,
            "entry_reasons": position["entry_reasons"],
            "exit_reasons": result["reasons"] if result else [],
        }

        position["remaining_size"] = max(position["remaining_size"] - close_size, 0.0)
        if position["remaining_size"] == 0:
            self.position = None

        self._write_event(event)
        return event

    def _check_stop_loss(self, current_price, current_time, symbol):
        stop_price = self.position.get("trailing_stop_price") or self.position.get("stop_price")
        if stop_price is None:
            return None

        if self.position["side"] == "LONG" and current_price <= stop_price:
            reason = "LONG trailing stop" if self.position.get("trailing_active") else "LONG stop loss"
            return self._close_position(current_price, current_time, symbol, reason)

        if self.position["side"] == "SHORT" and current_price >= stop_price:
            reason = "SHORT trailing stop" if self.position.get("trailing_active") else "SHORT stop loss"
            return self._close_position(current_price, current_time, symbol, reason)

        return None

    def _check_partial_take_profit(self, current_price, current_time, symbol, exit_reason, result):
        if self.position["partial_taken"]:
            return None

        pnl_pct = self._calculate_pnl_pct(self.position["side"], self.position["entry_price"], current_price)
        if pnl_pct < self.config.partial_take_profit_pct:
            return None

        close_size = self.position["remaining_size"] * self.config.partial_take_profit_size
        self.position["partial_taken"] = True
        return self._close_position(current_price, current_time, symbol, f"partial take profit: {exit_reason}", result, close_size)

    def _update_trailing_stop(self, current_price, atr):
        if atr is None:
            return

        pnl_pct = self._calculate_pnl_pct(self.position["side"], self.position["entry_price"], current_price)
        if pnl_pct < self.config.trailing_take_profit_pct:
            return

        self.position["trailing_active"] = True

        if self.position["side"] == "LONG":
            self.position["best_price"] = max(self.position["best_price"], current_price)
            new_stop = self.position["best_price"] - atr * self.config.trailing_atr_multiplier
            current_stop = self.position.get("trailing_stop_price")
            self.position["trailing_stop_price"] = new_stop if current_stop is None else max(current_stop, new_stop)
        else:
            self.position["best_price"] = min(self.position["best_price"], current_price)
            new_stop = self.position["best_price"] + atr * self.config.trailing_atr_multiplier
            current_stop = self.position.get("trailing_stop_price")
            self.position["trailing_stop_price"] = new_stop if current_stop is None else min(current_stop, new_stop)

    def _extract_atr(self, result):
        return None

    def _calculate_pnl_pct(self, side, entry_price, exit_price):
        if side == "LONG":
            return (exit_price - entry_price) / entry_price * 100
        return (entry_price - exit_price) / entry_price * 100

    def _write_event(self, event):
        if self.trade_log_path is None:
            return

        with open(self.trade_log_path, "a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")
