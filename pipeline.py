"""Run the daily pipeline: fetch, screen, extract and update, then print one summary.

All four stages record into one run. A failing source or candidate is normal
and doesn't stop the pipeline; a refused API key, a schema mismatch or a
database error does, and the summary still shows what ran. The database is
data/dev.db unless --db says otherwise.
"""

import argparse
import json
import sqlite3
import sys
import time

import anthropic

from fetch import run as fetch_run
from monitor.db import DEFAULT_DB, open_database, utc_now
from monitor.extraction import StopExtraction, extract
from monitor.extraction import dry_run as extraction_dry_run
from monitor.http import Fetcher
from monitor.screening import StopScreening, load_api_key, screen
from monitor.screening import cost as screening_cost
from monitor.screening import dry_run as screening_dry_run
from monitor.updates import run_update

FATAL = (StopScreening, StopExtraction, sqlite3.DatabaseError)


def _counts(pairs):
    return ", ".join(f"{what}: {n}" for what, n in pairs if n) or "nothing"


def _fetch_summary(conn, run_id):
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    failed = sum("stage" not in p and "url" not in p for p in json.loads(run["error_details"]))
    changes = dict(conn.execute("SELECT change, count(*) FROM item_changes WHERE run_id = ? GROUP BY change",
                                (run_id,)).fetchall())
    return (f"{run['sources_checked']} sources checked, {failed} failed; items: "
            f"{changes.get('new', 0)} new, {changes.get('changed', 0)} changed, {changes.get('removed', 0)} removed")


def _screening_summary(conn, run_id):
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    dropped = dict(conn.execute("SELECT category, count(*) FROM screening_log WHERE run_id = ? "
                                "AND stage = 'screening' GROUP BY category", (run_id,)).fetchall())
    calls = conn.execute("SELECT count(*) FROM candidates WHERE run_id = ?", (run_id,)).fetchone()[0]
    dollars = screening_cost(run["screening_input_tokens"], run["screening_output_tokens"],
                             run["screening_cache_read_tokens"], run["screening_cache_write_tokens"])
    text = (f"{calls} candidate calls, {dropped.get('not_a_call', 0)} not a call, "
            f"{dropped.get('not_relevant', 0)} not relevant; ${dollars:.4f}")
    return text, dollars


def run_pipeline(conn, fetcher, client, dry_run=False, max_cost=None, batch=False, limit=None, force=False,
                 verbose=False, clock=utc_now, sleep=time.sleep, out=print):
    """Run the four stages and print one summary. Returns the summary lines."""
    detail = out if verbose else (lambda line: None)
    lines, spent, run_id, stopped = [], 0.0, None, None
    try:
        if dry_run:
            lines.append("Fetch:      skipped (dry run)")
        else:
            run_id = fetch_run(conn, fetcher, force=force, clock=clock, out=detail)
            lines.append(f"Fetch:      {_fetch_summary(conn, run_id)}")

        if dry_run:
            estimate = []
            screening_dry_run(conn, clock=clock, out=lambda line: (estimate.append(line), detail(line)))
            lines.append(f"Screening:  {estimate[-1] if estimate else 'nothing waiting'}")
        else:
            screen(conn, client, clock=clock, out=detail, max_cost=max_cost)
            text, dollars = _screening_summary(conn, run_id)
            spent += dollars
            lines.append(f"Screening:  {text}")

        if limit == 0:
            lines.append("Extraction: skipped (--limit 0)")
        elif dry_run:
            estimate = []
            extraction_dry_run(conn, fetcher, limit=limit, clock=clock,
                               out=lambda line: (estimate.append(line), detail(line)))
            lines.append(f"Extraction: {estimate[-1] if estimate else 'nothing waiting'}")
        else:
            budget = None if max_cost is None else max(max_cost - spent, 0.0)
            tally = extract(conn, client, fetcher, limit=limit, batch=batch, max_cost=budget, clock=clock,
                            sleep=sleep, out=detail)
            if tally is None:
                lines.append("Extraction: nothing waiting")
            else:
                spent += tally.cost
                retried = f"; {tally.retried} retried" if tally.retried else ""
                lines.append(f"Extraction: {_counts(tally.outcomes.items())}{retried}; ${tally.cost:.4f}")

        stats = run_update(conn, clock=clock, out=detail, dry_run=dry_run)
        lines.append(f"Update:     {_counts(sorted(stats.items()))}"
                     + (" (dry run, nothing saved)" if dry_run else ""))
    except FATAL as e:
        stopped = f"{type(e).__name__}: {e}"

    header = f"Pipeline {'dry run' if dry_run else f'run {run_id}'}, {clock().strftime('%Y-%m-%d %H:%M')} UTC"
    out(header)
    for line in lines:
        out(f"  {line}")
    if not dry_run:
        out(f"  Cost:       ${spent:.4f}" + (f" (limit ${max_cost:.2f})" if max_cost is not None else ""))
    if run_id is not None:
        run = conn.execute("SELECT error_details, notes FROM runs WHERE id = ?", (run_id,)).fetchone()
        for label, entries in [("Problems:", json.loads(run[0])), ("Notes:", json.loads(run[1]))]:
            if entries:
                out(f"  {label:<11} {len(entries)}")
                for entry in entries[:10]:
                    out(f"    {entry.get('source_id') or entry.get('stage', '')}: {entry['message'][:150]}")
    if stopped:
        out(f"  STOPPED:    {stopped}")
    return lines, stopped


# Seconds before a request that gets no answer is given up and retried; a
# request normally takes seconds (screening) to about a minute (extraction).
REQUEST_TIMEOUT = 300


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"database file, relative to the project folder (default: {DEFAULT_DB})")
    parser.add_argument("--dry-run", action="store_true",
                        help="change nothing and call no API: skip fetching, estimate screening and "
                             "extraction, and show what the update stage would change")
    parser.add_argument("--max-cost", type=float, metavar="USD",
                        help="one budget for screening and extraction together")
    parser.add_argument("--batch", action="store_true", help="extract through Message Batches at half price")
    parser.add_argument("--limit", type=int, metavar="N", help="extract at most N candidates (0: none)")
    parser.add_argument("--force", action="store_true", help="fetch every active source, whether due or not")
    parser.add_argument("--verbose", action="store_true", help="also show each stage's own output")
    args = parser.parse_args()

    conn = open_database(args.db)
    client = None
    if not args.dry_run:
        api_key = load_api_key()
        if not api_key:
            sys.exit("No API key: set ANTHROPIC_API_KEY in the environment or in .env.")
        client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=REQUEST_TIMEOUT)
    try:
        _, stopped = run_pipeline(conn, Fetcher(), client, dry_run=args.dry_run, max_cost=args.max_cost,
                                  batch=args.batch, limit=args.limit, force=args.force, verbose=args.verbose)
    finally:
        conn.close()
    if stopped:
        sys.exit(1)


if __name__ == "__main__":
    main()
