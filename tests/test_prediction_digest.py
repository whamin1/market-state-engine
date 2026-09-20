from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3
import tempfile
from pathlib import Path
import unittest

from market_state_engine.prediction_research import MarketStateForecaster
from market_state_engine.prediction_summary import (
    format_prediction_digest, summarize_recent_actuals, summarize_score_changes,
)
from market_state_engine.state_recorder import CREATE_MARKET_STATE_TABLE, ensure_market_state_extensions


def forecast_at(timestamp, strategy="test_v1"):
    return {"source_timestamp": timestamp.isoformat(), "symbol": "BTCUSDT", "price": 77000,
            "long_score": 4, "short_score": 10, "atr_activity_score": 2, "core_score_diff": -6,
            "strategy_version": strategy, "forecast_version": "per_side_changes_v2",
            "horizons": {name: {"score_changes": {
                "LONG": summarize_score_changes(4, [(4, 9)], 1),
                "SHORT": summarize_score_changes(10, [(10, 7)], 1),
            }} for name in ("15m", "1h", "4h")}}


def record_at(timestamp, strategy="test_v1"):
    return {"source_timestamp": timestamp.isoformat(), "created_at": timestamp.isoformat(),
            "forecast": forecast_at(timestamp, strategy), "actual_outcomes": {}}


class DigestFormattingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 12, 3, 2, tzinfo=timezone.utc)

    def test_sections_spread_short_horizons_and_rolling_targets(self):
        forecast = forecast_at(self.now)
        previous = record_at(self.now - timedelta(hours=1))
        previous["forecast"]["horizons"]["4h"]["score_changes"]["LONG"]["median_score"] = 7
        context = {"previous_forecast": previous, "previous_snapshot": {
            "timestamp": (self.now - timedelta(hours=1)).isoformat(), "strategy_version": "test_v1",
            "long_score": 3, "short_score": 8}, "recent_actuals": []}
        message = format_prediction_digest(forecast, [], {}, self.now, context=context)
        headings = ["① 현재 상태", "② 4시간 전망", "③ 단기 전망", "④ 지난 변화", "⑤ 예측 성적"]
        self.assertEqual([message.index(h) for h in headings], sorted(message.index(h) for h in headings))
        for part in ("BTC: 77,000", "점수 차이 -6", "예상 점수 차이: +2 (현재보다 +8)",
                     "LONG: 3 → 4 (+1)", "SHORT: 8 → 10 (+2)", "LONG 예상 7 → 9 (+2)",
                     "1시간 뒤: LONG 9 (+5) / SHORT 7 (-3)", "15분 뒤: LONG 9",
                     "지난 예측 목표: 09-12 15:02 KST", "이번 예측 목표: 09-12 16:02 KST",
                     "같은 목표 시각의 수정 예측이 아닙니다"):
            self.assertIn(part, message)
        short_section = message[message.index("③"):message.index("④")]
        self.assertNotIn("확률", short_section)
        self.assertLessEqual(len(message.encode("utf-16-le")) // 2, 4096)

    def test_new_confirmation_window_not_forecast_creation_or_last_update(self):
        record = record_at(self.now - timedelta(hours=9))
        record["actual_labeled_at"] = self.now.isoformat()
        record["actual_outcomes"] = {
            "15m": {"timestamp": (self.now - timedelta(hours=8, minutes=45)).isoformat(),
                    "long_score": 1, "short_score": 1, "confirmed_at": (self.now - timedelta(hours=7)).isoformat()},
            "4h": {"timestamp": (self.now - timedelta(hours=5)).isoformat(),
                   "long_score": 11, "short_score": 4, "confirmed_at": self.now.isoformat()},
        }
        groups = summarize_recent_actuals([record], self.now)
        self.assertEqual(groups[0]["horizons"]["15m"]["LONG"]["count"], 0)
        self.assertEqual(groups[0]["horizons"]["4h"]["LONG"], {"count": 1, "mae": 2})
        self.assertEqual(groups[0]["horizons"]["4h"]["SHORT"]["mae"], 3)
        later = summarize_recent_actuals([record], self.now + timedelta(hours=6))
        self.assertEqual(later[0]["horizons"]["4h"]["LONG"]["count"], 0)

    def test_pending_and_wrong_strategy_do_not_contribute_error(self):
        record = record_at(self.now - timedelta(hours=1))
        record["actual_outcomes"] = {
            "4h": {"timestamp": (self.now + timedelta(hours=3)).isoformat(), "long_score": 99, "short_score": 99},
            "15m": {"timestamp": (self.now - timedelta(minutes=45)).isoformat(),
                    "long_score": 99, "short_score": 99, "report_strategy_matches": False},
        }
        group = summarize_recent_actuals([record], self.now)[0]
        self.assertEqual(group["horizons"]["4h"]["LONG"]["count"], 0)
        self.assertEqual(group["horizons"]["15m"]["LONG"]["count"], 0)

    def test_legacy_confirmation_fallback_and_version_groups(self):
        old = record_at(self.now - timedelta(hours=5))
        old["actual_outcomes"]["4h"] = {"timestamp": (self.now - timedelta(hours=1)).isoformat(),
                                         "long_score": 9, "short_score": 7}
        other = deepcopy(old)
        other["forecast"]["strategy_version"] = "test_v2"
        groups = summarize_recent_actuals([old, other], self.now)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["legacy_time_count"], 1)
        message = format_prediction_digest(forecast_at(self.now), [], {}, self.now,
                                           context={"recent_actuals": groups})
        self.assertIn("실제 시장 시각으로 집계", message)

    def test_missing_data_and_multiple_versions_fit_telegram(self):
        groups = []
        for i in range(5):
            old = record_at(self.now - timedelta(hours=5), f"strategy_{i}")
            for name, minutes in (("15m", 15), ("1h", 60), ("4h", 240)):
                old["actual_outcomes"][name] = {"timestamp": (self.now - timedelta(hours=5)
                                                              + timedelta(minutes=minutes)).isoformat(),
                                                 "long_score": 7, "short_score": 8, "confirmed_at": self.now.isoformat()}
            groups.extend(summarize_recent_actuals([old], self.now))
        message = format_prediction_digest(forecast_at(self.now), [], {}, self.now, context={"recent_actuals": groups})
        self.assertLessEqual(len(message.encode("utf-16-le")) // 2, 4096)
        self.assertIn("그 외 2개 버전", message)
        self.assertIn("1시간 전 비교: 자료 없음", message)
        self.assertIn("현재 예측 없음", format_prediction_digest(None, [], {}, self.now))


class DigestStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "report.db"
        self.start = datetime(2026, 9, 12, tzinfo=timezone.utc)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(CREATE_MARKET_STATE_TABLE)
            ensure_market_state_extensions(c)
        self.insert(self.start)
        self.forecaster = MarketStateForecaster(self.db, min_case_count=1)
        self.forecaster.save_forecast(forecast_at(self.start), "first", created_at=self.start)

    def insert(self, time, long_score=4, short_score=10):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("""INSERT INTO market_state
                (timestamp,symbol,strategy_version,schema_version,long_score,short_score)
                VALUES (?,'BTCUSDT','test_v1',2,?,?)""", (time.isoformat(), long_score, short_score))

    def label(self, name, minutes):
        end = self.start + timedelta(minutes=minutes)
        self.insert(end, 11, 4)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(f"""UPDATE market_state SET future_{name}_timestamp=?, future_{name}_long_score=11,
                future_{name}_short_score=4, future_{name}_core_score_diff=7 WHERE timestamp=?""",
                      (end.isoformat(), self.start.isoformat()))

    def test_confirmed_times_are_per_horizon_stable_and_forecast_is_preserved(self):
        before = self.forecaster.get_schedule_record("BTCUSDT", "first")["forecast"]
        for name, minutes in (("15m", 15), ("1h", 60), ("4h", 240)):
            self.label(name, minutes)
            now = self.start + timedelta(minutes=minutes + 1)
            self.forecaster.refresh_actual_outcomes(now=now)
        record = self.forecaster.get_schedule_record("BTCUSDT", "first")
        self.assertEqual(record["forecast"], before)
        self.assertEqual(record["actual_outcomes"]["15m"]["confirmed_at"], (self.start + timedelta(minutes=16)).isoformat())
        self.assertEqual(record["actual_outcomes"]["1h"]["confirmed_at"], (self.start + timedelta(minutes=61)).isoformat())
        restarted = MarketStateForecaster(self.db)
        restarted.refresh_actual_outcomes(now=now + timedelta(hours=1))
        self.assertEqual(restarted.get_schedule_record("BTCUSDT", "first")["actual_outcomes"], record["actual_outcomes"])

    def test_old_prediction_confirmed_recently_is_included_and_read_context_is_nonmutating(self):
        self.label("4h", 240)
        now = self.start + timedelta(days=2)
        self.forecaster.refresh_actual_outcomes(now=now)
        context = self.forecaster.report_context("BTCUSDT", None, now)
        self.assertEqual(context["recent_actuals"][0]["horizons"]["4h"]["LONG"]["count"], 1)
        self.assertIsNone(context["previous_snapshot"])
        record = self.forecaster.get_schedule_record("BTCUSDT", "first")
        self.assertNotIn("report_strategy_matches", record["actual_outcomes"]["4h"])

    def test_context_reads_real_one_hour_snapshot_and_previous_saved_forecast(self):
        now = self.start + timedelta(hours=2)
        self.insert(now - timedelta(hours=1), 3, 8)
        previous = forecast_at(now - timedelta(minutes=30))
        self.forecaster.save_forecast(previous, "previous", created_at=now - timedelta(minutes=30))
        context = self.forecaster.report_context("BTCUSDT", forecast_at(now), now)
        self.assertEqual(context["previous_snapshot"]["long_score"], 3)
        self.assertEqual(context["previous_forecast"]["forecast"], previous)


if __name__ == "__main__":
    unittest.main()
