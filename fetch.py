"""Fetch the sources that are due and record new, changed and removed items for screening.

The database is data/dev.db unless --db says otherwise; only the GitHub job
uses data/funding.db. No language model is called in this step.
"""

import argparse
import json
from datetime import date

from monitor.changes import plan_changes, record_changes, record_check, record_failure, stored_items
from monitor.db import DEFAULT_DB, open_database, timestamp, utc_now
from monitor.http import Fetcher, FetchError
from monitor.readers import linked_page_excerpt, read_source

INTERVAL_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}


def is_due(source, today):
    """Due if never checked successfully, or if enough calendar days have passed."""
    if source["last_checked_at"] is None:
        return True
    last = date.fromisoformat(source["last_checked_at"][:10])
    return (today - last).days >= INTERVAL_DAYS[source["check_frequency"]]


def run(conn, fetcher, force=False, only=(), clock=utc_now, out=print):
    """Check the active sources that are due (all of them with force, or just
    those in `only`) and record what changed. Returns the run's id."""
    sources = conn.execute("SELECT * FROM sources WHERE status = 'active' ORDER BY id").fetchall()
    if only:
        unknown = set(only) - {s["id"] for s in sources}
        if unknown:
            raise SystemExit(f"Not an active source: {', '.join(sorted(unknown))}")
        sources = [s for s in sources if s["id"] in only]

    now = clock()
    started = timestamp(now)
    with conn:
        run_id = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (started,)).lastrowid
    out(f"Run {run_id}, started {started}")
    checked = with_changes = 0
    totals = {"new": 0, "changed": 0, "removed": 0}
    problems = []
    for source in sources:
        source_id = source["id"]
        if not (force or only) and not is_due(source, now.date()):
            out(f"  {source_id:<20} not due ({source['check_frequency']})")
            continue
        checked += 1
        try:
            snapshot = read_source(fetcher, source)
        except Exception as e:
            # Expected failures say what went wrong; for anything else the
            # exception type helps track down the bug.
            message = str(e) if isinstance(e, (FetchError, ValueError)) else f"{type(e).__name__}: {e}"
            record_failure(conn, source_id, message)
            with conn:
                record_check(conn, run_id, source_id, timestamp(now), error=message)
            problems.append({"source_id": source_id, "message": message})
            out(f"  {source_id:<20} FAILED: {message}")
            continue

        stored = stored_items(conn, source_id)
        changes = plan_changes(stored, snapshot)
        for change in changes:
            # No texts are stored, so a changed entry needs its excerpt again too.
            if change.kind != "removed" and change.item.needs_excerpt:
                try:
                    page = linked_page_excerpt(fetcher, change.url, source["content_selector"])
                except Exception as e:
                    problems.append({"source_id": source_id, "url": change.url,
                                     "message": f"no excerpt: {e}"})
                    continue
                change.item.text = change.text = f"{change.item.title}\n\n{page}"
        with conn:
            record_changes(conn, run_id, source_id, snapshot, changes, stored, timestamp(now))
            record_check(conn, run_id, source_id, timestamp(now), snapshot)

        counts = {kind: sum(c.kind == kind for c in changes) for kind in totals}
        for kind, n in counts.items():
            totals[kind] += n
        with_changes += bool(changes)
        out(f"  {source_id:<20} {len(snapshot.items):>4} items: "
            + ", ".join(f"{n} {kind}" for kind, n in counts.items())
            + ("" if snapshot.complete else " (incomplete, so nothing marked removed)"))

    with conn:
        conn.execute("UPDATE runs SET finished_at = ?, sources_checked = ?, pages_changed = ?, "
                     "errors = ?, error_details = ? WHERE id = ?",
                     (timestamp(clock()), checked, with_changes, len(problems),
                      json.dumps(problems), run_id))
    failed = sum("url" not in p for p in problems)
    out(f"Checked {checked} sources: {with_changes} with changes, {failed} failed. Items: "
        + ", ".join(f"{n} {kind}" for kind, n in totals.items())
        + (f". {len(problems) - failed} excerpts could not be fetched." if len(problems) > failed else "."))
    return run_id


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"database file, relative to the project folder (default: {DEFAULT_DB})")
    parser.add_argument("--force", action="store_true",
                        help="check every active source, whether due or not")
    parser.add_argument("--source", action="append", default=[], metavar="ID",
                        help="check only this source (can be repeated)")
    args = parser.parse_args()

    conn = open_database(args.db)
    run(conn, Fetcher(), force=args.force, only=args.source)
    conn.close()


if __name__ == "__main__":
    main()
