"""Opening the database, and the timestamp format it stores."""

import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = "data/dev.db"
TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"
# Screening and extraction add their numbers to the latest run if it started
# this recently, since the daily job fetches, screens and extracts in a row.
SAME_RUN = timedelta(hours=6)


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(moment):
    return moment.strftime(TIMESTAMP)


def parse_timestamp(text):
    return datetime.strptime(text, TIMESTAMP).replace(tzinfo=timezone.utc)


def current_run(conn, now):
    """The run to record a step in: the latest one if it started within
    SAME_RUN, otherwise a new one. A new run is committed with the caller's
    next commit, so a dry run can roll it back."""
    latest = conn.execute("SELECT id, started_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if latest and now - parse_timestamp(latest["started_at"]) <= SAME_RUN:
        return latest["id"]
    return conn.execute("INSERT INTO runs (started_at) VALUES (?)", (timestamp(now),)).lastrowid


def open_database(db):
    """Open a database made by init_db.py (path relative to the project folder),
    stopping if it is missing or has a different schema version than schema.sql.

    Deleted content is overwritten with zeros (secure_delete): texts the
    pipeline clears, such as page texts after screening, would otherwise stay
    in the file's free space, and the file is public."""
    db_path = ROOT / db
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    expected = int(re.search(r"PRAGMA user_version = (\d+)", schema).group(1))
    if not db_path.exists():
        sys.exit(f"{db} doesn't exist. Create it with: python3 init_db.py --db {db}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA secure_delete = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != expected:
        sys.exit(f"{db} has schema version {version}, but schema.sql is version {expected}. "
                 f"Rebuild it with init_db.py.")
    return conn
