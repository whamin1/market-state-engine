from datetime import datetime, timezone

from .binance_signed_client import BinanceSignedClient


class LiveTrader:
    def __init__(self, config, client=None, dry_run=True, enabled=False):
        self.config = config
        self.client = client or BinanceSignedClient()
        self.dry_run = dry_run
        self.enabled = enabled
        self.add_count = 0

    def update(self, result, current_candle, current_time=None, symbol=None):
        if result["signal"] not in ("ENTER_LONG", "ENTER_SHORT"):
            return None

        exchange_position = self.get_position_snapshot(symbol, current_candle["close"])
        if exchange_position["status"] == "OPEN":
            return None

        side = "BUY" if result["signal"] == "ENTER_LONG" else "SELL"
        quantity = self._quantity_from_notional(self.config.live_entry_notional_usdt, current_candle["close"])

        event = {
            "type": "LIVE_ORDER",
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "signal": result["signal"],
            "side": side,
            "notional_usdt": self.config.live_entry_notional_usdt,
            "quantity": quantity,
            "price": current_candle["close"],
            "dry_run": self.dry_run or not self.enabled,
        }

        if event["dry_run"]:
            event["status"] = "DRY_RUN"
            return event

        self.client.set_margin_type(symbol, "ISOLATED")
        self.client.set_leverage(symbol, self.config.live_leverage)
        event["response"] = self.client.place_market_order(symbol, side, quantity)
        event["status"] = "SENT"
        return event

    def get_position_snapshot(self, symbol, current_price):
        try:
            positions = self.client.get_position_risk(symbol)
        except Exception as exc:
            return {"status": "UNKNOWN", "side": None, "entry": None, "stop": None, "unrealized_pnl_pct": 0.0, "error": str(exc)}

        position = positions[0] if isinstance(positions, list) and positions else positions
        amount = float(position.get("positionAmt", 0) or 0)
        if amount == 0:
            return {"status": "FLAT", "side": None, "entry": None, "stop": None, "unrealized_pnl_pct": 0.0}

        entry = float(position.get("entryPrice", 0) or 0)
        side = "LONG" if amount > 0 else "SHORT"
        if entry <= 0:
            pnl_pct = 0.0
        elif side == "LONG":
            pnl_pct = (current_price - entry) / entry * 100
        else:
            pnl_pct = (entry - current_price) / entry * 100

        return {
            "status": "OPEN",
            "side": side,
            "entry": entry,
            "stop": None,
            "unrealized_pnl_pct": pnl_pct,
            "amount": amount,
        }

    def get_account_snapshot(self, current_price):
        try:
            account = self.client.get_account()
            wallet = float(account.get("totalWalletBalance", 0) or 0)
            unrealized = float(account.get("totalUnrealizedProfit", 0) or 0)
            return {
                "start_balance": self.config.live_capital_usdt,
                "realized_pnl": wallet - self.config.live_capital_usdt,
                "unrealized_pnl": unrealized,
                "equity": wallet + unrealized,
            }
        except Exception:
            return {
                "start_balance": self.config.live_capital_usdt,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "equity": None,
            }

    def _quantity_from_notional(self, notional, price):
        quantity = notional / price
        return f"{quantity:.3f}"
