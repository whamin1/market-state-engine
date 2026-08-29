import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_state_engine.future_labeler import FutureStateLabeler
from market_state_engine.prediction_research import MarketStateForecaster, PredictionResearchScheduler
from market_state_engine.state_recorder import CREATE_MARKET_STATE_TABLE, ensure_market_state_extensions


class PredictionResearchTests(unittest.TestCase):
    def test_labeler_fills_actual_future_scores_returns_and_core_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.db"
            self._create_database(db_path)
            start = datetime(2026, 8, 29, tzinfo=timezone.utc)
            self._insert(db_path, start, 100.0, 10, 4, 1, 0)
            self._insert(db_path, start + timedelta(minutes=15), 101.0, 14, 3, 1, 0)
            self._insert(db_path, start + timedelta(hours=1), 98.0, 4, 12, 0, 2)
            self._insert(db_path, start + timedelta(hours=4), 103.0, 16, 3, 1, 0)

            counts = FutureStateLabeler(db_path).label_available(now=start + timedelta(hours=4, minutes=1))

            self.assertEqual(counts, {"15m": 1, "1h": 1, "4h": 1})
            connection = sqlite3.connect(db_path)
            try:
                row = connection.execute(
                    """
                    SELECT future_15m_long_score, future_1h_short_score, future_4h_timestamp,
                           future_15m_core_score_diff, return_1h, future_4h_entry_condition
                    FROM market_state WHERE timestamp = ?
                    """,
                    (start.isoformat(),),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row[0], 14)
            self.assertEqual(row[1], 12)
            self.assertEqual(row[3], 10)
            self.assertEqual(row[4], -2.0)
            self.assertEqual(row[5], 1)

    def test_forecast_counts_similar_labeled_cases_without_using_atr_direction_bonus(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.db"
            self._create_database(db_path)
            start = datetime(2026, 8, 29, tzinfo=timezone.utc)
            for index, future_scores in enumerate(((14, 4), (4, 13), (10, 4))):
                source_time = start + timedelta(minutes=index)
                self._insert(db_path, source_time, 100.0, 10, 4, 1, 0)
                self._insert(db_path, source_time + timedelta(minutes=15), 101.0, future_scores[0], future_scores[1], 1, 0)
                self._insert(db_path, source_time + timedelta(hours=1), 101.0, future_scores[0], future_scores[1], 1, 0)
                self._insert(db_path, source_time + timedelta(hours=4), 101.0, future_scores[0], future_scores[1], 1, 0)

            FutureStateLabeler(db_path).label_available(now=start + timedelta(hours=4, minutes=5))
            self._insert(db_path, start + timedelta(hours=5), 100.0, 10, 4, 1, 0)
            forecaster = MarketStateForecaster(db_path, min_case_count=1)
            forecast = forecaster.forecast_for_snapshot(forecaster.latest_snapshot())

            horizon = forecast["horizons"]["15m"]
            self.assertEqual(horizon["case_count"], 3)
            self.assertTrue(horizon["ready"])
            self.assertAlmostEqual(horizon["long_strengthen_pct"], 33.333, places=2)
            self.assertAlmostEqual(horizon["short_strengthen_pct"], 33.333, places=2)

    def test_scheduler_uses_six_hour_kst_schedule_and_is_separate_from_trading(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.db"
            self._create_database(db_path)
            now = datetime(2026, 8, 29, 15, 0, tzinfo=timezone.utc)
            self._insert(db_path, now - timedelta(minutes=1), 100.0, 10, 4, 1, 0)
            result = PredictionResearchScheduler(db_path, min_case_count=1).run_once(now=now)

            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["schedule_key"], "2026-08-30-00")
            self.assertIn("current_scores:", result["message"])

    @staticmethod
    def _create_database(db_path):
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(CREATE_MARKET_STATE_TABLE)
            ensure_market_state_extensions(connection)
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _insert(db_path, timestamp, price, long_score, short_score, long_activity, short_activity):
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                """
                INSERT OR IGNORE INTO market_state (
                    timestamp, symbol, strategy_version, schema_version, price,
                    long_score, short_score, atr_activity_score,
                    activity_direction_long_score, activity_direction_short_score,
                    strategy_config_json
                ) VALUES (?, 'BTCUSDT', 'test_v1', 2, ?, ?, ?, 2, ?, ?, ?)
                """,
                (timestamp.isoformat(), price, long_score, short_score, long_activity, short_activity,
                 '{"entry_long_score":10,"entry_short_score":10,"entry_score_gap":5}'),
            )
            connection.commit()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
