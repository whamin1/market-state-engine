import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from market_state_engine.future_labeler import FutureStateLabeler
from market_state_engine.prediction_research import (
    FORECAST_VERSION, MarketStateForecaster, PredictionResearchScheduler,
)
from market_state_engine.prediction_summary import (
    evaluate_forecasts, format_prediction_digest, summarize_score_changes,
)
from market_state_engine.state_recorder import CREATE_MARKET_STATE_TABLE, ensure_market_state_extensions


class HourlyPredictionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "state.db"
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(CREATE_MARKET_STATE_TABLE)
            ensure_market_state_extensions(connection)
        self.start = datetime(2026, 9, 12, tzinfo=timezone.utc)

    def insert(self, timestamp, long_score=9, short_score=4):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                """INSERT INTO market_state
                   (timestamp, symbol, strategy_version, schema_version, price, long_score,
                    short_score, atr_activity_score, activity_direction_long_score, activity_direction_short_score)
                   VALUES (?, 'BTCUSDT', 'test_v1', 2, 100.0, ?, ?, 2, 0, 0)""",
                (timestamp.isoformat(), long_score, short_score),
            )

    def label(self, source_time, minutes, long_score, short_score, target_time=None):
        horizon = {15: "15m", 60: "1h", 240: "4h"}[minutes]
        target_time = target_time or source_time + timedelta(minutes=minutes)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                f"""UPDATE market_state SET future_{horizon}_timestamp = ?,
                   future_{horizon}_long_score = ?, future_{horizon}_short_score = ?,
                   future_{horizon}_core_score_diff = ?, future_{horizon}_entry_condition = 0,
                   return_{horizon} = 0.5 WHERE timestamp = ?""",
                (target_time.isoformat(), long_score, short_score, long_score - short_score, source_time.isoformat()),
            )

    def test_each_hour_is_saved_but_only_digest_hours_send_and_restart_deduplicates(self):
        scheduler = PredictionResearchScheduler(self.db, min_case_count=1)
        with patch("market_state_engine.prediction_research.send_prediction_alert", return_value=True) as send:
            for hour in range(4):  # 09, 10, 11, 12 KST
                now = self.start + timedelta(hours=hour, minutes=2)
                self.insert(now - timedelta(seconds=30))
                result = scheduler.run_once(now=now, send_telegram=True)
                self.assertEqual(result["status"], "sent" if hour == 3 else "recorded")
            restarted = PredictionResearchScheduler(self.db, min_case_count=1)
            restarted.run_once(now=now + timedelta(seconds=10), send_telegram=True)
            self.assertEqual(send.call_count, 1)
            self.assertLessEqual(len(send.call_args.args[0].encode("utf-16-le")) // 2, 4096)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM prediction_forecast").fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT count(*) FROM prediction_digest").fetchone()[0], 1)

    def test_all_four_kst_digest_hours_and_only_those_hours(self):
        scheduler = PredictionResearchScheduler(self.db, min_case_count=1)
        sent_hours = []
        with patch("market_state_engine.prediction_research.send_prediction_alert", return_value=True) as send:
            for hour in range(24):
                now = self.start + timedelta(hours=hour, minutes=2)
                self.insert(now - timedelta(seconds=30))
                if scheduler.run_once(now=now, send_telegram=True)["status"] == "sent":
                    sent_hours.append((hour + 9) % 24)
            self.assertEqual(sorted(sent_hours), [0, 6, 12, 18])
            self.assertEqual(send.call_count, 4)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM prediction_forecast").fetchone()[0], 24)
            self.assertEqual(connection.execute("SELECT count(*) FROM prediction_digest").fetchone()[0], 4)

    def test_snapshot_older_than_two_minutes_does_not_create_forecast(self):
        self.insert(self.start - timedelta(hours=3))
        result = PredictionResearchScheduler(self.db).run_once(now=self.start)
        self.assertEqual(result["status"], "missing_snapshot")
        self.assertIsNone(result["forecast"])
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM prediction_forecast").fetchone()[0], 0)

    def test_missing_database_is_not_created_by_research(self):
        missing = Path(self.directory.name) / "missing.db"
        result = PredictionResearchScheduler(missing).run_once(now=self.start)
        self.assertEqual(result["status"], "missing_database")
        self.assertFalse(missing.exists())

    def test_forecasts_exclude_future_information_and_respect_side_start_bands(self):
        older = self.start - timedelta(hours=5)
        self.insert(older, 8, 4)
        self.label(older, 15, 15, 4)
        other_band = older + timedelta(minutes=1)
        self.insert(other_band, 10, 4)
        self.label(other_band, 15, 20, 4)
        leaked = self.start - timedelta(minutes=5)
        self.insert(leaked, 9, 4)
        self.label(leaked, 15, 30, 4)  # These results are in the backup but unknown at prediction time.
        self.insert(self.start, 9, 4)
        forecaster = MarketStateForecaster(self.db, min_case_count=1)
        forecast = forecaster.forecast_for_snapshot(forecaster.latest_snapshot())
        horizon = forecast["horizons"]["15m"]
        self.assertEqual(horizon["case_count"], 2)
        self.assertEqual(horizon["score_changes"]["LONG"]["case_count"], 1)
        self.assertEqual(horizon["score_changes"]["LONG"]["median_score"], 16)
        self.assertEqual(horizon["score_changes"]["LONG"]["up_7_pct"], 100)
        self.assertEqual(horizon["score_changes"]["LONG"]["up_10_pct"], 0)
        self.assertEqual(horizon["score_changes"]["SHORT"]["up_7_pct"], 0)
        self.assertEqual(forecast["forecast_version"], FORECAST_VERSION)

    def test_actuals_fill_per_horizon_without_counting_unknown_as_false(self):
        self.insert(self.start)
        forecaster = MarketStateForecaster(self.db, min_case_count=1)
        forecast = forecaster.forecast_for_snapshot(forecaster.latest_snapshot())
        key = "2026-09-12-09"
        forecaster.save_forecast(forecast, key, created_at=self.start)
        for minutes, long_score, short_score in [(15, 16, 4), (60, 2, 14), (240, 20, 0)]:
            self.label(self.start, minutes, long_score, short_score)
        forecaster.refresh_actual_outcomes(now=self.start + timedelta(minutes=16))
        record = forecaster.get_schedule_record("BTCUSDT", key)
        self.assertEqual(record["actual_outcomes"]["15m"]["long_score_change"], 7)
        self.assertIsNone(record["actual_outcomes"]["1h"]["timestamp"])
        self.assertIsNone(record["actual_outcomes"]["1h"]["entry_condition"])
        self.assertEqual(record["outcomes_complete"], 0)
        forecaster.refresh_actual_outcomes(now=self.start + timedelta(minutes=61))
        record = forecaster.get_schedule_record("BTCUSDT", key)
        self.assertEqual(record["actual_outcomes"]["1h"]["short_score_change"], 10)
        self.assertIsNone(record["actual_outcomes"]["4h"]["timestamp"])
        forecaster.refresh_actual_outcomes(now=self.start + timedelta(minutes=241))
        self.assertEqual(forecaster.get_schedule_record("BTCUSDT", key)["outcomes_complete"], 1)

    def test_labeler_does_not_read_a_target_after_its_asof_time(self):
        self.insert(self.start)
        self.insert(self.start + timedelta(minutes=16), 16, 4)
        counts = FutureStateLabeler(self.db).label_available(now=self.start + timedelta(minutes=15, seconds=30))
        self.assertEqual(counts["15m"], 0)

    def test_side_probabilities_distinguish_seven_and_ten_point_changes(self):
        item = summarize_score_changes(12, [(12, 2), (12, 5), (12, 12), (12, 19), (12, 22)], 5)
        self.assertTrue(item["ready"])
        self.assertEqual(item["up_7_pct"], 40)
        self.assertEqual(item["down_7_pct"], 40)
        self.assertEqual(item["up_10_pct"], 20)
        self.assertEqual(item["down_10_pct"], 20)
        self.assertEqual(item["median_score"], 12)
        self.assertEqual(item["start_band"], "10_plus")
        self.assertFalse(summarize_score_changes(4, [], 1)["ready"])

    def test_projected_changes_cannot_fall_below_zero_score(self):
        item = summarize_score_changes(6, [(7, 0)], 1)
        self.assertEqual(item["median_score"], 0)
        self.assertEqual(item["median_change"], -6)
        self.assertEqual(item["down_7_pct"], 0)
        self.assertEqual(item["stable_7_pct"], 100)

    def test_evaluation_keeps_maturity_start_band_and_thresholds_separate(self):
        self.insert(self.start, 4, 14)
        forecaster = MarketStateForecaster(self.db, min_case_count=1)
        forecast = forecaster.forecast_for_snapshot(forecaster.latest_snapshot())
        for horizon in forecast["horizons"].values():
            horizon["ready"] = True
            horizon["long_strengthen_pct"] = 100
            horizon["short_strengthen_pct"] = 0
            horizon["stable_pct"] = 0
            horizon["score_changes"] = {
                "LONG": summarize_score_changes(4, [(4, 11)], 1),
                "SHORT": summarize_score_changes(14, [(14, 4)], 1),
            }
        record = {"source_timestamp": self.start.isoformat(), "created_at": self.start.isoformat(),
                  "schedule_key": "2026-09-12-09", "forecast": forecast,
                  "actual_outcomes": {"15m": {"timestamp": (self.start + timedelta(minutes=15)).isoformat(),
                                              "long_score": 11, "short_score": 6, "core_score_change": 15}}}
        now = self.start + timedelta(minutes=30)
        evaluation = evaluate_forecasts([record], now)
        stats = evaluation["horizons"]["15m"]
        self.assertEqual(stats["baseline"]["hits"], 1)
        self.assertEqual(stats["sides"]["LONG"]["7"]["hits"], 1)
        self.assertEqual(stats["sides"]["SHORT"]["7"]["hits"], 1)
        self.assertEqual(stats["sides"]["SHORT"]["10"]["hits"], 0)
        self.assertEqual(stats["sides"]["LONG"]["bands"]["0_9"]["count"], 1)
        self.assertEqual(stats["sides"]["LONG"]["bands"]["10_plus"]["count"], 0)
        self.assertEqual(evaluation["horizons"]["4h"]["pending"], 1)
        self.assertEqual(evaluation["horizons"]["4h"]["baseline"]["count"], 0)
        message = format_prediction_digest(forecast, [record] * 6, evaluation, now)
        self.assertIn("LONG 현재 4 → 예상 11 (+7)", message)
        self.assertIn("SHORT 현재 14 → 예상 4 (-10)", message)
        self.assertLessEqual(len(message.encode("utf-16-le")) // 2, 4096)

    def test_failed_telegram_send_retries_without_recalculating_saved_prediction(self):
        now = self.start + timedelta(hours=3, minutes=2)
        self.insert(now - timedelta(seconds=30))
        scheduler = PredictionResearchScheduler(self.db, min_case_count=1)
        with patch("market_state_engine.prediction_research.send_prediction_alert", side_effect=[False, True]) as send:
            first = scheduler.run_once(now=now, send_telegram=True)
            self.insert(now + timedelta(seconds=10), 20, 0)
            second = scheduler.run_once(now=now + timedelta(seconds=20), send_telegram=True)
            self.assertEqual(first["status"], "ready")
            self.assertEqual(second["status"], "sent")
            self.assertEqual(first["forecast"], second["forecast"])
            self.assertEqual(send.call_args_list[0].args[0], send.call_args_list[1].args[0])

    def test_existing_forecast_json_survives_database_migration(self):
        legacy_json = '{"legacy":true}'
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("""CREATE TABLE prediction_forecast (
                id INTEGER PRIMARY KEY, symbol TEXT, schedule_key TEXT, created_at TEXT,
                source_timestamp TEXT, source_price REAL, source_long_score INTEGER, source_short_score INTEGER,
                source_atr_activity_score INTEGER, source_core_score_diff INTEGER, strategy_version TEXT,
                forecast_json TEXT, actual_outcomes_json TEXT, actual_labeled_at TEXT, telegram_sent_at TEXT,
                UNIQUE(symbol, schedule_key))""")
            connection.execute("INSERT INTO prediction_forecast (symbol,schedule_key,forecast_json) VALUES ('BTCUSDT','old',?)",
                               (legacy_json,))
        MarketStateForecaster(self.db)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            saved = connection.execute("SELECT forecast_json,forecast_version,outcomes_complete FROM prediction_forecast").fetchone()
        self.assertEqual(saved, (legacy_json, "legacy_core_gap_v1", 0))


if __name__ == "__main__":
    unittest.main()
