"""Create the database from schema.sql and load sources.csv and tags.csv.

The database is data/dev.db unless --db says otherwise; only the GitHub job
uses data/funding.db. Safe to run again, for example after editing a CSV: it adds and updates rows
from the CSVs and never touches pipeline state, calls, the screening log or
runs. A row deleted from a CSV is flagged rather than deleted, because calls
may still point to it.
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = "data/dev.db"
SCHEMA_PATH = ROOT / "schema.sql"
SOURCES_CSV = ROOT / "sources.csv"
TAGS_CSV = ROOT / "tags.csv"

SOURCE_COLUMNS = ["id", "funder", "programme", "url", "access_method", "content_selector",
                  "scope", "family", "check_frequency", "status", "notes"]
TAG_CSV_COLUMNS = ["group", "per_call", "tag_id", "label", "description"]
TABLES = ["sources", "tags", "calls", "call_tags", "screening_log", "runs",
          "source_items", "item_changes", "candidates", "call_updates", "call_listings",
          "call_history", "call_flags"]


def read_csv(path, columns, key):
    """Return (line number, row) pairs, with empty values as None.

    Stops the script if the header is wrong or a key is missing or repeated.
    """
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if set(reader.fieldnames or []) != set(columns):
            sys.exit(f"{path.name}: expected columns {', '.join(columns)}; "
                     f"found {', '.join(reader.fieldnames or [])}")
        rows, seen = [], set()
        for row in reader:
            line = reader.line_num
            if None in row:
                sys.exit(f"{path.name} line {line}: more values than columns")
            row = {k: (v or "").strip() or None for k, v in row.items()}
            if row[key] is None:
                sys.exit(f"{path.name} line {line}: {key} is empty")
            if row[key] in seen:
                sys.exit(f"{path.name} line {line}: duplicate {key} {row[key]!r}")
            seen.add(row[key])
            rows.append((line, row))
    return rows


def upsert(conn, table, key, columns, rows, csv_name):
    """Insert each row, or update the given columns where the key exists."""
    sql = (f"INSERT INTO {table} ({', '.join(columns)}) "
           f"VALUES ({', '.join(':' + c for c in columns)}) "
           f"ON CONFLICT ({key}) DO UPDATE SET "
           + ", ".join(f"{c} = excluded.{c}" for c in columns if c != key))
    for line, row in rows:
        try:
            conn.execute(sql, row)
        except sqlite3.IntegrityError as e:
            sys.exit(f"{csv_name} line {line}: {e}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"database file, relative to the project folder (default: {DEFAULT_DB})")
    args = parser.parse_args()
    db_path = ROOT / args.db

    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    schema_version = int(re.search(r"PRAGMA user_version = (\d+)", schema).group(1))

    # Read both CSVs first, so a mistake in either stops us before the
    # database changes.
    sources = read_csv(SOURCES_CSV, SOURCE_COLUMNS, "id")
    tags = read_csv(TAGS_CSV, TAG_CSV_COLUMNS, "tag_id")
    for _, row in tags:
        row["tag_group"] = row.pop("group")
        row["retired"] = 0

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    db_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if db_version not in (0, schema_version):
        sys.exit(f"{args.db} has schema version {db_version}, but "
                 f"schema.sql is version {schema_version}. There is no migration "
                 f"yet: delete the database, which also deletes its results, and "
                 f"run init_db.py again.")
    conn.executescript(schema)

    with conn:
        upsert(conn, "sources", "id", SOURCE_COLUMNS, sources, SOURCES_CSV.name)
        upsert(conn, "tags", "tag_id", ["tag_id", "tag_group", "per_call", "label",
                                        "description", "retired"], tags, TAGS_CSV.name)

        removed = conn.execute(
            "UPDATE sources SET status = 'removed' WHERE status <> 'removed' "
            "AND id NOT IN (SELECT value FROM json_each(?)) RETURNING id",
            (json.dumps([row["id"] for _, row in sources]),)).fetchall()
        retired = conn.execute(
            "UPDATE tags SET retired = 1 WHERE retired = 0 "
            "AND tag_id NOT IN (SELECT value FROM json_each(?)) RETURNING tag_id",
            (json.dumps([row["tag_id"] for _, row in tags]),)).fetchall()

    for (source_id,) in removed:
        print(f"Source {source_id} is no longer in {SOURCES_CSV.name}: status set to removed")
    for (tag_id,) in retired:
        print(f"Tag {tag_id} is no longer in {TAGS_CSV.name}: marked as retired")

    print(f"{args.db} (schema version {schema_version}):")
    for table in TABLES:
        count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"  {table:<14} {count:>4} rows")
    conn.close()


if __name__ == "__main__":
    main()
