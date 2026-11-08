"""GitHub issues for the health problems that need a person: a source that has
failed ALERT_FAILURES checks in a row, and a month's API spend past ALERT_SPEND
of the budget.

Issues carry the health-alert label and a hidden marker naming what they are
about, so an open issue is never opened twice. A source's issue is closed once
the source works again; a spend issue is left for a person to close.
"""

import json
import re
import subprocess

from monitor.db import utc_now
from monitor.health import ALERT_FAILURES, ALERT_SPEND, health_report

LABEL = "health-alert"
LABEL_COLOR = "d93f0b"
LABEL_DESCRIPTION = "Raised by the daily job's health check"
MARKER = re.compile(r"<!-- plant-funding-monitor:(\S+) -->")


def marker(key):
    return f"<!-- plant-funding-monitor:{key} -->"


class GhCli:
    """GitHub through the gh command; in the job, GH_TOKEN authenticates it."""

    def __init__(self, run=subprocess.run):
        self.run = run

    def _gh(self, *args):
        return self.run(["gh", *args], capture_output=True, text=True, check=True).stdout

    def labels(self):
        return [label["name"] for label in json.loads(self._gh("label", "list", "--limit", "500", "--json", "name"))]

    def create_label(self, name, color, description):
        self._gh("label", "create", name, "--color", color, "--description", description)

    def open_issues(self, label):
        return json.loads(self._gh("issue", "list", "--label", label, "--state", "open", "--limit", "500",
                                   "--json", "number,title,body"))

    def create_issue(self, title, body, label):
        self._gh("issue", "create", "--title", title, "--body", body, "--label", label)

    def close_issue(self, number, comment):
        self._gh("issue", "close", str(number), "--comment", comment)


def source_issue(source, run_url):
    title = f"Source failing: {source['id']}"
    body = (f"The daily job couldn't read **{source['funder']}** (`{source['id']}`) for {source['failures']} "
            f"checks in a row.\n\n"
            f"- Source page: {source['url']}\n"
            f"- Last error: `{source['error']}`\n"
            f"- Last checked: {source['last_checked_at']}\n"
            + (f"- Run: {run_url}\n" if run_url else "")
            + "\nCheck whether the page moved or was redesigned, then fix `url` or `content_selector` in "
              "`sources.csv`. This issue closes by itself once the source works again.\n\n"
            + marker(f"source:{source['id']}"))
    return title, body


def spend_issue(report, run_url):
    share = report["spend"] / report["budget"]
    title = f"API spend passed {ALERT_SPEND:.0%} of the monthly budget ({report['month']})"
    body = (f"This month's API spend is about **${report['spend']:.2f}**, {share:.0%} of the "
            f"${report['budget']:.2f} monthly budget ({report['month']}).\n\n"
            f"It is worked out from the tokens in the runs table at standard prices, so runs that used Message "
            f"Batches count at twice their real cost. The Anthropic Console limit stops all API calls once it is "
            f"reached, for the daily job and local runs alike.\n"
            + (f"\nRun: {run_url}\n" if run_url else "")
            + "\n" + marker(f"spend:{report['month']}"))
    return title, body


def sync_alerts(conn, github, budget=None, run_url=None, dry_run=False, clock=utc_now, out=print):
    """Open the issues the health report calls for and close those of sources
    that work again. A dry run reads the open issues but changes nothing.
    Returns the actions as (verb, title) pairs."""
    report = health_report(conn, budget=budget, clock=clock)
    has_label = LABEL in github.labels()
    open_by_key = {}
    for issue in github.open_issues(LABEL) if has_label else []:
        found = MARKER.search(issue.get("body") or "")
        if found:
            open_by_key[found.group(1)] = issue

    wanted = {f"source:{s['id']}": source_issue(s, run_url) for s in report["failing"] if s["failures"] >= ALERT_FAILURES}
    if budget and report["spend"] >= ALERT_SPEND * budget:
        wanted[f"spend:{report['month']}"] = spend_issue(report, run_url)

    actions = []
    for key, (title, body) in wanted.items():
        if key in open_by_key:
            continue
        if not dry_run:
            if not has_label:
                github.create_label(LABEL, LABEL_COLOR, LABEL_DESCRIPTION)
                has_label = True
            github.create_issue(title, body, LABEL)
        actions.append(("open", title))

    for key, issue in open_by_key.items():
        if not key.startswith("source:"):
            continue
        source = conn.execute("SELECT status, consecutive_failures, last_checked_at FROM sources WHERE id = ?",
                              (key.removeprefix("source:"),)).fetchone()
        if source is None or source["status"] != "active" or source["consecutive_failures"] > 0:
            continue
        if not dry_run:
            github.close_issue(issue["number"], f"The source works again: checked successfully at "
                                                f"{source['last_checked_at']}" + (f" ({run_url})." if run_url else "."))
        actions.append(("close", issue["title"]))

    words = {"open": "Would open" if dry_run else "Opened", "close": "Would close" if dry_run else "Closed"}
    for verb, title in actions:
        out(f"{words[verb]}: {title}")
    if not actions:
        out("Health alerts: nothing to open or close.")
    return actions
