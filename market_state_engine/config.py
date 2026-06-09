from dataclasses import dataclass


@dataclass(frozen=True)
class MarketStateConfig:
    entry_long_score: int = 15
    entry_short_score: int = 15
    entry_score_gap: int = 5

    range_days: int = 15
    range_min_width_pct: float = 4.0
    range_near_pct: float = 2.0
    range_breakout_pct: float = 1.0
    range_breakout_score: int = 3
    range_position_bins: int = 20
    range_edge_bins_no_score: int = 2
    range_position_max_score: int = 4

    atr_period: int = 14
    atr_stop_multiplier: float = 1.5

    partial_take_profit_pct: float = 2.0
    partial_take_profit_size: float = 0.5
    trailing_take_profit_pct: float = 5.0
    trailing_atr_multiplier: float = 1.5

    add_entry_score_increase: int = 5
    max_add_entries: int = 1
    add_entry_size: float = 0.5

    paper_start_balance: float = 100.0
