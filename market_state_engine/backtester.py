from __future__ import annotations
from .config import MarketStateConfig
from .engine import MarketStateEngine
from .logger import MarketStateLogger
from .paper_trader import PaperTrader


class Backtester:
    def __init__(
        self,
        config: MarketStateConfig | None = None,
        warmup_candles: int = 365,
        state_log_path="work/logs/backtest_states.jsonl",
        trade_log_path="work/logs/backtest_trades.jsonl",
        close_open_position_on_end: bool = True,
    ):
        self.config = config or MarketStateConfig()
        self.warmup_candles = warmup_candles
        self.close_open_position_on_end = close_open_position_on_end
        self.engine = MarketStateEngine(self.config)
        self.logger = MarketStateLogger(state_log_path)
        self.trader = PaperTrader(self.config, trade_log_path=trade_log_path)

    def run(self, candles, symbol=None, liquidation_data=None):
        if len(candles) <= self.warmup_candles:
            raise ValueError("not enough candles for backtest warmup")

        trade_events = []

        for index in range(self.warmup_candles, len(candles)):
            completed_candles = candles[index - self.warmup_candles : index]
            current_candle = candles[index]
            current_time = current_candle.get("timestamp")

            result = self.engine.update(
                completed_candles,
                current_candle=current_candle,
                day_progress=1,
                current_time=current_time,
                liquidation_data=liquidation_data,
            )

            self.logger.log(
                result,
                symbol=symbol,
                current_candle=current_candle,
                current_time=current_time,
            )

            trade_event = self.trader.update(
                result,
                current_candle=current_candle,
                current_time=current_time,
                symbol=symbol,
            )

            if trade_event:
                trade_events.append(trade_event)

        if self.close_open_position_on_end and self.trader.position is not None:
            last_candle = candles[-1]
            close_event = self.trader._close_position(
                last_candle["close"],
                last_candle.get("timestamp"),
                symbol,
                "backtest ended",
            )
            trade_events.append(close_event)

        return self._summarize(trade_events)

    def _summarize(self, trade_events):
        closed_trades = [event for event in trade_events if event["type"] == "CLOSE"]
        total_pnl_pct = sum(event["pnl_pct"] for event in closed_trades)
        win_count = sum(1 for event in closed_trades if event["pnl_pct"] > 0)

        return {
            "event_count": len(trade_events),
            "closed_trade_count": len(closed_trades),
            "win_count": win_count,
            "loss_count": len(closed_trades) - win_count,
            "total_pnl_pct": total_pnl_pct,
            "open_position": self.trader.position,
        }
