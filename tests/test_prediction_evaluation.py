from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from market_state_engine.prediction_evaluation import evaluate_database, format_evaluation
from market_state_engine.prediction_research import CREATE_PREDICTION_FORECAST_TABLE
from market_state_engine.state_recorder import CREATE_MARKET_STATE_TABLE, ensure_market_state_extensions


class PredictionEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "evaluation.db"
        self.start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.now = self.start + timedelta(days=3)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(CREATE_MARKET_STATE_TABLE)
            ensure_market_state_extensions(c)
            c.execute(CREATE_PREDICTION_FORECAST_TABLE)

    def add(self, hour=0, strategy="A", current=(10, 10), actual=(10, 10), predicted=(10, 10),
            classes=("stable", "stable"), target_strategy=None, labels=True, delay=0, ready=True,
            forecast_version="per_side_changes_v2"):
        start = self.start + timedelta(hours=hour)
        end = start + timedelta(hours=4, minutes=delay)
        items = {}
        for i, side in enumerate(("LONG", "SHORT")):
            items[side] = {"start_score": current[i], "median_score": predicted[i], "ready": ready}
            for threshold in (7, 10):
                for direction in ("up", "down", "stable"):
                    items[side][f"{direction}_{threshold}_pct"] = 100 if direction == classes[i] else 0
        forecast = {"forecast_version": forecast_version, "strategy_version": strategy,
                    "long_score": current[0], "short_score": current[1],
                    "horizons": {"4h": {"score_changes": items}}}
        with closing(sqlite3.connect(self.db)) as c, c:
            for timestamp, version, values in ((start, strategy, current), (end, target_strategy or strategy, actual)):
                c.execute("""INSERT OR IGNORE INTO market_state
                    (timestamp,symbol,strategy_version,schema_version,long_score,short_score)
                    VALUES (?,'BTCUSDT',?,2,?,?)""", (timestamp.isoformat(), version, *values))
            if labels:
                c.execute("""UPDATE market_state SET future_4h_timestamp=?,future_4h_long_score=?,
                    future_4h_short_score=? WHERE timestamp=? AND symbol='BTCUSDT'""",
                          (end.isoformat(), *actual, start.isoformat()))
            cursor = c.execute("""INSERT INTO prediction_forecast
                (symbol,schedule_key,created_at,source_timestamp,source_long_score,source_short_score,
                 strategy_version,forecast_version,forecast_json)
                VALUES ('BTCUSDT',?,?,?,?,?,?,?,?)""",
                (str(hour), (start + timedelta(seconds=30)).isoformat(), start.isoformat(), *current,
                 strategy, forecast_version, json.dumps(forecast)))
            return cursor.lastrowid

    def result(self, now=None):
        return evaluate_database(self.db, now=now or self.now)

    def test_mae_paired_baseline_improvement_and_strategy_separation(self):
        self.add(current=(4, 8), actual=(12, 6), predicted=(10, 7))
        self.add(hour=6, current=(4, 8), actual=(8, 4), predicted=(9, 7))
        self.add(hour=24, strategy="B", current=(4, 8), actual=(9, 8), predicted=(4, 8))
        result = self.result()
        a = result["strategies"]["A"]["all"]["sides"]
        self.assertEqual(a["LONG"]["count"], 2)
        self.assertEqual(a["LONG"]["forecast_mae"], 1.5)
        self.assertEqual(a["LONG"]["persistence_mae"], 6)
        self.assertEqual(a["LONG"]["improvement_pct"], 75)
        self.assertEqual(a["SHORT"]["forecast_mae"], 2)
        self.assertEqual(a["SHORT"]["persistence_mae"], 3)
        self.assertAlmostEqual(a["SHORT"]["improvement_pct"], 100 / 3)
        self.assertEqual(result["strategies"]["B"]["all"]["sides"]["LONG"]["improvement_pct"], 0)

    def test_non_overlapping_is_chronological_greedy_and_four_hours_apart(self):
        last = self.add(hour=8)
        overlap = self.add(hour=1)
        first = self.add(hour=0)
        middle = self.add(hour=4)
        group = self.result()["strategies"]["A"]
        self.assertEqual(group["all"]["sample_count"], 4)
        self.assertEqual(group["non_overlapping"]["forecast_ids"], [first, middle, last])
        self.assertNotIn(overlap, group["non_overlapping"]["forecast_ids"])

    def test_delayed_label_end_is_respected_in_non_overlapping_cohort(self):
        first = self.add(delay=5)
        self.add(hour=4)
        group = self.result()["strategies"]["A"]
        self.assertEqual(group["non_overlapping"]["forecast_ids"], [first])

    def test_pending_missing_and_legacy_are_not_errors(self):
        self.add(hour=0)
        self.add(hour=1, labels=False)
        self.add(hour=2, forecast_version="legacy_core_gap_v1")
        self.add(hour=4)
        group = self.result(self.start + timedelta(hours=6))["strategies"]["A"]
        self.assertEqual(group["all"]["sample_count"], 1)
        self.assertEqual(group["excluded"], {"pending": 1, "missing_actual": 1, "not_forecast_v2": 1})

    def test_cross_version_label_is_excluded(self):
        self.add(target_strategy="B")
        group = self.result()["strategies"]["A"]
        self.assertEqual(group["all"]["sample_count"], 0)
        self.assertEqual(group["excluded"]["strategy_mismatch"], 1)

    def test_zero_baseline_and_no_positive_predictions_are_undefined_not_zero(self):
        self.add(predicted=(12, 10))
        result = self.result()
        score = result["strategies"]["A"]["all"]["sides"]["LONG"]
        self.assertIsNone(score["improvement_pct"])
        self.assertIsNone(score["large_changes"]["7"]["up"]["precision"])
        self.assertIsNone(score["large_changes"]["7"]["up"]["recall"])
        self.assertIn("N/A", format_evaluation(result))
        json.dumps(result, allow_nan=False)

    def test_threshold_events_precision_recall_and_direction(self):
        self.add(current=(10, 10), actual=(17, 0), classes=("up", "down"))
        self.add(hour=6, current=(10, 10), actual=(10, 3), classes=("up", "stable"))
        self.add(hour=12, current=(10, 10), actual=(20, 20), classes=("stable", "down"))
        sides = self.result()["strategies"]["A"]["all"]["sides"]
        up = sides["LONG"]["large_changes"]["7"]["up"]
        self.assertEqual((up["actual_count"], up["predicted_count"], up["true_positive"]), (2, 2, 1))
        self.assertEqual((up["precision"], up["recall"]), (0.5, 0.5))
        ten = sides["LONG"]["large_changes"]["10"]["up"]
        self.assertEqual((ten["actual_count"], ten["true_positive"]), (1, 0))
        down = sides["SHORT"]["large_changes"]["7"]["down"]
        self.assertEqual((down["precision"], down["recall"]), (0.5, 0.5))
        any_change = sides["SHORT"]["large_changes"]["7"]["any_change"]
        self.assertEqual(any_change["precision"], 1)
        self.assertAlmostEqual(any_change["recall"], 2 / 3)

    def test_cached_actual_fallback_read_only_and_missing_probabilities(self):
        identity = self.add(labels=False)
        with closing(sqlite3.connect(self.db)) as c, c:
            forecast = json.loads(c.execute("SELECT forecast_json FROM prediction_forecast").fetchone()[0])
            del forecast["horizons"]["4h"]["score_changes"]["LONG"]["up_7_pct"]
            outcome = {"4h": {"timestamp": (self.start + timedelta(hours=4)).isoformat(),
                               "long_score": 10, "short_score": 10}}
            c.execute("UPDATE prediction_forecast SET forecast_json=?,actual_outcomes_json=? WHERE id=?",
                      (json.dumps(forecast), json.dumps(outcome), identity))
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        result = self.result()
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())
        score = result["strategies"]["A"]["all"]["sides"]["LONG"]
        self.assertEqual(score["count"], 1)
        self.assertEqual(score["large_changes"]["7"]["probability_missing_count"], 1)

    def test_missing_database_is_not_created(self):
        path = self.db.with_name("missing.db")
        with self.assertRaises(sqlite3.OperationalError):
            evaluate_database(path, now=self.now)
        self.assertFalse(path.exists())

    def test_unready_sides_and_negative_improvement(self):
        self.add(current=(10, 10), actual=(11, 11), predicted=(14, 14))
        self.add(hour=6, ready=False)
        group = self.result()["strategies"]["A"]
        self.assertEqual(group["excluded"]["no_evaluable_side"], 1)
        self.assertEqual(group["all"]["sides"]["LONG"]["improvement_pct"], -200)

    def test_legacy_schema_without_version_column_uses_json_version(self):
        self.add()
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("ALTER TABLE prediction_forecast DROP COLUMN forecast_version")
        self.assertEqual(self.result()["strategies"]["A"]["all"]["sample_count"], 1)

    def test_tied_probability_prefers_stable_and_unknown_strategy_is_not_pooled(self):
        identity = self.add(actual=(17, 10))
        other = self.add(hour=6)
        with closing(sqlite3.connect(self.db)) as c, c:
            forecast = json.loads(c.execute("SELECT forecast_json FROM prediction_forecast WHERE id=?",
                                           (identity,)).fetchone()[0])
            item = forecast["horizons"]["4h"]["score_changes"]["LONG"]
            item.update(up_7_pct=50, stable_7_pct=50, down_7_pct=0)
            c.execute("UPDATE prediction_forecast SET forecast_json=? WHERE id=?", (json.dumps(forecast), identity))
            c.execute("UPDATE prediction_forecast SET strategy_version=NULL WHERE id=?", (other,))
        result = self.result()
        self.assertEqual(result["unknown_strategy_excluded"], 1)
        metric = result["strategies"]["A"]["all"]["sides"]["LONG"]["large_changes"]["7"]["up"]
        self.assertEqual(metric["predicted_count"], 0)
        self.assertEqual(metric["recall"], 0)

    def test_strategy_switch_inside_window_is_excluded_even_if_end_version_matches(self):
        self.add()
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("""INSERT INTO market_state (timestamp,symbol,strategy_version,schema_version)
                VALUES (?,'BTCUSDT','B',2)""", ((self.start + timedelta(hours=2)).isoformat(),))
        self.assertEqual(self.result()["strategies"]["A"]["excluded"]["strategy_mismatch"], 1)


if __name__ == "__main__":
    unittest.main()
