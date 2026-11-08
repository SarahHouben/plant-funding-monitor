"""Apply re-extracted calls, closures and the daily refresh of deadlines (step 13), without model calls.

The database is data/dev.db unless --db says otherwise. --dry-run shows what
would change and saves nothing.
"""

import argparse

from monitor.db import DEFAULT_DB, open_database
from monitor.updates import run_update


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"database file, relative to the project folder (default: {DEFAULT_DB})")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, and save nothing")
    args = parser.parse_args()
    conn = open_database(args.db)
    try:
        run_update(conn, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
