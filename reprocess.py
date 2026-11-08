"""Reprocess without a full run: screen one source again, or extract chosen calls again.

--rescreen-source ID reads that source again, screens all its items as new and
rejects calls whose items are now not relevant. --reextract extracts the calls
chosen with --call, --flag and --closed-undated again and applies the results
through the update stage, so every change is recorded in call_history. The
database is data/dev.db unless --db says otherwise; --dry-run calls no API and
changes nothing.
"""

import argparse
import sys

import anthropic

from monitor.db import DEFAULT_DB, open_database
from monitor.extraction import StopExtraction
from monitor.http import Fetcher
from monitor.reprocess import FLAG_KINDS, reextract, rescreen_source
from monitor.screening import StopScreening, load_api_key

# Seconds before a request that gets no answer is given up and retried; a
# request normally takes seconds (screening) to about a minute (extraction).
REQUEST_TIMEOUT = 300


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"database file, relative to the project folder (default: {DEFAULT_DB})")
    task = parser.add_mutually_exclusive_group(required=True)
    task.add_argument("--rescreen-source", metavar="ID",
                      help="read this source again, screen all its items as new, and reject calls now found "
                           "not relevant")
    task.add_argument("--reextract", action="store_true",
                      help="extract the calls chosen with --call, --flag and --closed-undated again")
    parser.add_argument("--call", type=int, action="append", default=[], metavar="ID",
                        help="with --reextract: this call, even if rejected (can be repeated)")
    parser.add_argument("--flag", choices=FLAG_KINDS, help="with --reextract: calls with an open flag of this kind")
    parser.add_argument("--closed-undated", action="store_true",
                        help="with --reextract: closed calls without any dated stage")
    parser.add_argument("--batch", action="store_true",
                        help="with --reextract: use the Message Batches API at half price")
    parser.add_argument("--max-cost", type=float, metavar="USD",
                        help="stop before the estimated spend would pass this many US dollars")
    parser.add_argument("--dry-run", action="store_true",
                        help="call no API and change nothing: show what would be done, with a cost estimate")
    args = parser.parse_args()
    choices = args.call or args.flag or args.closed_undated
    if args.reextract and not choices:
        parser.error("--reextract needs --call, --flag or --closed-undated")
    if args.rescreen_source and (choices or args.batch):
        parser.error("--call, --flag, --closed-undated and --batch go with --reextract")

    conn = open_database(args.db)
    client = None
    try:
        if not args.dry_run:
            api_key = load_api_key()
            if not api_key:
                sys.exit("No API key: set ANTHROPIC_API_KEY in the environment or in .env.")
            client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=REQUEST_TIMEOUT)
        if args.rescreen_source:
            rescreen_source(conn, Fetcher(), client, args.rescreen_source, dry_run=args.dry_run,
                            max_cost=args.max_cost)
        else:
            reextract(conn, Fetcher(), client, args.call, args.flag, args.closed_undated, dry_run=args.dry_run,
                      max_cost=args.max_cost, batch=args.batch)
    except (StopScreening, StopExtraction) as e:
        sys.exit(str(e))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
