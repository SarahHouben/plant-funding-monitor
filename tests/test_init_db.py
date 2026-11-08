"""Tests for init_db.py and schema.sql.

Run from the repo root with: python3 tests/test_init_db.py

The tests copy schema.sql, init_db.py and the two CSVs to a temporary folder,
so data/funding.db is never touched. They use the real CSVs, so they also
check that those load, but they don't depend on what the CSVs contain.
"""

import csv
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FILES = ["schema.sql", "init_db.py", "sources.csv", "tags.csv"]
NOW = "2026-11-07T06:00:00Z"

# A valid call; tests override single fields.
CALL = {
    "title": "Testaufruf",
    "title_en": "Test call",
    "call_type": "deadline_bound",
    "status": "open",
    "deadlines": '[{"stage": "sketch", "date": "2027-01-14", "note": "12:00 CET"}]',
    "next_deadline": "2027-01-14",
    "summary_en": "Summary.",
    "summary_de": "Zusammenfassung.",
    "apply_via": "easy-Online",
    "apply_url": "https://example.org/apply",
    "more_info_links": '[{"label": "Funding guideline", "url": "https://example.org/guideline"}]',
    "contact_point": "Example agency, unit 321, grants@example.org",
    "relevance_score": 4,
    "first_seen_at": NOW,
    "last_verified_at": NOW,
    "evidence": '{"deadlines": ["Skizzen bis 14.01.2027"]}',
}


def check(label, ok):
    if not ok:
        print(f"FAIL {label}")
        sys.exit(1)
    print(f"PASS {label}")


def init(work):
    """Run init_db.py in the work folder and return (exit code, output)."""
    result = subprocess.run([sys.executable, str(work / "init_db.py")],
                            capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


@contextmanager
def db(work):
    """Open the work database with foreign keys on; commit on success, then close."""
    conn = sqlite3.connect(work / "data" / "dev.db")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def write_csv(path, fieldnames, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def error_from(work, sql, params):
    """Run one statement and return the database error it raises, or "no error"."""
    try:
        with db(work) as c:
            c.execute(sql, params)
    except sqlite3.Error as e:
        return str(e)
    return "no error"


def insert_call(conn, **fields):
    row = {**CALL, **fields}
    row.setdefault("canonical_url", row["original_url"])
    sql = (f"INSERT INTO calls ({', '.join(row)}) "
           f"VALUES ({', '.join(':' + k for k in row)})")
    return conn.execute(sql, row).lastrowid


def run_tests(work):
    source_fields, sources = read_csv(work / "sources.csv")
    tag_fields, tags = read_csv(work / "tags.csv")
    removed_source, other_source = sources[0]["id"], sources[1]["id"]
    first_tag = {}
    for tag in tags:
        first_tag.setdefault(tag["group"], tag)
    removed_tag = first_tag["career_stage"]["tag_id"]
    groups = ["research_area", "organism", "funding_type", "career_stage"]

    code, out = init(work)
    check("first run loads the real CSVs", code == 0)
    with db(work) as c:
        counts = [c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ["sources", "tags"]]
    check(f"every CSV row is loaded ({len(sources)} sources, {len(tags)} tags)",
          counts == [len(sources), len(tags)])

    # What a pipeline run leaves behind: source state, a run, two calls with
    # tags, and a dropped item.
    with db(work) as c:
        c.execute("UPDATE sources SET content_hash = 'abc', last_checked_at = ?, "
                  "last_error = 'timeout', consecutive_failures = 2 WHERE id IN (?, ?)",
                  (NOW, removed_source, other_source))
        run_id = c.execute("INSERT INTO runs (started_at, finished_at) VALUES (?, ?)",
                           (NOW, NOW)).lastrowid
        call_id = insert_call(c, source_id=removed_source,
                              original_url="https://example.org/call-1")
        c.executemany("INSERT INTO call_tags (call_id, tag_id) VALUES (?, ?)",
                      [(call_id, t["tag_id"]) for t in first_tag.values()])
        insert_call(c, source_id=other_source, original_url="https://example.org/call-2",
                    title_en=None, call_type="recurring", next_deadline="2027-03-01",
                    deadlines='[{"stage": "application", "annual_date": "03-01"}, '
                              '{"stage": "application", "annual_date": "08-01"}]')
        c.executemany("INSERT INTO screening_log (run_id, stage, source_id, title, url, "
                      "category, reason, screened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      [(run_id, "screening", other_source, "News item",
                        "https://example.org/news", "not_a_call", "A news item, not a call", NOW),
                       (run_id, "extraction", other_source, "Programme overview",
                        "https://example.org/overview", "not_a_call",
                        "A general programme description with no call", NOW)])
    with db(work) as c:
        stages = c.execute("SELECT deadlines FROM calls "
                           "WHERE original_url = 'https://example.org/call-2'").fetchone()[0]
    check("a recurring call stores annual_date stages",
          [s["annual_date"] for s in json.loads(stages)] == ["03-01", "08-01"])

    # Delete a source and a tag that are both in use, and edit a note.
    write_csv(work / "sources.csv", source_fields,
              [{**s, "notes": "Edited note"} if s["id"] == other_source else s
               for s in sources if s["id"] != removed_source])
    write_csv(work / "tags.csv", tag_fields,
              [t for t in tags if t["tag_id"] != removed_tag])
    code, out = init(work)
    check("second run after deleting a source and a tag from the CSVs", code == 0)
    with db(work) as c:
        state = c.execute("SELECT status, content_hash, last_checked_at, last_error, "
                          "consecutive_failures FROM sources WHERE id = ?",
                          (removed_source,)).fetchone()
        kept = c.execute("SELECT notes, content_hash, last_checked_at, last_error, "
                         "consecutive_failures FROM sources WHERE id = ?",
                         (other_source,)).fetchone()
        retired = c.execute("SELECT retired FROM tags WHERE tag_id = ?", (removed_tag,)).fetchone()
        counts = [c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ["calls", "call_tags", "screening_log", "runs"]]
    check("deleted source is marked removed and keeps its pipeline state",
          state == ("removed", "abc", NOW, "timeout", 2))
    check("source still in the CSV picks up its edited note and keeps its pipeline state",
          kept == ("Edited note", "abc", NOW, "timeout", 2))
    check("deleted tag is marked retired, not deleted", retired == (1,))
    check("calls, call_tags, screening_log and runs are untouched", counts == [2, 4, 2, 1])

    # Restoring the CSVs clears both flags.
    for name in ["sources.csv", "tags.csv"]:
        shutil.copy(REPO / name, work / name)
    code, out = init(work)
    check("third run with the original CSVs", code == 0)
    with db(work) as c:
        state = c.execute("SELECT status, content_hash FROM sources WHERE id = ?",
                          (removed_source,)).fetchone()
        retired = c.execute("SELECT retired FROM tags WHERE tag_id = ?", (removed_tag,)).fetchone()
    check("restored source gets its CSV status back and keeps its state",
          state == (sources[0]["status"], "abc"))
    check("restored tag is no longer retired", retired == (0,))

    with db(work) as c:
        c.execute("UPDATE calls SET review_status = 'rejected' "
                  "WHERE original_url = 'https://example.org/call-2'")
        rows = c.execute("SELECT id, apply_via, apply_url, contact_point, more_info_links, "
                         "research_areas, organisms, funding_types, career_stages "
                         "FROM calls_search").fetchall()
    expected = [(call_id, CALL["apply_via"], CALL["apply_url"], CALL["contact_point"],
                 json.loads(CALL["more_info_links"]), *([first_tag[g]["label"]] for g in groups))]
    check("calls_search shows the application and contact fields, one array of tag "
          "labels per group, and no rejected calls",
          [(*r[:4], *map(json.loads, r[4:])) for r in rows] == expected)

    # Data the schema must reject, with the error it should give.
    bad_calls = [
        ("a duplicate canonical_url", {"original_url": "https://example.org/call-1"},
         "UNIQUE constraint failed: calls.canonical_url"),
        ("an unknown call_type", {"call_type": "rolling"}, "call_type IN"),
        ("a relevance score of 6", {"relevance_score": 6}, "relevance_score BETWEEN"),
        ("a date that isn't YYYY-MM-DD", {"next_deadline": "14.01.2027"}, "date(next_deadline)"),
        ("an impossible date", {"next_deadline": "2027-02-30"}, "date(next_deadline)"),
        ("a timestamp that isn't UTC", {"first_seen_at": "2026-11-07 06:00:00"}, "first_seen_at"),
        ("malformed JSON", {"deadlines": "[{"}, "json_valid(deadlines)"),
        ("deadlines that aren't an array", {"deadlines": '{"stage": "x"}'}, "json_type(deadlines)"),
        ("evidence that isn't an object", {"evidence": "[]"}, "json_type(evidence)"),
        ("more_info_links that aren't an array", {"more_info_links": '{"label": "x"}'},
         "json_type(more_info_links)"),
    ]
    for label, fields, expected_error in bad_calls:
        error = "no error"
        try:
            with db(work) as c:
                insert_call(c, **{"source_id": other_source,
                                  "original_url": "https://example.org/call-3", **fields})
        except sqlite3.Error as e:
            error = str(e)
        check(f"rejects {label}", expected_error in error)

    error = error_from(work, "INSERT INTO call_tags (call_id, tag_id) VALUES (?, 'not-a-tag')",
                       (call_id,))
    check("rejects a tag that isn't in the tags table", "FOREIGN KEY constraint failed" in error)

    error = error_from(work, "INSERT INTO screening_log (run_id, stage, source_id, url, category, "
                             "reason, screened_at) VALUES (?, 'review', ?, 'https://example.org/x', "
                             "'not_a_call', 'x', ?)", (run_id, other_source, NOW))
    check("rejects a screening_log stage other than screening or extraction",
          "stage IN ('screening', 'extraction')" in error)

    # A bad value in the last CSV row stops init_db.py, and the edit to the
    # first row is rolled back with it.
    bad_sources = [dict(s) for s in sources]
    bad_sources[0]["notes"] = "Should not land"
    bad_sources[-1]["scope"] = "nationwide"
    write_csv(work / "sources.csv", source_fields, bad_sources)
    code, out = init(work)
    with db(work) as c:
        landed = c.execute("SELECT count(*) FROM sources "
                           "WHERE notes = 'Should not land'").fetchone()[0]
    check("a bad CSV value stops init_db.py and changes nothing",
          code != 0 and "scope IN" in out and landed == 0)
    shutil.copy(REPO / "sources.csv", work / "sources.csv")

    with db(work) as c:
        newer = c.execute("PRAGMA user_version").fetchone()[0] + 1
        c.execute(f"PRAGMA user_version = {newer}")
    code, out = init(work)
    check("a schema version mismatch is refused",
          code != 0 and f"schema version {newer}" in out)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for name in FILES:
            shutil.copy(REPO / name, work / name)
        run_tests(work)
    print("All checks passed.")


if __name__ == "__main__":
    main()
