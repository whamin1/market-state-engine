import json
from datetime import datetime, timezone
from pathlib import Path


class PaperTrader:
    def __init__(self, config, trade_log_path="work/logs/trade_log.jsonl", state_path="work/state/paper_trader_state.json"):
        self.config = config
        self.trade_log_path = Path(trade_log_path) if trade_log_path else None
        if self.trade_log_path:
            self.trade_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path = Path(state_path) if state_path else None
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.position = None
        self.realized_pnl = 0.0
        self.last_profit_exit = None
        self._load_state()

    def update(self, result, current_candle, current_time=None, symbol=None):
        current_price = current_candle["close"]
        atr = result.get("atr")

        if self.position is None:
            if result["signal"] == "ENTER_LONG":
                if self._is_entry_blocked_by_profit_reentry("LONG", result, current_time):
                    return None
                return self._open_position("LONG", current_price, atr, current_time, symbol, result)
            if result["signal"] == "ENTER_SHORT":
                if self._is_entry_blocked_by_profit_reentry("SHORT", result, current_time):
                    return None
                return self._open_position("SHORT", current_price, atr, current_time, symbol, result)
            return None

        self._update_peak_profit(current_price)
        self._save_state()

        stop_event = self._check_stop_loss(current_price, current_time, symbol, result)
        if stop_event:
            return stop_event

        protection_event = self._check_small_profit_protection(current_price, current_time, symbol, result)
        if protection_event:
            return protection_event

        self._update_trailing_stop(current_price, atr)
        self._save_state()

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
            "peak_profit_pct": 0.0,
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
        self._save_state()
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
        self._save_state()
        return event

    def _close_position(self, exit_price, current_time, symbol, exit_reason, result=None, close_size=None):
        position = self.position
        close_size = position["remaining_size"] if close_size is None else close_size
        pnl_pct = self._calculate_pnl_pct(position["side"], position["entry_price"], exit_price)
        realized_pnl_amount = self.config.paper_start_balance * close_size * pnl_pct / 100
        self.realized_pnl += realized_pnl_amount

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
            "realized_pnl_amount": realized_pnl_amount,
            "paper_balance_after": self.config.paper_start_balance + self.realized_pnl,
            "close_size": close_size,
            "remaining_size_after": max(position["remaining_size"] - close_size, 0.0),
            "exit_reason": exit_reason,
            "entry_reasons": position["entry_reasons"],
            "exit_reasons": result["reasons"] if result else [],
        }

        position["remaining_size"] = max(position["remaining_size"] - close_size, 0.0)
        if position["remaining_size"] == 0:
            self._record_profit_exit_if_needed(position, event, result)
            self.position = None

        self._write_event(event)
        self._save_state()
        return event

    def _record_profit_exit_if_needed(self, position, event, result):
        pnl_pct = event.get("pnl_pct", 0.0)
        if pnl_pct <= 0:
            return

        side = position["side"]
        exit_score = None
        if result:
            exit_score = result["long_score"] if side == "LONG" else result["short_score"]
        if exit_score is None:
            exit_score = position.get("last_add_score") or position.get("entry_score")

        self.last_profit_exit = {
            "side": side,
            "exit_time": event.get("exit_time") or event.get("logged_at"),
            "exit_score": exit_score,
            "pnl_pct": pnl_pct,
            "reason": event.get("exit_reason"),
        }

    def _is_entry_blocked_by_profit_reentry(self, side, result, current_time):
        if not self.last_profit_exit or self.last_profit_exit.get("side") != side:
            return False

        exit_time = self.last_profit_exit.get("exit_time")
        if not exit_time:
            return False

        elapsed_minutes = self._elapsed_minutes(exit_time, current_time)
        if elapsed_minutes is None:
            return False
        if elapsed_minutes >= self.config.profit_reentry_score_memory_minutes:
            self.last_profit_exit = None
            self._save_state()
            return False
        if elapsed_minutes < self.config.profit_reentry_cooldown_minutes:
            return True

        exit_score = self.last_profit_exit.get("exit_score")
        if exit_score is None:
            return False

        current_score = result["long_score"] if side == "LONG" else result["short_score"]
        return current_score < exit_score + self.config.profit_reentry_score_increase

    def _elapsed_minutes(self, start_time, end_time):
        try:
            start = self._parse_datetime(start_time)
            end = self._parse_datetime(end_time) if end_time else datetime.now(timezone.utc)
        except ValueError:
            return None
        return (end - start).total_seconds() / 60

    def _parse_datetime(self, value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _check_stop_loss(self, current_price, current_time, symbol, result=None):
        stop_price = self.position.get("trailing_stop_price") or self.position.get("stop_price")
        if stop_price is None:
            return None

        if self.position["side"] == "LONG" and current_price <= stop_price:
            reason = "LONG trailing stop" if self.position.get("trailing_active") else "LONG stop loss"
            return self._close_position(current_price, current_time, symbol, reason, result)

        if self.position["side"] == "SHORT" and current_price >= stop_price:
            reason = "SHORT trailing stop" if self.position.get("trailing_active") else "SHORT stop loss"
            return self._close_position(current_price, current_time, symbol, reason, result)

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

    def _update_peak_profit(self, current_price):
        pnl_pct = self._calculate_pnl_pct(self.position["side"], self.position["entry_price"], current_price)
        self.position["peak_profit_pct"] = max(self.position.get("peak_profit_pct", 0.0), pnl_pct)

    def _check_small_profit_protection(self, current_price, current_time, symbol, result):
        if not self.config.small_profit_protection_enabled:
            return None

        pnl_pct = self._calculate_pnl_pct(self.position["side"], self.position["entry_price"], current_price)
        peak_profit = self.position.get("peak_profit_pct", 0.0)
        if peak_profit < self.config.small_profit_protection_min_peak_pct:
            return None
        if peak_profit >= self.config.small_profit_protection_max_pct:
            return None
        if pnl_pct <= 0:
            return None

        trigger_pct = peak_profit * self.config.small_profit_protection_retrace_ratio
        if pnl_pct > trigger_pct:
            return None

        reason = f"small profit protection: peak {peak_profit:.2f}% -> current {pnl_pct:.2f}%"
        return self._close_position(current_price, current_time, symbol, reason, result)

    def _update_trailing_stop(self, current_price, atr):
        if atr is None and not self.config.trailing_use_percent_distance:
            return

        pnl_pct = self._calculate_pnl_pct(self.position["side"], self.position["entry_price"], current_price)
        if pnl_pct < self.config.trailing_take_profit_pct:
            return

        self.position["trailing_active"] = True

        if self.position["side"] == "LONG":
            self.position["best_price"] = max(self.position["best_price"], current_price)
            new_stop = self._calculate_trailing_stop_from_best_price("LONG", self.position["best_price"], atr)
            current_stop = self.position.get("trailing_stop_price")
            self.position["trailing_stop_price"] = new_stop if current_stop is None else max(current_stop, new_stop)
        else:
            self.position["best_price"] = min(self.position["best_price"], current_price)
            new_stop = self._calculate_trailing_stop_from_best_price("SHORT", self.position["best_price"], atr)
            current_stop = self.position.get("trailing_stop_price")
            self.position["trailing_stop_price"] = new_stop if current_stop is None else min(current_stop, new_stop)

    def _calculate_trailing_stop_from_best_price(self, side, best_price, atr):
        if self.config.trailing_use_percent_distance:
            if side == "LONG":
                return best_price * (1 - self.config.trailing_distance_pct / 100)
            return best_price * (1 + self.config.trailing_distance_pct / 100)

        if side == "LONG":
            return best_price - atr * self.config.trailing_atr_multiplier
        return best_price + atr * self.config.trailing_atr_multiplier

    def _extract_atr(self, result):
        return None

    def _calculate_pnl_pct(self, side, entry_price, exit_price):
        if side == "LONG":
            return (exit_price - entry_price) / entry_price * 100
        return (entry_price - exit_price) / entry_price * 100

    def get_position_snapshot(self, current_price):
        if self.position is None:
            return {
                "status": "FLAT",
                "side": None,
                "entry": None,
                "stop": None,
                "unrealized_pnl_pct": 0.0,
            }

        stop_price = self.position.get("trailing_stop_price") or self.position.get("stop_price")
        return {
            "status": "OPEN",
            "side": self.position["side"],
            "entry": self.position["entry_price"],
            "entry_time": self.position["entry_time"],
            "stop": stop_price,
            "remaining_size": self.position["remaining_size"],
            "total_size": self.position["total_size"],
            "unrealized_pnl_pct": self._calculate_pnl_pct(
                self.position["side"],
                self.position["entry_price"],
                current_price,
            ),
        }

    def get_account_snapshot(self, current_price):
        position = self.get_position_snapshot(current_price)
        unrealized_pnl = 0.0

        if self.position is not None:
            unrealized_pnl = (
                self.config.paper_start_balance
                * self.position["remaining_size"]
                * position["unrealized_pnl_pct"]
                / 100
            )

        return {
            "start_balance": self.config.paper_start_balance,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "equity": self.config.paper_start_balance + self.realized_pnl + unrealized_pnl,
        }

    def _write_event(self, event):
        if self.trade_log_path is None:
            return

        with open(self.trade_log_path, "a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _save_state(self):
        if self.state_path is None:
            return

        tmp_path = self.state_path.with_name(f"{self.state_path.name}.tmp")
        state = {
            "position": self.position,
            "realized_pnl": self.realized_pnl,
            "last_profit_exit": self.last_profit_exit,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        with open(tmp_path, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)

        tmp_path.replace(self.state_path)

    def _load_state(self):
        if self.state_path is None or not self.state_path.exists():
            return

        with open(self.state_path, encoding="utf-8") as file:
            state = json.load(file)

        self.position = state.get("position")
        self.realized_pnl = state.get("realized_pnl", 0.0)
        self.last_profit_exit = state.get("last_profit_exit")
