"""Export compact research updates; confirm downloads separately; merge offline."""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid
import zipfile


TABLE_KEYS = {
    "market_state": ("timestamp", "symbol"),
    "prediction_forecast": ("symbol", "schedule_key"),
    "prediction_digest": ("symbol", "schedule_key"),
    "market_state_labels": ("timestamp", "symbol"),
}


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _date(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("A UTC offset is required, e.g. 2026-09-20T04:34:11+00:00")
    return parsed.astimezone(timezone.utc)


def _connect_read(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def _columns(connection, table):
    return [(row[1], row[2]) for row in connection.execute(f"PRAGMA table_info({_quote(table)})")]


def _ensure_table(connection, table, columns):
    if not columns:
        return
    keys = TABLE_KEYS[table]
    if not set(keys).issubset(name for name, kind in columns):
        raise ValueError(f"Missing keys in {table}")
    allowed = {"TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC", ""}
    if any(kind.upper() not in allowed for name, kind in columns):
        raise ValueError(f"Unsupported column type in {table}")
    has_id = table == "prediction_forecast" and "id" in dict(columns)
    declarations = [f"{_quote(name)} {kind}" + (" PRIMARY KEY AUTOINCREMENT" if has_id and name == "id" else "")
                    for name, kind in columns]
    declarations.append(("UNIQUE (" if has_id else "PRIMARY KEY (") + ",".join(map(_quote, keys)) + ")")
    connection.execute(f"CREATE TABLE IF NOT EXISTS {_quote(table)} ({','.join(declarations)})")
    existing = dict(_columns(connection, table))
    for name, kind in columns:
        if name not in existing:
            connection.execute(f"ALTER TABLE {_quote(table)} ADD COLUMN {_quote(name)} {kind}")


def _copy_rows(source, target, source_table, target_table, columns, where="", parameters=()):
    _ensure_table(target, target_table, columns)
    names = ",".join(_quote(name) for name, kind in columns)
    cursor = source.execute(f"SELECT {names} FROM {_quote(source_table)} {where}", parameters)
    count = 0
    while batch := cursor.fetchmany(500):
        target.executemany(f"INSERT INTO {_quote(target_table)} ({names}) VALUES ({','.join('?' for _ in columns)})", batch)
        count += len(batch)
    return count


def _hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_state(path, state):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def export_delta(source="work/data/btc_market_state.db", directory="work/exports", since=None):
    source, directory = Path(source).resolve(), Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "research_export_cursor.json"
    # Exclusive creation prevents two exports/confirmations from advancing the same stream.
    lock = directory / "research_export.lock"
    with lock.open("x") as handle:
        handle.close()  # Windows permits unlinking the reservation only after its handle is closed.
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            if state and state["source"] != str(source):
                raise ValueError("Export directory belongs to another source database")
            if state.get("pending"):
                archive = directory / state["pending"]["filename"]
                if not archive.exists() or _hash(archive) != state["pending"]["sha256"]:
                    raise ValueError("Pending archive is missing or changed; do not confirm it")
                return {"archive": str(archive), "pending": True, "manifest": state["pending"]["manifest"]}
            if state:
                if since and _date(since) != _date(state["received_until"]):
                    raise ValueError("Saved cursor differs from --since; omit --since")
                since = state["received_until"]
            if not since:
                raise ValueError("First export needs --since with the last received market timestamp")
            since = _date(since).isoformat()
            cutoff = (_date(since) - timedelta(days=1)).isoformat()
            export_id = uuid.uuid4().hex
            archive = directory / f"btc_market_state_delta_{export_id[:12]}.zip"
            with tempfile.TemporaryDirectory(prefix="research_delta_", dir=directory) as temporary:
                delta = Path(temporary) / "delta.db"
                with closing(_connect_read(source)) as src, closing(sqlite3.connect(delta)) as dst:
                    market_columns = _columns(src, "market_state")
                    if not market_columns:
                        raise ValueError("Source has no market_state table")
                    upper = src.execute("SELECT MAX(timestamp) FROM market_state").fetchone()[0]
                    if upper is None or _date(upper) < _date(since):
                        raise ValueError("Source is empty or older than the download cursor")
                    counts = {"market_state": _copy_rows(src, dst, "market_state", "market_state",
                              market_columns, "WHERE timestamp >= ?", (cutoff,))}
                    # Old rows have no updated_at. Resend only their small outcome columns
                    # so arbitrarily late labels are not missed without copying large JSON blobs.
                    labels = [(name, kind) for name, kind in market_columns
                              if name in ("timestamp", "symbol") or name.startswith(("future_", "return_"))]
                    counts["market_state_labels"] = _copy_rows(src, dst, "market_state", "market_state_labels", labels)
                    for table in ("prediction_forecast", "prediction_digest"):
                        columns = _columns(src, table)
                        if columns:
                            counts[table] = _copy_rows(src, dst, table, table, columns)
                    dst.commit()
                manifest = {"format_version": 1, "export_id": export_id,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "since": since, "overlap_from": cutoff, "until": upper,
                            "rows": counts, "delta_sha256": _hash(delta),
                            "previous_export_id": state.get("last_export_id")}
                with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as bundle:
                    bundle.write(delta, "delta.db")
                    bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            state = {**state, "source": str(source), "received_until": since,
                     "pending": {"filename": archive.name, "sha256": _hash(archive), "manifest": manifest}}
            _write_state(state_path, state)
            return {"archive": str(archive), "pending": False, "manifest": manifest}
        finally:
            lock.unlink(missing_ok=True)


def confirm_download(directory="work/exports", delete_export=False):
    directory = Path(directory).resolve()
    state_path = directory / "research_export_cursor.json"
    lock = directory / "research_export.lock"
    with lock.open("x") as handle:
        handle.close()
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            pending = state.get("pending")
            if not pending:
                raise ValueError("No pending export to confirm")
            archive = (directory / pending["filename"]).resolve()
            if archive.parent != directory or not archive.name.startswith("btc_market_state_delta_"):
                raise ValueError("Invalid export path")
            if _hash(archive) != pending["sha256"]:
                raise ValueError("Archive changed; cursor was not advanced")
            manifest = pending["manifest"]
            state.update(received_until=manifest["until"], last_export_id=manifest["export_id"], pending=None)
            _write_state(state_path, state)
            if delete_export:
                archive.unlink()
            return state["received_until"]
        finally:
            lock.unlink(missing_ok=True)


def _upsert(source, target, table):
    columns = _columns(source, table)
    if not columns:
        return
    # Local prediction IDs may differ; use the existing natural unique keys.
    _ensure_table(target, table, columns)
    columns = [(name, kind) for name, kind in columns if not (table == "prediction_forecast" and name == "id")]
    names = [name for name, kind in columns]
    keys = TABLE_KEYS[table]
    update = ",".join(f"{_quote(n)}=excluded.{_quote(n)}" for n in names if n not in keys)
    sql = (f"INSERT INTO {_quote(table)} ({','.join(map(_quote,names))}) VALUES ({','.join('?' for _ in names)}) "
           f"ON CONFLICT ({','.join(map(_quote,keys))}) DO UPDATE SET {update}")
    cursor = source.execute(f"SELECT {','.join(map(_quote,names))} FROM {_quote(table)}")
    while batch := cursor.fetchmany(500):
        target.executemany(sql, batch)


def merge_delta(base, archive, output):
    """Create a NEW offline merged DB. Never modify the supplied base or live DB."""
    base, archive, output = Path(base).resolve(), Path(archive).resolve(), Path(output).resolve()
    if not base.exists() or output.exists():
        raise ValueError("Base must exist and output must be a new filename")
    with tempfile.TemporaryDirectory(prefix="research_merge_") as temporary:
        delta = Path(temporary) / "delta.db"
        with zipfile.ZipFile(archive) as bundle:
            if bundle.getinfo("manifest.json").file_size > 65536:
                raise ValueError("Invalid manifest size")
            manifest = json.loads(bundle.read("manifest.json"))
            if manifest.get("format_version") != 1:
                raise ValueError("Unsupported export format")
            with bundle.open("delta.db") as source, delta.open("wb") as destination:
                shutil.copyfileobj(source, destination)
        if _hash(delta) != manifest["delta_sha256"]:
            raise ValueError("Downloaded database checksum mismatch")
        with closing(_connect_read(base)) as old, closing(_connect_read(delta)) as changes:
            last = old.execute("SELECT MAX(timestamp) FROM market_state").fetchone()[0]
            if last is None or not _date(manifest["since"]) <= _date(last) <= _date(manifest["until"]):
                raise ValueError("Wrong base: missing prior interval, or base is newer than this export")
            if _columns(old, "research_import"):
                previous = old.execute("SELECT export_id, created_at FROM research_import ORDER BY created_at DESC LIMIT 1").fetchone()
                if previous and previous[0] == manifest["export_id"]:
                    raise ValueError("This export is already merged")
                if previous and (previous[0] != manifest["previous_export_id"]
                                 or _date(previous[1]) > _date(manifest["created_at"])):
                    raise ValueError("Export chain mismatch: merge the downloads in order")
            # Exclusive create ensures an existing file can never be overwritten.
            with output.open("xb"):
                pass
            try:
                with closing(sqlite3.connect(output)) as result:
                    old.backup(result)
                    result.execute("BEGIN")
                    for table in ("market_state", "prediction_forecast", "prediction_digest"):
                        _upsert(changes, result, table)
                    labels = _columns(changes, "market_state_labels")
                    values = [(name, kind) for name, kind in labels if name not in ("timestamp", "symbol")]
                    _ensure_table(result, "market_state", [("timestamp", "TEXT"), ("symbol", "TEXT"), *values])
                    if values:
                        names = [name for name, kind in values]
                        sql = f"UPDATE market_state SET {','.join(_quote(n)+'=?' for n in names)} WHERE timestamp=? AND symbol=?"
                        cursor = changes.execute(f"SELECT {','.join(map(_quote,names))},timestamp,symbol FROM market_state_labels")
                        while batch := cursor.fetchmany(500):
                            result.executemany(sql, batch)
                    result.execute("CREATE TABLE IF NOT EXISTS research_import (export_id TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
                    result.execute("INSERT INTO research_import VALUES (?,?)", (manifest["export_id"], manifest["created_at"]))
                    result.commit()
                    if result.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("Merged database failed integrity check")
            except BaseException:
                output.unlink(missing_ok=True)
                raise
    return str(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--source", default="work/data/btc_market_state.db")
    export.add_argument("--directory", default="work/exports")
    export.add_argument("--since", help="First time only: last received market timestamp with UTC offset")
    confirm = sub.add_parser("confirm")
    confirm.add_argument("--directory", default="work/exports")
    confirm.add_argument("--downloaded", action="store_true", required=True)
    confirm.add_argument("--delete-export", action="store_true", help="Delete only the confirmed server ZIP")
    merge = sub.add_parser("merge")
    merge.add_argument("--base", required=True)
    merge.add_argument("--archive", required=True)
    merge.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "export":
            result = export_delta(args.source, args.directory, args.since)
            print("PENDING: same archive, cursor unchanged" if result["pending"] else "CREATED: cursor unchanged until confirmation")
            print("DOWNLOAD:", result["archive"])
            print("SIZE_MB:", round(Path(result["archive"]).stat().st_size / 1024 / 1024, 2))
            print("MARKET_UNTIL:", result["manifest"]["until"])
        elif args.command == "confirm":
            print("CONFIRMED:", confirm_download(args.directory, args.delete_export))
        else:
            print("MERGED:", merge_delta(args.base, args.archive, args.output))
    except (ValueError, OSError, sqlite3.Error, KeyError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Transfer failed: {error}\n")


if __name__ == "__main__":
    main()
