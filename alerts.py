"""Open or close the health alerts as GitHub issues, after a pipeline run.

One issue per source that failed 3 checks in a row, closed again when the
source works; one when this month's API spend passes 80% of --monthly-budget.
Uses the gh command (GH_TOKEN in the GitHub job). --dry-run reads the open
issues but changes nothing. The database is data/dev.db unless --db says
otherwise; it is only read.
"""

import argparse
import os
import subprocess
import sys

from monitor.alerts import GhCli, sync_alerts
from monitor.db import DEFAULT_DB, open_database


def run_url():
    """The link to this GitHub Actions run, or None outside GitHub."""
    parts = [os.environ.get(name) for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")]
    return f"{parts[0]}/{parts[1]}/actions/runs/{parts[2]}" if all(parts) else None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"database file, relative to the project folder (default: {DEFAULT_DB})")
    parser.add_argument("--monthly-budget", type=float, metavar="USD",
                        help="the monthly API budget; without it, no spend alert")
    parser.add_argument("--dry-run", action="store_true", help="read the open issues, but open or close nothing")
    args = parser.parse_args()
    conn = open_database(args.db)
    try:
        sync_alerts(conn, GhCli(), budget=args.monthly_budget, run_url=run_url(), dry_run=args.dry_run)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        detail = getattr(e, "stderr", "") or str(e)
        sys.exit(f"Health alerts failed: the gh command didn't work ({detail.strip()})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
