import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import MarketStateConfig


class MarketStateEngine:
    def __init__(self, config: MarketStateConfig | None = None, state_path=None):
        self.config = config or MarketStateConfig()
        self.state_path = Path(state_path) if state_path else None
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.failure_counts = {
            "upper_breakout_failure": 0,
            "lower_breakdown_failure": 0,
        }
        self._counted_failure_keys = set()
        self.range_levels = {
            "breakout": None,
            "breakdown": None,
        }
        self._load_state()

    def update(self, ohlcv_data, current_candle=None, day_progress=None, current_time=None, liquidation_data=None):
        failure_result = self.update_failure_counts(ohlcv_data)
        price_position_result = self.calc_price_position_score(ohlcv_data, current_candle)
        body_result = self.calc_body_score(ohlcv_data, current_candle)
        volume_result = self.calc_volume_score(ohlcv_data, current_candle, day_progress, current_time)
        trend_result = self.calc_trend_continuity_score(ohlcv_data, current_candle)
        range_result = self.calc_range_score(ohlcv_data, current_candle, current_time)
        atr_result = self.calc_atr_score(ohlcv_data, current_candle)
        liquidation_result = self.calc_liquidation_score(liquidation_data, current_time)

        long_score = (
            price_position_result["long_score"]
            + body_result["long_score"]
            + volume_result["long_score"]
            + trend_result["long_score"]
            + range_result["long_score"]
            + liquidation_result["long_score"]
        )
        short_score = (
            price_position_result["short_score"]
            + body_result["short_score"]
            + volume_result["short_score"]
            + trend_result["short_score"]
            + range_result["short_score"]
            + liquidation_result["short_score"]
        )
        long_score += liquidation_result["long_activity_bonus"]
        short_score += liquidation_result["short_activity_bonus"]

        activity_score = atr_result["activity_score"]
        activity_direction_result = self.calc_activity_direction_bonus(current_candle, activity_score)
        long_score += activity_direction_result["long_score"]
        short_score += activity_direction_result["short_score"]
        long_score = max(long_score, 0)
        short_score = max(short_score, 0)

        if range_result["block_trade"]:
            state = "HOLD"
            signal = "NO_TRADE"
        else:
            state = self.get_state(long_score, short_score)
            signal = self.get_signal(long_score, short_score)

        return {
            "long_score": long_score,
            "short_score": short_score,
            "activity_score": activity_score,
            "liquidation_activity_score": liquidation_result["activity_score"],
            "liquidation_activity_bonus": max(
                liquidation_result["long_activity_bonus"],
                liquidation_result["short_activity_bonus"],
            ),
            "state": state,
            "signal": signal,
            "range": range_result["range"],
            "atr": atr_result["atr"],
            "failure_counts": dict(self.failure_counts),
            # These are the values already calculated above. They are exposed so
            # recorders can persist the real scoring inputs without recalculating.
            "score_components": {
                "price_position": self._score_pair(price_position_result),
                "body": self._score_pair(body_result),
                "volume": self._score_pair(volume_result),
                "trend_continuity": self._score_pair(trend_result),
                "range": {
                    **self._score_pair(range_result),
                    **dict(range_result.get("score_components", {})),
                    "block_trade": range_result.get("block_trade", False),
                },
                "liquidation": {
                    "long_score": liquidation_result["long_score"],
                    "short_score": liquidation_result["short_score"],
                    "activity_score": liquidation_result["activity_score"],
                    "long_activity_bonus": liquidation_result["long_activity_bonus"],
                    "short_activity_bonus": liquidation_result["short_activity_bonus"],
                },
                "atr": {"activity_score": atr_result["activity_score"]},
                "activity_direction": self._score_pair(activity_direction_result),
            },
            "indicators": {
                "current_candle": self._candle_values(current_candle),
                "price_position": price_position_result.get("indicators", {}),
                "body": body_result.get("indicators", {}),
                "volume": volume_result.get("indicators", {}),
                "trend_continuity": trend_result.get("indicators", {}),
                "range": range_result.get("range"),
                "atr": atr_result.get("indicators", {}),
                "liquidation": liquidation_result.get("indicators", {}),
            },
            "reasons": (
                failure_result["reasons"]
                + price_position_result["reasons"]
                + body_result["reasons"]
                + volume_result["reasons"]
                + trend_result["reasons"]
                + range_result["reasons"]
                + atr_result["reasons"]
                + activity_direction_result["reasons"]
                + liquidation_result["reasons"]
            ),
        }

    @staticmethod
    def _score_pair(result):
        return {
            "long_score": result.get("long_score", 0),
            "short_score": result.get("short_score", 0),
        }

    @staticmethod
    def _candle_values(candle):
        if not candle:
            return {}

        return {
            "timestamp": candle.get("timestamp"),
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "close": candle.get("close"),
            "volume": candle.get("volume"),
        }

    def calc_activity_direction_bonus(self, current_candle, activity_score):
        if not self.config.activity_direction_bonus_enabled or current_candle is None or activity_score <= 0:
            return {"long_score": 0, "short_score": 0, "reasons": []}

        direction = self._candle_direction(current_candle)
        if direction == "UP":
            return {
                "long_score": activity_score,
                "short_score": 0,
                "reasons": [f"activity_direction LONG +{activity_score}"],
            }

        if direction == "DOWN":
            return {
                "long_score": 0,
                "short_score": activity_score,
                "reasons": [f"activity_direction SHORT +{activity_score}"],
            }

        return {"long_score": 0, "short_score": 0, "reasons": ["activity_direction 0: current candle is doji"]}

    def update_failure_counts(self, ohlcv_data):
        if len(ohlcv_data) < self.config.range_days + 1:
            return {"reasons": ["failure_count skipped: not enough completed candles"]}

        completed_candle = ohlcv_data[-1]
        reference_candles = ohlcv_data[: -1]
        range_candles = reference_candles[-self.config.range_days :]
        range_high = max(candle["high"] for candle in range_candles)
        range_low = min(candle["low"] for candle in range_candles)

        candle_key = completed_candle.get("timestamp", len(ohlcv_data))
        reasons = []

        upper_breakout_price = range_high * (1 + self.config.range_breakout_pct / 100)
        lower_breakdown_price = range_low * (1 - self.config.range_breakout_pct / 100)

        upper_failure_key = (candle_key, "upper_breakout_failure")
        if (
            completed_candle["high"] > upper_breakout_price
            and completed_candle["close"] <= range_high
            and upper_failure_key not in self._counted_failure_keys
        ):
            self.failure_counts["upper_breakout_failure"] += 1
            self._counted_failure_keys.add(upper_failure_key)
            reasons.append("failure_count upper breakout failure +1")

        lower_failure_key = (candle_key, "lower_breakdown_failure")
        if (
            completed_candle["low"] < lower_breakdown_price
            and completed_candle["close"] >= range_low
            and lower_failure_key not in self._counted_failure_keys
        ):
            self.failure_counts["lower_breakdown_failure"] += 1
            self._counted_failure_keys.add(lower_failure_key)
            reasons.append("failure_count lower breakdown failure +1")

        if not reasons:
            reasons.append("failure_count 0: no completed breakout failure")

        return {"reasons": reasons}

    def calc_liquidation_score(self, liquidation_data=None, current_time=None):
        if not liquidation_data:
            return self._empty_liquidation_result("liquidation_score skipped: no data")

        raw_events = liquidation_data.get("raw_events", [])
        hourly_history = liquidation_data.get("hourly_history", [])
        symbol = liquidation_data.get("symbol")

        if not raw_events or not hourly_history:
            return self._empty_liquidation_result("liquidation_score skipped: missing raw or hourly data")

        now = self._parse_time(current_time) if current_time is not None else datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(hours=1)
        one_week_ago = now - timedelta(days=7)

        current_short_liq = 0.0
        current_long_liq = 0.0

        for event in raw_events:
            if symbol and event.get("symbol") != symbol:
                continue

            event_time = self._parse_time(event.get("event_time_kst", event.get("timestamp")))
            if not (one_hour_ago < event_time <= now):
                continue

            usd_size = self._safe_float(event.get("usd_size"))
            liq_type = event.get("liq_type")
            side = event.get("side")

            if liq_type == "short_liquidation" or side == "BUY":
                current_short_liq += usd_size
            elif liq_type == "long_liquidation" or side == "SELL":
                current_long_liq += usd_size

        reference_total_liqs = []
        reference_net_liqs = []

        for row in hourly_history:
            if symbol and row.get("symbol") != symbol:
                continue

            row_time = self._parse_time(row.get("window_end_kst", row.get("timestamp")))
            if not (one_week_ago <= row_time < now):
                continue

            short_liq = self._safe_float(row.get("short_liq_usd_1h", row.get("short_liq_usd")))
            long_liq = self._safe_float(row.get("long_liq_usd_1h", row.get("long_liq_usd")))
            reference_total_liqs.append(short_liq + long_liq)
            reference_net_liqs.append(abs(short_liq - long_liq))

        current_total_liq = current_short_liq + current_long_liq
        current_net_liq = current_short_liq - current_long_liq
        imbalance_ratio = current_net_liq / current_total_liq if current_total_liq > 0 else 0.0
        liquidation_activity_score = self._percentile_to_score(current_total_liq, reference_total_liqs)
        net_liq_score = self._percentile_to_score(abs(current_net_liq), reference_net_liqs)
        reference_hours = len(reference_total_liqs)

        if current_total_liq <= 0:
            result = self._empty_liquidation_result("liquidation_score 0: no liquidation in current hour")
            result["indicators"] = self._liquidation_indicators(
                current_short_liq,
                current_long_liq,
                current_total_liq,
                current_net_liq,
                imbalance_ratio,
                reference_hours,
            )
            return result

        if abs(imbalance_ratio) < self.config.liquidation_min_imbalance_ratio:
            return {
                "long_score": 0,
                "short_score": 0,
                "activity_score": liquidation_activity_score,
                "long_activity_bonus": 0,
                "short_activity_bonus": 0,
                "indicators": self._liquidation_indicators(
                    current_short_liq,
                    current_long_liq,
                    current_total_liq,
                    current_net_liq,
                    imbalance_ratio,
                    reference_hours,
                ),
                "reasons": [
                    "liquidation_score balanced direction +0 "
                    f"imbalance_ratio={imbalance_ratio:.2f} total_1h={current_total_liq:.2f} "
                    f"activity_score={liquidation_activity_score} reference_hours={reference_hours}"
                ],
            }

        direction = "LONG" if imbalance_ratio > 0 else "SHORT"
        activity_bonus = 0
        if (
            liquidation_activity_score >= self.config.liquidation_activity_bonus_min_score
            and abs(imbalance_ratio) >= self.config.liquidation_activity_bonus_min_imbalance_ratio
        ):
            activity_bonus = self.config.liquidation_activity_bonus_score

        reasons = [
            f"liquidation_score imbalance {direction} +{net_liq_score} "
            f"short_liq_1h={current_short_liq:.2f} long_liq_1h={current_long_liq:.2f} "
            f"imbalance_ratio={imbalance_ratio:.2f} activity_score={liquidation_activity_score} "
            f"reference_hours={reference_hours}"
        ]
        if activity_bonus:
            reasons.append(f"liquidation_activity_bonus {direction} +{activity_bonus}")

        return {
            "long_score": net_liq_score if direction == "LONG" else 0,
            "short_score": net_liq_score if direction == "SHORT" else 0,
            "activity_score": liquidation_activity_score,
            "long_activity_bonus": activity_bonus if direction == "LONG" else 0,
            "short_activity_bonus": activity_bonus if direction == "SHORT" else 0,
            "indicators": self._liquidation_indicators(
                current_short_liq,
                current_long_liq,
                current_total_liq,
                current_net_liq,
                imbalance_ratio,
                reference_hours,
            ),
            "reasons": reasons,
        }

    @staticmethod
    def _empty_liquidation_result(reason):
        return {
            "long_score": 0,
            "short_score": 0,
            "activity_score": 0,
            "long_activity_bonus": 0,
            "short_activity_bonus": 0,
            "indicators": {},
            "reasons": [reason],
        }

    @staticmethod
    def _liquidation_indicators(short_liq, long_liq, total_liq, net_liq, imbalance_ratio, reference_hours):
        return {
            "short_liq_1h": short_liq,
            "long_liq_1h": long_liq,
            "total_liq_1h": total_liq,
            "net_liq_1h": net_liq,
            "imbalance_ratio": imbalance_ratio,
            "reference_hours": reference_hours,
        }

    def calc_atr_score(self, ohlcv_data, current_candle=None):
        if current_candle is None:
            candles = ohlcv_data
        else:
            candles = ohlcv_data + [current_candle]

        minimum_candles = self.config.atr_period + 2
        if len(candles) < minimum_candles:
            return {
                "activity_score": 0,
                "atr": None,
                "indicators": {"period": self.config.atr_period, "reference_count": 0, "current_true_range": None},
                "reasons": ["atr_score skipped: not enough candles"],
            }

        true_ranges = self._calculate_true_ranges(candles)
        atr_values = self._calculate_atr_values(true_ranges, self.config.atr_period)

        if len(atr_values) < 2:
            return {
                "activity_score": 0,
                "atr": None,
                "indicators": {"period": self.config.atr_period, "reference_count": 0, "current_true_range": None},
                "reasons": ["atr_score skipped: not enough ATR values"],
            }

        current_atr = atr_values[-1]
        reference_atrs = atr_values[-366:-1]
        raw_score = self._percentile_to_score(current_atr, reference_atrs)
        score = min(raw_score, self.config.atr_max_score)

        return {
            "activity_score": score,
            "atr": current_atr,
            "indicators": {
                "period": self.config.atr_period,
                "reference_count": len(reference_atrs),
                "current_true_range": true_ranges[-1],
                "raw_score": raw_score,
                "score_cap": self.config.atr_max_score,
            },
            "reasons": [f"atr_score activity +{score} raw={raw_score} cap={self.config.atr_max_score} atr={current_atr:.2f}"],
        }

    def calc_trend_continuity_score(self, ohlcv_data, current_candle=None):
        if current_candle is None:
            if len(ohlcv_data) < 8:
                return {
                    "long_score": 0,
                    "short_score": 0,
                    "indicators": {},
                    "reasons": ["trend_continuity skipped: not enough candles"],
                }

            reference_candles = ohlcv_data[:-1]
            current_candle = ohlcv_data[-1]
        else:
            if len(ohlcv_data) < 7:
                return {
                    "long_score": 0,
                    "short_score": 0,
                    "indicators": {},
                    "reasons": ["trend_continuity skipped: not enough reference candles"],
                }

            reference_candles = ohlcv_data

        current_direction = self._candle_direction(current_candle)
        previous_direction = self._candle_direction(reference_candles[-1])
        trend_indicators = {
            "current_direction": current_direction,
            "previous_direction": previous_direction,
            "current_body": abs(current_candle["close"] - current_candle["open"]),
            "reference_count": len(reference_candles),
        }

        if current_direction == "FLAT":
            return {
                "long_score": 0,
                "short_score": 0,
                "indicators": trend_indicators,
                "reasons": ["trend_continuity 0: current candle is doji"],
            }

        long_score = 0
        short_score = 0
        reasons = []

        if current_direction == previous_direction:
            current_body = trend_indicators["current_body"]
            reference_bodies = [abs(candle["close"] - candle["open"]) for candle in reference_candles]
            continuity_score = self._percentile_to_score(current_body, reference_bodies)

            if current_direction == "UP":
                long_score += continuity_score
                reasons.append(f"trend_continuity same direction LONG +{continuity_score}")
            else:
                short_score += continuity_score
                reasons.append(f"trend_continuity same direction SHORT +{continuity_score}")
        else:
            reasons.append("trend_continuity 0: current and previous candle differ")

        recent_7_candles = reference_candles[-6:] + [current_candle]
        up_count = sum(1 for candle in recent_7_candles if self._candle_direction(candle) == "UP")
        down_count = sum(1 for candle in recent_7_candles if self._candle_direction(candle) == "DOWN")
        trend_indicators["up_count_7"] = up_count
        trend_indicators["down_count_7"] = down_count

        if up_count == 7:
            long_score += 2
            reasons.append("trend_continuity 7 up candles LONG +2")
        elif up_count >= 6:
            long_score += 1
            reasons.append("trend_continuity 6 up candles LONG +1")

        if down_count == 7:
            short_score += 2
            reasons.append("trend_continuity 7 down candles SHORT +2")
        elif down_count >= 6:
            short_score += 1
            reasons.append("trend_continuity 6 down candles SHORT +1")

        return {
            "long_score": long_score,
            "short_score": short_score,
            "indicators": trend_indicators,
            "reasons": reasons,
        }

    def calc_range_score(self, ohlcv_data, current_candle=None, current_time=None):
        if current_candle is None:
            if len(ohlcv_data) < self.config.range_days + 1:
                return self._empty_range_result("range skipped: not enough candles")

            reference_candles = ohlcv_data[:-1]
            current_candle = ohlcv_data[-1]
        else:
            if len(ohlcv_data) < self.config.range_days:
                return self._empty_range_result("range skipped: not enough reference candles")

            reference_candles = ohlcv_data

        range_candles = reference_candles[-self.config.range_days :]
        range_high = max(candle["high"] for candle in range_candles)
        range_low = min(candle["low"] for candle in range_candles)
        current_price = current_candle["close"]
        current_high = current_candle["high"]
        current_low = current_candle["low"]
        now = self._parse_time(current_time) if current_time is not None else datetime.now(timezone.utc)
        range_width_pct = (range_high - range_low) / range_low * 100

        long_score = 0
        short_score = 0
        breakout_long_score = 0
        breakdown_short_score = 0
        edge_penalty_long_score = 0
        edge_penalty_short_score = 0
        block_trade = False
        reasons = []

        if range_width_pct < self.config.range_min_width_pct:
            block_trade = True
            reasons.append(f"range HOLD: width {range_width_pct:.2f}% below {self.config.range_min_width_pct:.2f}%")

        self._expire_range_levels(now)

        breakout_level = self._get_active_range_level("breakout", now)
        breakdown_level = self._get_active_range_level("breakdown", now)
        if breakout_level is not None and current_price <= breakout_level:
            self._clear_range_level("breakout")
            breakout_level = None
            reasons.append("range_break_score upper breakout cleared: price returned below level")

        if breakdown_level is not None and current_price >= breakdown_level:
            self._clear_range_level("breakdown")
            breakdown_level = None
            reasons.append("range_break_score lower breakdown cleared: price returned above level")

        if breakout_level is None and current_price > range_high * (1 + self.config.range_breakout_pct / 100):
            breakout_level = range_high
            self._remember_range_level("breakout", breakout_level, now)

        if breakdown_level is None and current_price < range_low * (1 - self.config.range_breakout_pct / 100):
            breakdown_level = range_low
            self._remember_range_level("breakdown", breakdown_level, now)

        if breakout_level is not None:
            score = self._get_range_break_score("breakout", now)
            long_score += score
            breakout_long_score = score
            reasons.append(f"range_break_score upper breakout LONG +{score} level={breakout_level:.2f}")
        elif self._pct_distance(current_price, range_high) <= self.config.range_near_pct:
            long_score -= self.config.range_near_score_penalty
            edge_penalty_long_score = -self.config.range_near_score_penalty
            reasons.append(f"range_edge_penalty near high LONG -{self.config.range_near_score_penalty}")

        if breakdown_level is not None:
            score = self._get_range_break_score("breakdown", now)
            short_score += score
            breakdown_short_score = score
            reasons.append(f"range_break_score lower breakdown SHORT +{score} level={breakdown_level:.2f}")
        elif self._pct_distance(current_price, range_low) <= self.config.range_near_pct:
            short_score -= self.config.range_near_score_penalty
            edge_penalty_short_score = -self.config.range_near_score_penalty
            reasons.append(f"range_edge_penalty near low SHORT -{self.config.range_near_score_penalty}")

        position_result = self._calc_range_position_score(current_price, range_low, range_high)
        long_score += position_result["long_score"]
        short_score += position_result["short_score"]
        if position_result["reason"]:
            reasons.append(position_result["reason"])

        if not reasons:
            reasons.append("range 0: normal range conditions")

        return {
            "long_score": long_score,
            "short_score": short_score,
            "block_trade": block_trade,
            "score_components": {
                "breakout_long_score": breakout_long_score,
                "breakdown_short_score": breakdown_short_score,
                "edge_penalty_long_score": edge_penalty_long_score,
                "edge_penalty_short_score": edge_penalty_short_score,
                "position_long_score": position_result["long_score"],
                "position_short_score": position_result["short_score"],
            },
            "range": {
                "high": range_high,
                "low": range_low,
                "width_pct": range_width_pct,
                "position_bin": position_result["position_bin"],
                "breakout_level": breakout_level,
                "breakdown_level": breakdown_level,
                "breakout_age_hours": self._get_range_break_age_hours("breakout", now),
                "breakdown_age_hours": self._get_range_break_age_hours("breakdown", now),
            },
            "reasons": reasons,
        }

    def _remember_range_level(self, side, level, now):
        self.range_levels[side] = {
            "level": level,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=self.config.range_level_memory_days)).isoformat(),
        }
        self._save_state()

    def _clear_range_level(self, side):
        self.range_levels[side] = None
        self._save_state()

    def _get_range_break_score(self, side, now):
        level_state = self.range_levels.get(side)
        if not level_state:
            return 0

        age_days = self._get_range_break_age_hours(side, now) // 24
        return max(self.config.range_breakout_score - age_days, 0)

    def _get_range_break_age_hours(self, side, now):
        level_state = self.range_levels.get(side)
        if not level_state:
            return None

        created_at = self._parse_time(level_state["created_at"])
        return max(int((now - created_at).total_seconds() // 3600), 0)

    def _get_active_range_level(self, side, now):
        level_state = self.range_levels.get(side)
        if not level_state:
            return None

        expires_at = self._parse_time(level_state["expires_at"])
        if now >= expires_at:
            self.range_levels[side] = None
            self._save_state()
            return None

        return level_state["level"]

    def _expire_range_levels(self, now):
        self._get_active_range_level("breakout", now)
        self._get_active_range_level("breakdown", now)

    def _save_state(self):
        if self.state_path is None:
            return

        tmp_path = self.state_path.with_name(f"{self.state_path.name}.tmp")
        state = {
            "range_levels": self.range_levels,
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

        loaded_levels = state.get("range_levels", {})
        self.range_levels = {
            "breakout": loaded_levels.get("breakout"),
            "breakdown": loaded_levels.get("breakdown"),
        }

    def _calc_range_position_score(self, current_price, range_low, range_high):
        if current_price < range_low or current_price > range_high:
            return {"long_score": 0, "short_score": 0, "position_bin": None, "reason": None}

        range_size = range_high - range_low
        if range_size <= 0:
            return {"long_score": 0, "short_score": 0, "position_bin": None, "reason": None}

        bins = self.config.range_position_bins
        position_ratio = (current_price - range_low) / range_size
        position_bin = int(position_ratio * bins) + 1
        position_bin = min(max(position_bin, 1), bins)

        edge_bins = self.config.range_edge_bins_no_score
        lower_score_bins = list(range(edge_bins + 1, bins // 2))
        upper_score_bins = list(range((bins // 2) + 2, bins - edge_bins + 1))

        if position_bin in lower_score_bins:
            distance_from_center = (bins // 2) - position_bin
            max_distance = (bins // 2) - (edge_bins + 1)
            score = self._scale_range_position_score(distance_from_center, max_distance)
            return {
                "long_score": score,
                "short_score": 0,
                "position_bin": position_bin,
                "reason": f"range_position_score bin {position_bin}/{bins} LONG +{score}",
            }

        if position_bin in upper_score_bins:
            distance_from_center = position_bin - ((bins // 2) + 1)
            max_distance = (bins - edge_bins) - ((bins // 2) + 1)
            score = self._scale_range_position_score(distance_from_center, max_distance)
            return {
                "long_score": 0,
                "short_score": score,
                "position_bin": position_bin,
                "reason": f"range_position_score bin {position_bin}/{bins} SHORT +{score}",
            }

        return {
            "long_score": 0,
            "short_score": 0,
            "position_bin": position_bin,
            "reason": f"range_position_score bin {position_bin}/{bins} score 0",
        }

    def _scale_range_position_score(self, distance_from_center, max_distance):
        if max_distance <= 0:
            return 0

        score = round(distance_from_center / max_distance * self.config.range_position_max_score)
        return min(max(score, 1), self.config.range_position_max_score)

    def _empty_range_result(self, reason):
        return {
            "long_score": 0,
            "short_score": 0,
            "block_trade": False,
            "score_components": {
                "breakout_long_score": 0,
                "breakdown_short_score": 0,
                "edge_penalty_long_score": 0,
                "edge_penalty_short_score": 0,
                "position_long_score": 0,
                "position_short_score": 0,
            },
            "range": None,
            "reasons": [reason],
        }

    def _pct_distance(self, price, reference_price):
        return abs(price - reference_price) / reference_price * 100

    def _safe_float(self, value, default=0.0):
        try:
            if value is None or value == "":
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _candle_direction(self, candle):
        if candle["close"] > candle["open"]:
            return "UP"

        if candle["close"] < candle["open"]:
            return "DOWN"

        return "FLAT"

    def _calculate_true_ranges(self, candles):
        true_ranges = []

        for index in range(1, len(candles)):
            candle = candles[index]
            previous_close = candles[index - 1]["close"]
            true_range = max(
                candle["high"] - candle["low"],
                abs(candle["high"] - previous_close),
                abs(candle["low"] - previous_close),
            )
            true_ranges.append(true_range)

        return true_ranges

    def _calculate_atr_values(self, true_ranges, period):
        atr_values = []

        for index in range(period - 1, len(true_ranges)):
            atr_window = true_ranges[index - period + 1 : index + 1]
            atr_values.append(sum(atr_window) / period)

        return atr_values

    def calc_price_position_score(self, ohlcv_data, current_candle=None):
        if current_candle is None:
            if len(ohlcv_data) < 2:
                return {
                    "long_score": 0,
                    "short_score": 0,
                    "indicators": {},
                    "reasons": ["price_position skipped: not enough candles"],
                }

            reference_candles = ohlcv_data[:-1]
            current_candle = ohlcv_data[-1]
        else:
            if len(ohlcv_data) < 1:
                return {
                    "long_score": 0,
                    "short_score": 0,
                    "indicators": {},
                    "reasons": ["price_position skipped: no reference candles"],
                }

            reference_candles = ohlcv_data

        periods = [
            ("1y", 365, 3),
            ("6m", 182, 2),
            ("1m", 30, 1),
        ]

        long_score = 0
        short_score = 0
        reasons = []
        current_price = current_candle["close"]
        period_levels = {}

        for label, days, weight in periods:
            candles = reference_candles[-days:]
            if not candles:
                continue

            period_high = max(candle["high"] for candle in candles)
            period_low = min(candle["low"] for candle in candles)
            period_levels[label] = {
                "high": period_high,
                "low": period_low,
                "candle_count": len(candles),
            }

            if current_price > period_high:
                long_score += weight
                reasons.append(f"price_position {label} high breakout LONG +{weight}")

            if current_price < period_low:
                short_score += weight
                reasons.append(f"price_position {label} low breakdown SHORT +{weight}")

        if not reasons:
            reasons.append("price_position 0: inside previous ranges")

        return {
            "long_score": long_score,
            "short_score": short_score,
            "indicators": {
                "current_price": current_price,
                "period_levels": period_levels,
            },
            "reasons": reasons,
        }

    def calc_body_score(self, ohlcv_data, current_candle=None):
        if current_candle is None:
            if len(ohlcv_data) < 2:
                return {
                    "long_score": 0,
                    "short_score": 0,
                    "indicators": {},
                    "reasons": ["body_score skipped: not enough candles"],
                }

            reference_candles = ohlcv_data[:-1]
            current_candle = ohlcv_data[-1]
        else:
            if len(ohlcv_data) < 1:
                return {
                    "long_score": 0,
                    "short_score": 0,
                    "indicators": {},
                    "reasons": ["body_score skipped: no reference candles"],
                }

            reference_candles = ohlcv_data

        current_body = abs(current_candle["close"] - current_candle["open"])
        reference_bodies = [abs(candle["close"] - candle["open"]) for candle in reference_candles]
        score = self._percentile_to_score(current_body, reference_bodies)
        indicators = {
            "body": current_body,
            "direction": self._candle_direction(current_candle),
            "reference_count": len(reference_bodies),
        }

        if current_candle["close"] > current_candle["open"]:
            return {
                "long_score": score,
                "short_score": 0,
                "indicators": indicators,
                "reasons": [f"body_score LONG +{score}"],
            }

        if current_candle["close"] < current_candle["open"]:
            return {
                "long_score": 0,
                "short_score": score,
                "indicators": indicators,
                "reasons": [f"body_score SHORT +{score}"],
            }

        return {
            "long_score": 0,
            "short_score": 0,
            "indicators": indicators,
            "reasons": ["body_score 0: current candle is doji"],
        }

    def calc_volume_score(self, ohlcv_data, current_candle=None, day_progress=None, current_time=None):
        if current_candle is None:
            if len(ohlcv_data) < 2:
                return {
                    "long_score": 0,
                    "short_score": 0,
                    "indicators": {},
                    "reasons": ["volume_score skipped: not enough candles"],
                }

            reference_candles = ohlcv_data[:-1]
            current_candle = ohlcv_data[-1]
            expected_volume = current_candle["volume"]
        else:
            if len(ohlcv_data) < 1:
                return {
                    "long_score": 0,
                    "short_score": 0,
                    "indicators": {},
                    "reasons": ["volume_score skipped: no reference candles"],
                }

            reference_candles = ohlcv_data
            if day_progress is None:
                day_progress = self._calculate_day_progress(current_candle, current_time)
            expected_volume = self._estimate_final_volume(current_candle["volume"], day_progress)

        reference_volumes = [candle["volume"] for candle in reference_candles]
        score = self._percentile_to_score(expected_volume, reference_volumes)
        indicators = {
            "current_volume": current_candle["volume"],
            "expected_final_volume": expected_volume,
            "day_progress": day_progress,
            "reference_count": len(reference_volumes),
            "direction": self._candle_direction(current_candle),
        }

        if current_candle["close"] > current_candle["open"]:
            return {
                "long_score": score,
                "short_score": 0,
                "indicators": indicators,
                "reasons": [f"volume_score LONG +{score} expected_volume={expected_volume:.2f}"],
            }

        if current_candle["close"] < current_candle["open"]:
            return {
                "long_score": 0,
                "short_score": score,
                "indicators": indicators,
                "reasons": [f"volume_score SHORT +{score} expected_volume={expected_volume:.2f}"],
            }

        return {
            "long_score": 0,
            "short_score": 0,
            "indicators": indicators,
            "reasons": ["volume_score 0: current candle is doji"],
        }

    def _estimate_final_volume(self, current_volume, day_progress):
        if day_progress is None:
            return current_volume

        if day_progress <= 0:
            return current_volume

        if day_progress > 1:
            day_progress = 1

        return current_volume / day_progress

    def _calculate_day_progress(self, current_candle, current_time=None):
        candle_open_time = current_candle.get("open_time", current_candle.get("timestamp"))
        if candle_open_time is None:
            return None

        start_time = self._parse_time(candle_open_time)
        now = self._parse_time(current_time) if current_time is not None else datetime.now(timezone.utc)

        elapsed_seconds = (now - start_time).total_seconds()
        day_seconds = 24 * 60 * 60
        progress = elapsed_seconds / day_seconds

        if progress <= 0:
            return 0
        if progress > 1:
            return 1

        return progress

    def _parse_time(self, value):
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, timezone.utc)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise TypeError(f"Unsupported time value: {value!r}")

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    def _percentile_to_score(self, current_value, reference_values):
        if not reference_values:
            return 0

        lower_or_equal_count = sum(1 for value in reference_values if value <= current_value)
        percentile = lower_or_equal_count / len(reference_values) * 100

        if percentile < 30:
            return 0
        if percentile < 40:
            return 1
        if percentile < 50:
            return 2
        if percentile < 60:
            return 3
        if percentile < 70:
            return 4
        if percentile < 80:
            return 5

        return 6

    def get_state(self, long_score: int, short_score: int) -> str:
        if long_score >= self.config.entry_long_score and long_score - short_score >= self.config.entry_score_gap:
            return "LONG"

        if short_score >= self.config.entry_short_score and short_score - long_score >= self.config.entry_score_gap:
            return "SHORT"

        return "HOLD"

    def get_signal(self, long_score: int, short_score: int) -> str:
        if long_score >= self.config.entry_long_score and long_score - short_score >= self.config.entry_score_gap:
            return "ENTER_LONG"

        if short_score >= self.config.entry_short_score and short_score - long_score >= self.config.entry_score_gap:
            return "ENTER_SHORT"

        return "NO_TRADE"
