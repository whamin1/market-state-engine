import json
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from .binance_signed_client import BinanceSignedClient


class LiveTrader:
    def __init__(
        self,
        config,
        client=None,
        dry_run=True,
        enabled=False,
        state_path="work/state/live_trader_state.json",
        trade_log_path="work/logs/live_trade_log.jsonl",
    ):
        self.config = config
        self.client = client or BinanceSignedClient()
        self.dry_run = dry_run
        self.enabled = enabled
        self.state_path = Path(state_path) if state_path else None
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.trade_log_path = Path(trade_log_path) if trade_log_path else None
        if self.trade_log_path:
            self.trade_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.position_state = None
        self.realized_pnl = 0.0
        self._load_state()

    def update(self, result, current_candle, current_time=None, symbol=None):
        current_price = current_candle["close"]
        is_dry_run = self.dry_run or not self.enabled
        if is_dry_run:
            sync_event = self._sync_dry_run_position_state(symbol, current_price, current_time, result)
        else:
            sync_event = self._sync_position_state(symbol, current_price, current_time, result)
        if sync_event:
            return sync_event

        if result["signal"] not in ("ENTER_LONG", "ENTER_SHORT"):
            return None

        if is_dry_run and self.position_state:
            return None

        side = "BUY" if result["signal"] == "ENTER_LONG" else "SELL"
        position_side = "LONG" if side == "BUY" else "SHORT"
        quantity = self._quantity_from_notional(self.config.live_entry_notional_usdt, current_price)

        event = {
            "type": "LIVE_ORDER",
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "signal": result["signal"],
            "side": side,
            "position_side": position_side,
            "notional_usdt": self.config.live_entry_notional_usdt,
            "quantity": quantity,
            "price": current_price,
            "dry_run": is_dry_run,
        }

        if event["dry_run"]:
            event["status"] = "DRY_RUN"
            self._set_position_state(symbol, position_side, current_price, current_time, result, quantity, dry_run=True)
            return event

        exchange_position = self.get_position_snapshot(symbol, current_price)
        if exchange_position["status"] == "OPEN":
            return None
        if exchange_position["status"] != "FLAT":
            raise RuntimeError(f"cannot place entry while exchange position status is {exchange_position['status']}")

        self.client.set_margin_type(symbol, "ISOLATED")
        self.client.set_leverage(symbol, self.config.live_leverage)
        event["response"] = self.client.place_market_order(symbol, side, quantity)
        event["status"] = "SENT"
        self._set_position_state(symbol, position_side, current_price, current_time, result, quantity)
        return event

    def get_position_snapshot(self, symbol, current_price):
        if self.position_state and self.position_state.get("dry_run"):
            return self._get_local_position_snapshot(current_price)

        try:
            positions = self.client.get_position_risk(symbol)
        except Exception as exc:
            return {"status": "UNKNOWN", "side": None, "entry": None, "stop": None, "unrealized_pnl_pct": 0.0, "error": str(exc)}

        position = positions[0] if isinstance(positions, list) and positions else positions
        amount = float(position.get("positionAmt", 0) or 0)
        if amount == 0:
            if self.position_state is not None:
                self.position_state = None
                self._save_state()
            return {"status": "FLAT", "side": None, "entry": None, "stop": None, "unrealized_pnl_pct": 0.0}

        entry = float(position.get("entryPrice", 0) or 0)
        side = "LONG" if amount > 0 else "SHORT"
        self._ensure_position_state(symbol, side, entry)

        if entry <= 0:
            pnl_pct = 0.0
        elif side == "LONG":
            pnl_pct = (current_price - entry) / entry * 100
        else:
            pnl_pct = (entry - current_price) / entry * 100

        unrealized_pnl_usdt = abs(amount) * (current_price - entry)
        if side == "SHORT":
            unrealized_pnl_usdt = abs(amount) * (entry - current_price)

        stop_price = None
        entry_time = None
        if self.position_state:
            self.position_state["amount"] = abs(amount)
            self.position_state["entry_price"] = entry
            self.position_state["updated_at"] = datetime.now(timezone.utc).isoformat()
            stop_price = self.position_state.get("trailing_stop_price") or self.position_state.get("stop_price")
            entry_time = self.position_state.get("entry_time")

        return {
            "status": "OPEN",
            "side": side,
            "entry": entry,
            "entry_time": entry_time,
            "stop": stop_price,
            "unrealized_pnl_pct": pnl_pct,
            "unrealized_pnl_usdt": unrealized_pnl_usdt,
            "amount": amount,
            "add_count": self.position_state.get("add_count", 0) if self.position_state else 0,
            "trailing_active": self.position_state.get("trailing_active", False) if self.position_state else False,
        }

    def get_account_snapshot(self, current_price):
        unrealized = self._calculate_local_unrealized_pnl(current_price)
        return {
            "start_balance": self.config.live_capital_usdt,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": unrealized,
            "equity": self.config.live_capital_usdt + self.realized_pnl + unrealized,
        }

    def _quantity_from_notional(self, notional, price):
        quantity = notional / price
        return f"{quantity:.3f}"

    def _sync_position_state(self, symbol, current_price, current_time, result):
        previous_state = dict(self.position_state) if self.position_state else None
        snapshot = self.get_position_snapshot(symbol, current_price)
        if snapshot["status"] == "FLAT" and previous_state:
            estimated_pnl = self._calculate_pnl_pct(
                previous_state.get("side"),
                previous_state.get("entry_price"),
                current_price,
            )
            amount = float(previous_state.get("amount", 0) or 0)
            estimated_pnl_usdt = amount * current_price * estimated_pnl / 100
            self.realized_pnl += estimated_pnl_usdt
            self._save_state()
            return {
                "type": "LIVE_POSITION_CLOSED",
                "logged_at": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "side": previous_state.get("side"),
                "entry_price": previous_state.get("entry_price"),
                "exit_price": current_price,
                "reason": "exchange position is closed",
                "estimated_realized_pnl": estimated_pnl_usdt,
            }

        if snapshot["status"] != "OPEN" or self.position_state is None:
            return None

        stop_event = self._maybe_close_at_stop(symbol, snapshot, current_price, "stop loss")
        if stop_event:
            return stop_event

        protection_event = self._maybe_close_live_on_small_profit_protection(symbol, snapshot, current_price)
        if protection_event:
            return protection_event

        trailing_event = self._update_trailing_state(snapshot["side"], current_price, symbol)
        if trailing_event and trailing_event["type"] == "LIVE_TRAILING_STOP_HIT":
            return self._close_live_position(symbol, snapshot, current_price, "trailing stop")

        signal_event = self._maybe_close_on_opposite_signal(symbol, snapshot, current_price, result)
        if signal_event:
            return signal_event

        if current_time and not self.position_state.get("last_seen_time"):
            self.position_state["last_seen_time"] = current_time
        else:
            self.position_state["last_seen_time"] = datetime.now(timezone.utc).isoformat()
        self._save_state()
        return trailing_event

    def _set_position_state(self, symbol, side, entry_price, current_time, result, quantity, dry_run=False):
        atr = result.get("atr")
        stop_price = None
        if atr is not None:
            if side == "LONG":
                stop_price = entry_price - atr * self.config.atr_stop_multiplier
            else:
                stop_price = entry_price + atr * self.config.atr_stop_multiplier

        self.position_state = {
            "symbol": symbol,
            "side": side,
            "entry_time": current_time or datetime.now(timezone.utc).isoformat(),
            "entry_price": entry_price,
            "amount": abs(float(quantity)),
            "dry_run": dry_run,
            "stop_price": stop_price,
            "trailing_stop_price": None,
            "trailing_active": False,
            "best_price": entry_price,
            "peak_profit_pct": 0.0,
            "add_count": 0,
            "partial_taken": False,
            "trailing_stop_alerted": False,
            "entry_score": result["long_score"] if side == "LONG" else result["short_score"],
            "last_add_score": result["long_score"] if side == "LONG" else result["short_score"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_state()

    def _ensure_position_state(self, symbol, side, entry_price):
        if self.position_state and self.position_state.get("symbol") == symbol and self.position_state.get("side") == side:
            self.position_state["entry_price"] = entry_price
            return

        self.position_state = {
            "symbol": symbol,
            "side": side,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "entry_price": entry_price,
            "amount": None,
            "dry_run": False,
            "stop_price": None,
            "trailing_stop_price": None,
            "trailing_active": False,
            "best_price": entry_price,
            "peak_profit_pct": 0.0,
            "add_count": 0,
            "partial_taken": False,
            "trailing_stop_alerted": False,
            "entry_score": None,
            "last_add_score": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_state()

    def _get_local_position_snapshot(self, current_price):
        entry_price = self.position_state["entry_price"]
        side = self.position_state["side"]
        pnl_pct = self._calculate_pnl_pct(side, entry_price, current_price)
        stop_price = self.position_state.get("trailing_stop_price") or self.position_state.get("stop_price")
        return {
            "status": "OPEN",
            "side": side,
            "entry": entry_price,
            "entry_time": self.position_state.get("entry_time"),
            "stop": stop_price,
            "unrealized_pnl_pct": pnl_pct,
            "unrealized_pnl_usdt": self._calculate_local_unrealized_pnl(current_price),
            "amount": self.position_state.get("amount"),
            "add_count": self.position_state.get("add_count", 0),
            "trailing_active": self.position_state.get("trailing_active", False),
        }

    def _sync_dry_run_position_state(self, symbol, current_price, current_time, result):
        if not self.position_state or not self.position_state.get("dry_run"):
            return None

        snapshot = self._get_local_position_snapshot(current_price)
        stop_event = self._maybe_close_dry_run_at_stop(current_price, "stop loss")
        if stop_event:
            return stop_event

        protection_event = self._maybe_close_dry_run_on_small_profit_protection(current_price)
        if protection_event:
            return protection_event

        trailing_event = self._update_trailing_state(self.position_state["side"], current_price, symbol)
        self.position_state["last_seen_time"] = current_time or datetime.now(timezone.utc).isoformat()

        if trailing_event and trailing_event["type"] == "LIVE_TRAILING_STOP_HIT":
            return self._close_dry_run_position(current_price, "trailing stop")

        signal_event = self._maybe_close_dry_run_on_opposite_signal(current_price, result)
        if signal_event:
            return signal_event

        self._save_state()
        return trailing_event

    def _maybe_close_at_stop(self, symbol, snapshot, current_price, reason):
        stop_price = self._get_stop_price()
        if not self._is_stop_touched(snapshot["side"], current_price, stop_price):
            return None
        return self._close_live_position(symbol, snapshot, current_price, reason)

    def _maybe_close_dry_run_at_stop(self, current_price, reason):
        stop_price = self._get_stop_price()
        side = self.position_state["side"]
        if not self._is_stop_touched(side, current_price, stop_price):
            return None
        return self._close_dry_run_position(current_price, reason)

    def _maybe_close_on_opposite_signal(self, symbol, snapshot, current_price, result):
        if not self._is_opposite_signal(snapshot["side"], result.get("signal")):
            return None

        if self._can_take_partial_profit(snapshot["side"], current_price):
            return self._close_live_position(symbol, snapshot, current_price, "partial take profit: opposite signal", fraction=self.config.partial_take_profit_size)
        return self._close_live_position(symbol, snapshot, current_price, "opposite signal")

    def _maybe_close_dry_run_on_opposite_signal(self, current_price, result):
        if not self._is_opposite_signal(self.position_state["side"], result.get("signal")):
            return None

        if self._can_take_partial_profit(self.position_state["side"], current_price):
            return self._close_dry_run_position(current_price, "partial take profit: opposite signal", fraction=self.config.partial_take_profit_size)
        return self._close_dry_run_position(current_price, "opposite signal")

    def _maybe_close_live_on_small_profit_protection(self, symbol, snapshot, current_price):
        reason = self._get_small_profit_protection_reason(snapshot["side"], current_price)
        if reason is None:
            return None
        return self._close_live_position(symbol, snapshot, current_price, reason)

    def _maybe_close_dry_run_on_small_profit_protection(self, current_price):
        reason = self._get_small_profit_protection_reason(self.position_state["side"], current_price)
        if reason is None:
            return None
        return self._close_dry_run_position(current_price, reason)

    def _get_small_profit_protection_reason(self, side, current_price):
        if not self.config.small_profit_protection_enabled:
            return None

        pnl_pct = self._calculate_pnl_pct(side, self.position_state["entry_price"], current_price)
        peak_profit = max(self.position_state.get("peak_profit_pct", 0.0), pnl_pct)
        self.position_state["peak_profit_pct"] = peak_profit

        if peak_profit < self.config.small_profit_protection_min_peak_pct:
            return None
        if peak_profit >= self.config.small_profit_protection_max_pct:
            return None
        if pnl_pct <= 0:
            return None

        trigger_pct = peak_profit * self.config.small_profit_protection_retrace_ratio
        if pnl_pct > trigger_pct:
            return None

        return f"small profit protection: peak {peak_profit:.2f}% -> current {pnl_pct:.2f}%"

    def _close_live_position(self, symbol, snapshot, current_price, reason, fraction=1.0):
        amount = abs(float(snapshot.get("amount", 0) or 0))
        quantity = self._round_down_quantity(amount * fraction)
        if quantity <= 0:
            raise RuntimeError("close quantity is below the BTCUSDT minimum step")

        side = "SELL" if snapshot["side"] == "LONG" else "BUY"
        response = self.client.place_market_order(symbol, side, self._format_quantity(quantity), reduce_only=True)
        pnl_pct = self._calculate_pnl_pct(snapshot["side"], snapshot["entry"], current_price)
        estimated_pnl = quantity * (current_price - snapshot["entry"])
        if snapshot["side"] == "SHORT":
            estimated_pnl = quantity * (snapshot["entry"] - current_price)
        self.realized_pnl += estimated_pnl

        is_partial = fraction < 1.0 and quantity < amount
        event = {
            "type": "LIVE_PARTIAL_CLOSE" if is_partial else "LIVE_CLOSE",
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": snapshot["side"],
            "entry_price": snapshot["entry"],
            "exit_price": current_price,
            "quantity": self._format_quantity(quantity),
            "pnl_pct": pnl_pct,
            "estimated_realized_pnl": estimated_pnl,
            "reason": reason,
            "response": response,
            "status": "SENT",
        }

        if is_partial:
            self.position_state["amount"] = max(amount - quantity, 0.0)
            self.position_state["partial_taken"] = True
        else:
            self.position_state = None
        self._save_state()
        return event

    def _close_dry_run_position(self, current_price, reason, fraction=1.0):
        amount = float(self.position_state.get("amount", 0) or 0)
        quantity = self._round_down_quantity(amount * fraction)
        if quantity <= 0:
            quantity = amount

        side = self.position_state["side"]
        entry_price = self.position_state["entry_price"]
        pnl_pct = self._calculate_pnl_pct(side, entry_price, current_price)
        realized_pnl = quantity * (current_price - entry_price)
        if side == "SHORT":
            realized_pnl = quantity * (entry_price - current_price)
        self.realized_pnl += realized_pnl

        is_partial = fraction < 1.0 and quantity < amount
        event = {
            "type": "LIVE_PARTIAL_CLOSE" if is_partial else "LIVE_CLOSE",
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "symbol": self.position_state["symbol"],
            "side": side,
            "entry_price": entry_price,
            "exit_price": current_price,
            "quantity": self._format_quantity(quantity),
            "pnl_pct": pnl_pct,
            "estimated_realized_pnl": realized_pnl,
            "reason": reason,
            "status": "DRY_RUN_CLOSE",
        }

        if is_partial:
            self.position_state["amount"] = max(amount - quantity, 0.0)
            self.position_state["partial_taken"] = True
        else:
            self.position_state = None
        self._save_state()
        return event

    def _get_stop_price(self):
        if not self.position_state:
            return None
        return self.position_state.get("trailing_stop_price") or self.position_state.get("stop_price")

    def _is_stop_touched(self, side, current_price, stop_price):
        if stop_price is None:
            return False
        if side == "LONG":
            return current_price <= stop_price
        return current_price >= stop_price

    def _is_opposite_signal(self, side, signal):
        return (side == "LONG" and signal == "ENTER_SHORT") or (side == "SHORT" and signal == "ENTER_LONG")

    def _can_take_partial_profit(self, side, current_price):
        if self.position_state.get("partial_taken"):
            return False
        pnl_pct = self._calculate_pnl_pct(side, self.position_state["entry_price"], current_price)
        return pnl_pct >= self.config.partial_take_profit_pct

    def _round_down_quantity(self, quantity):
        return math.floor(quantity * 1000) / 1000

    def _format_quantity(self, quantity):
        return f"{quantity:.3f}"

    def record_event(self, event):
        if self.trade_log_path is None:
            return
        with open(self.trade_log_path, "a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _update_trailing_state(self, side, current_price, symbol):
        if not self.position_state:
            return None

        entry_price = self.position_state.get("entry_price")
        if not entry_price:
            return None

        if side == "LONG":
            pnl_pct = (current_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - current_price) / entry_price * 100

        was_active = self.position_state.get("trailing_active", False)
        if not was_active and pnl_pct < self.config.trailing_take_profit_pct:
            return None

        self.position_state["trailing_active"] = True
        if side == "LONG":
            best_price = max(self.position_state.get("best_price", entry_price), current_price)
            new_stop = best_price * (1 - self.config.trailing_distance_pct / 100)
            current_stop = self.position_state.get("trailing_stop_price")
            self.position_state["best_price"] = best_price
            self.position_state["trailing_stop_price"] = new_stop if current_stop is None else max(current_stop, new_stop)
        else:
            best_price = min(self.position_state.get("best_price", entry_price), current_price)
            new_stop = best_price * (1 + self.config.trailing_distance_pct / 100)
            current_stop = self.position_state.get("trailing_stop_price")
            self.position_state["best_price"] = best_price
            self.position_state["trailing_stop_price"] = new_stop if current_stop is None else min(current_stop, new_stop)

        self.position_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        stop_price = self.position_state.get("trailing_stop_price")

        if self._is_trailing_stop_touched(side, current_price, stop_price):
            if self.position_state.get("trailing_stop_alerted"):
                return None
            self.position_state["trailing_stop_alerted"] = True
            return {
                "type": "LIVE_TRAILING_STOP_HIT",
                "logged_at": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "side": side,
                "price": current_price,
                "stop_price": stop_price,
                "best_price": self.position_state.get("best_price"),
                "reason": "local trailing stop touched",
            }

        if not was_active:
            return {
                "type": "LIVE_TRAILING_START",
                "logged_at": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "side": side,
                "price": current_price,
                "stop_price": stop_price,
                "best_price": self.position_state.get("best_price"),
                "trigger_pct": self.config.trailing_take_profit_pct,
            }

        return None

    def _is_trailing_stop_touched(self, side, current_price, stop_price):
        return self._is_stop_touched(side, current_price, stop_price)

    def _calculate_local_unrealized_pnl(self, current_price):
        if not self.position_state:
            return 0.0

        entry_price = self.position_state.get("entry_price")
        amount = self.position_state.get("amount")
        side = self.position_state.get("side")
        if not entry_price or not amount or not side:
            return 0.0

        if side == "LONG":
            return amount * (current_price - entry_price)
        return amount * (entry_price - current_price)

    def _calculate_pnl_pct(self, side, entry_price, current_price):
        if side == "LONG":
            return (current_price - entry_price) / entry_price * 100
        return (entry_price - current_price) / entry_price * 100

    def _save_state(self):
        if self.state_path is None:
            return

        tmp_path = self.state_path.with_name(f"{self.state_path.name}.tmp")
        state = {
            "position": self.position_state,
            "realized_pnl": self.realized_pnl,
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
        self.position_state = state.get("position")
        self.realized_pnl = state.get("realized_pnl", 0.0)
