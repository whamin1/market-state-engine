from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from market_state_engine.data_transfer import export_delta, confirm_download, merge_delta
from market_state_engine.state_recorder import CREATE_MARKET_STATE_TABLE
from market_state_engine.prediction_research import CREATE_PREDICTION_FORECAST_TABLE, CREATE_PREDICTION_DIGEST_TABLE


class DataTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "live.db"
        self.base = self.root / "received.db"
        self.exports = self.root / "exports"
        self.t = datetime(2026, 9, 20, tzinfo=timezone.utc)
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute(CREATE_MARKET_STATE_TABLE)
            c.execute(CREATE_PREDICTION_FORECAST_TABLE)
            c.execute(CREATE_PREDICTION_DIGEST_TABLE)
        self.add(-10)
        self.add(-2)
        self.add(0)
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute("""INSERT INTO prediction_forecast
                (symbol,schedule_key,created_at,source_timestamp,strategy_version,forecast_json)
                VALUES ('BTCUSDT','old',?,?,'v1','{}')""", ((self.t-timedelta(days=10)).isoformat(),)*2)
        with closing(sqlite3.connect(self.source)) as c, closing(sqlite3.connect(self.base)) as dest:
            c.backup(dest)
        self.add(2)
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute("""UPDATE market_state SET future_4h_long_score=17,future_4h_timestamp=?
                WHERE timestamp=?""", ((self.t-timedelta(days=10)+timedelta(hours=4)).isoformat(),
                                       (self.t-timedelta(days=10)).isoformat()))
            c.execute("UPDATE prediction_forecast SET actual_outcomes_json=?", ('{"4h":{"long_score":17}}',))

    def add(self, days):
        with closing(sqlite3.connect(self.source)) as c, c:
            c.execute("""INSERT INTO market_state (timestamp,symbol,strategy_version,schema_version,price,long_score)
                VALUES (?,'BTCUSDT','v1',2,100,4)""", ((self.t+timedelta(days=days)).isoformat(),))

    def export(self):
        return export_delta(self.source, self.exports, self.t.isoformat())

    def test_export_only_recent_full_rows_all_labels_and_does_not_change_source(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = self.export()
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(result["manifest"]["rows"]["market_state"], 2)
        self.assertEqual(result["manifest"]["rows"]["market_state_labels"], 4)
        self.assertEqual(result["manifest"]["rows"]["prediction_forecast"], 1)
        self.assertFalse(list(self.exports.glob("*.db")))
        state = json.loads((self.exports / "research_export_cursor.json").read_text())
        self.assertEqual(state["received_until"], self.t.isoformat())
        with zipfile.ZipFile(result["archive"]) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(set(z.namelist()), {"manifest.json", "delta.db"})

    def test_repeat_returns_same_archive_until_confirmation(self):
        first = self.export()
        self.add(3)
        second = export_delta(self.source, self.exports)
        self.assertTrue(second["pending"])
        self.assertEqual(first["archive"], second["archive"])
        self.assertEqual(len(list(self.exports.glob("*.zip"))), 1)

    def test_confirm_then_export_moves_cursor_and_only_deletes_confirmed_zip(self):
        first = self.export()
        unrelated = self.exports / "important.db"
        unrelated.write_bytes(b"keep")
        until = confirm_download(self.exports, delete_export=True)
        self.assertEqual(until, (self.t+timedelta(days=2)).isoformat())
        self.assertFalse(Path(first["archive"]).exists())
        self.assertTrue(unrelated.exists())
        self.add(3)
        second = export_delta(self.source, self.exports)
        self.assertEqual(second["manifest"]["since"], until)
        self.assertEqual(second["manifest"]["previous_export_id"], first["manifest"]["export_id"])

    def test_merge_preserves_history_updates_old_labels_and_source_base_are_unchanged(self):
        result = self.export()
        before = hashlib.sha256(self.base.read_bytes()).hexdigest()
        target = self.root / "merged.db"
        merge_delta(self.base, result["archive"], target)
        self.assertEqual(before, hashlib.sha256(self.base.read_bytes()).hexdigest())
        with closing(sqlite3.connect(target)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM market_state").fetchone()[0], 4)
            self.assertEqual(c.execute("SELECT future_4h_long_score FROM market_state ORDER BY timestamp LIMIT 1").fetchone()[0], 17)
            self.assertEqual(json.loads(c.execute("SELECT actual_outcomes_json FROM prediction_forecast").fetchone()[0])["4h"]["long_score"], 17)
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with self.assertRaises(ValueError):
            merge_delta(target, result["archive"], self.root / "duplicate.db")

    def test_sequential_merge_and_missing_interval_rejected(self):
        first = self.export()
        first_base = self.root / "first.db"
        merge_delta(self.base, first["archive"], first_base)
        confirm_download(self.exports)
        self.add(3)
        second = export_delta(self.source, self.exports)
        with self.assertRaises(ValueError):
            merge_delta(self.base, second["archive"], self.root / "gap.db")
        output = self.root / "second.db"
        merge_delta(first_base, second["archive"], output)
        with closing(sqlite3.connect(output)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM market_state").fetchone()[0], 5)

    def test_missing_source_and_existing_output_are_not_overwritten(self):
        missing = self.root / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            export_delta(missing, self.exports, self.t.isoformat())
        self.assertFalse(missing.exists())
        self.assertFalse((self.exports / "research_export.lock").exists())
        result = self.export()
        with self.assertRaises(ValueError):
            merge_delta(self.base, result["archive"], self.base)

    def test_modified_archive_cannot_advance_cursor(self):
        result = self.export()
        Path(result["archive"]).write_bytes(b"broken")
        with self.assertRaises(ValueError):
            confirm_download(self.exports, delete_export=True)
        state = json.loads((self.exports / "research_export_cursor.json").read_text())
        self.assertEqual(state["received_until"], self.t.isoformat())

    def test_export_lock_does_not_remove_another_process_lock(self):
        self.exports.mkdir()
        lock = self.exports / "research_export.lock"
        lock.touch()
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertTrue(lock.exists())


if __name__ == "__main__":
    unittest.main()
