"""Screen new and changed items with Claude Haiku: which ones contain relevant funding calls?

Calls become candidates for extraction (step 12); everything else goes to the
screening log. The database is data/dev.db unless --db says otherwise. The API
key comes from ANTHROPIC_API_KEY in the environment, or else from .env.
"""

import argparse
import sys

import anthropic

from monitor.db import DEFAULT_DB, open_database
from monitor.screening import StopScreening, dry_run, load_api_key, screen


# Seconds before a request that gets no answer is given up and retried; a
# request normally takes seconds (screening) to about a minute (extraction).
REQUEST_TIMEOUT = 300


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"database file, relative to the project folder (default: {DEFAULT_DB})")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the batches and a token and cost estimate, without calling the API")
    parser.add_argument("--limit", type=int, metavar="N", help="screen only the first N items")
    args = parser.parse_args()

    conn = open_database(args.db)
    if args.dry_run:
        dry_run(conn, args.limit)
        return
    api_key = load_api_key()
    if not api_key:
        sys.exit("No API key: set ANTHROPIC_API_KEY in the environment or in .env.")
    client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=REQUEST_TIMEOUT)
    try:
        screen(conn, client, args.limit)
    except StopScreening as e:
        sys.exit(str(e))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
