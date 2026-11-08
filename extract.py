"""Extract the details of candidate calls with Claude, and store them in the calls table.

Fetches each candidate's call text politely (its page, PDF or EU topic), asks
the model for the structured details, checks them, and stores new calls. The
database is data/dev.db unless --db says otherwise. The API key comes from
ANTHROPIC_API_KEY in the environment, or else from .env.
"""

import argparse
import sys

import anthropic

from monitor.db import DEFAULT_DB, open_database
from monitor.extraction import DEFAULT_MODEL, StopExtraction, dry_run, extract
from monitor.http import Fetcher
from monitor.screening import load_api_key


# Seconds before a request that gets no answer is given up and retried; a
# request normally takes seconds (screening) to about a minute (extraction).
REQUEST_TIMEOUT = 300


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"database file, relative to the project folder (default: {DEFAULT_DB})")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model (default: {DEFAULT_MODEL})")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch the call texts and estimate tokens and cost, without calling the API")
    parser.add_argument("--limit", type=int, metavar="N", help="extract only the first N candidates")
    parser.add_argument("--candidate", type=int, action="append", metavar="ID",
                        help="extract only this candidate (can be repeated)")
    parser.add_argument("--batch", action="store_true",
                        help="use the Message Batches API at half price (results can take a while)")
    parser.add_argument("--max-cost", type=float, metavar="USD",
                        help="stop before the estimated spend of this run would pass this many US dollars")
    args = parser.parse_args()

    conn = open_database(args.db)
    try:
        if args.dry_run:
            dry_run(conn, Fetcher(), args.model, args.candidate, args.limit)
            return
        api_key = load_api_key()
        if not api_key:
            sys.exit("No API key: set ANTHROPIC_API_KEY in the environment or in .env.")
        client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=REQUEST_TIMEOUT)
        extract(conn, client, Fetcher(), args.model, args.candidate, args.limit, args.batch, args.max_cost)
    except StopExtraction as e:
        sys.exit(str(e))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
