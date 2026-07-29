from dataclasses import dataclass


@dataclass(frozen=True)
class MarketStateConfig:
    entry_long_score: int = 10
    entry_short_score: int = 10
    entry_score_gap: int = 5

    range_days: int = 15
    range_min_width_pct: float = 4.0
    range_near_pct: float = 2.0
    range_breakout_pct: float = 1.0
    range_breakout_score: int = 3
    range_level_memory_days: int = 3
    range_position_bins: int = 20
    range_edge_bins_no_score: int = 2
    range_position_max_score: int = 4

    atr_period: int = 14
    atr_stop_multiplier: float = 1.5
    # ATR itself decides the stop distance. Set a number only when a hard cap is wanted.
    atr_stop_max_loss_pct: float | None = None
    activity_direction_bonus_enabled: bool = True

    partial_take_profit_pct: float = 3.0
    partial_take_profit_size: float = 0.5
    small_profit_protection_enabled: bool = True
    small_profit_protection_min_peak_pct: float = 0.6
    small_profit_protection_fixed_exit_pct: float = 0.15
    small_profit_protection_ratio_start_peak_pct: float = 1.5
    small_profit_protection_retrace_ratio: float = 0.6
    profit_reentry_cooldown_minutes: int = 30
    profit_reentry_score_increase: int = 3
    profit_reentry_score_memory_minutes: int = 240
    trailing_take_profit_pct: float = 3.0
    trailing_atr_multiplier: float = 1.5
    trailing_distance_pct: float = 0.7
    trailing_use_percent_distance: bool = True

    add_entry_score_increase: int = 5
    max_add_entries: int = 1
    add_entry_size: float = 1.5

    paper_start_balance: float = 100.0

    live_capital_usdt: float = 300.0
    live_entry_notional_usdt: float = 120.0
    live_add_notional_usdt: float = 180.0
    live_leverage: int = 2
