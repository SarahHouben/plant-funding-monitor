"""Tests for the health report (monitor/health.py) and the health alerts
(monitor/alerts.py, alerts.py), with fake data and a fake GitHub: no network,
no spending.

Run from the repo root, after installing requirements.txt:
    .venv/bin/python tests/test_health.py
"""

import json
import sys
import tempfile
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from monitor.alerts import LABEL, GhCli, sync_alerts  # noqa: E402
from monitor.db import timestamp  # noqa: E402
from monitor.health import format_health, health_report  # noqa: E402
from test_update import NOW, check, insert_call, make_db  # noqa: E402

SOURCES = [(name, f"https://{name}.test/calls", "html", "") for name in
           ("broken", "flaky", "paused", "quiet", "fresh", "weekly", "monthly", "never", "unread", "shrunk", "emptied",
            "dipped", "grew", "single")]


def ago(days):
    return timestamp(NOW - timedelta(days=days))


class FakeGitHub:
    """Labels and issues in memory; every change is logged in calls."""

    def __init__(self, labels=()):
        self.label_names, self.issues, self.calls = list(labels), [], []

    def labels(self):
        return list(self.label_names)

    def create_label(self, name, color, description):
        self.calls.append(("label", name))
        self.label_names.append(name)

    def open_issues(self, label):
        return [issue for issue in self.issues if issue["state"] == "open" and label in issue["labels"]]

    def create_issue(self, title, body, label):
        self.calls.append(("create", title))
        self.issues.append({"number": len(self.issues) + 1, "title": title, "body": body, "labels": [label],
                            "state": "open", "comments": []})

    def close_issue(self, number, comment):
        self.calls.append(("close", number))
        issue = self.issues[number - 1]
        issue["state"] = "closed"
        issue["comments"].append(comment)


def setup(conn):
    """Sources in every state the report looks at."""
    with conn:
        run = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (timestamp(NOW),)).lastrowid
        conn.execute("UPDATE sources SET last_checked_at = ?, last_changed_at = ?", (ago(1), ago(1)))
        conn.execute("UPDATE sources SET consecutive_failures = 3, last_error = 'HTTP 404 for https://broken.test/calls' "
                     "WHERE id = 'broken'")
        conn.execute("UPDATE sources SET consecutive_failures = 1, last_error = 'Timeout' WHERE id = 'flaky'")
        conn.execute("UPDATE sources SET status = 'paused', consecutive_failures = 5, last_error = 'x' WHERE id = 'paused'")
        conn.execute("UPDATE sources SET last_changed_at = ? WHERE id = 'quiet'", (ago(31),))
        conn.execute("UPDATE sources SET last_changed_at = ? WHERE id = 'fresh'", (ago(29),))
        conn.execute("UPDATE sources SET check_frequency = 'weekly', last_changed_at = ? WHERE id = 'weekly'", (ago(61),))
        conn.execute("UPDATE sources SET check_frequency = 'monthly', last_changed_at = ? WHERE id = 'monthly'",
                     (ago(100),))
        conn.execute("UPDATE sources SET last_changed_at = NULL WHERE id IN ('never', 'unread')")
        conn.execute("UPDATE sources SET last_checked_at = NULL WHERE id = 'unread'")
        checks = [("never", 40, 5, None), ("shrunk", 8, 32, None), ("shrunk", 1, 12, None), ("emptied", 8, 10, None),
                  ("emptied", 1, 0, None), ("dipped", 8, 10, None), ("dipped", 1, 6, None), ("grew", 8, 5, None),
                  ("grew", 1, 9, None), ("single", 1, 3, None), ("flaky", 8, 20, None), ("flaky", 4, 4, None),
                  ("flaky", 0, None, "Timeout"), ("broken", 0, None, "HTTP 404 for https://broken.test/calls")]
        for source_id, days, items, error in checks:
            conn.execute("INSERT INTO source_checks (run_id, source_id, checked_at, item_count, complete, error) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (run, source_id, ago(days), items,
                                                       None if items is None else 1, error))
        open_call = insert_call(conn, "fresh", "https://fresh.test/a", [])
        rejected = insert_call(conn, "fresh", "https://fresh.test/b", [])
        conn.execute("UPDATE calls SET review_status = 'rejected' WHERE id = ?", (rejected,))
        for call_id, kind, resolved in [(open_call, "deadline_missing", None), (open_call, "thin_source", None),
                                        (rejected, "thin_source", None), (open_call, "not_a_call", ago(1))]:
            conn.execute("INSERT INTO call_flags (call_id, kind, run_id, created_at, resolved_at) VALUES (?, ?, ?, ?, ?)",
                         (call_id, kind, run, ago(2), resolved))
    return run


def add_spend(conn, started_at, input_tokens, output_tokens=0):
    """A run with extraction tokens costing input_tokens * $2 + output_tokens * $10 per million."""
    with conn:
        conn.execute("INSERT INTO runs (started_at, extraction_input_tokens, extraction_output_tokens) VALUES (?, ?, ?)",
                     (started_at, input_tokens, output_tokens))


def test_report():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp), SOURCES)) as conn:
        run = setup(conn)
        report = health_report(conn, run_id=run, budget=10, clock=lambda: NOW)
        failing = {s["id"]: s for s in report["failing"]}
        check("failing sources: active ones with failures in a row, most first; paused ones are left out",
              list(failing) == ["broken", "flaky"] and failing["broken"]["failures"] == 3
              and "HTTP 404" in failing["broken"]["error"] and failing["flaky"]["this_run"])
        check("silent sources: past 30 days for daily, 60 for weekly, 120 for monthly; a source that never changed "
              "counts from its first check; one never read is left out",
              {s["id"] for s in report["silent"]} == {"quiet", "weekly", "never"})
        drops = {d["id"]: d for d in report["drops"]}
        check("item drops: to zero, or to less than half of the last successful check",
              set(drops) == {"shrunk", "emptied", "flaky"} and (drops["shrunk"]["before"], drops["shrunk"]["after"]) == (32, 12)
              and drops["emptied"]["after"] == 0)
        check("a small dip, a rise or a single check is no drop, and a failed check doesn't count as one",
              not {"dipped", "grew", "single", "broken"} & set(drops) and drops["flaky"]["before"] == 20)
        check("open flags are counted by kind, without resolved ones or flags on rejected calls",
              report["flags"] == {"deadline_missing": 1, "thin_source": 1})

        add_spend(conn, ago(3), 2_000_000)                     # $4.00 this month
        add_spend(conn, "2026-10-31T23:00:00Z", 5_000_000)     # last month: not counted
        report = health_report(conn, run_id=run, budget=10, clock=lambda: NOW)
        check("the month's spend counts only runs started this month, at standard prices",
              abs(report["spend"] - 4.0) < 1e-9 and report["month"] == "2026-11")
        lines = format_health(report)
        text = "\n".join(lines)
        check("the health section lists all of it, ending with the spend against the budget",
              lines[0] == "Health" and "broken: 3 checks in a row (this run): HTTP 404" in text
              and "flaky: 1 check in a row (this run): Timeout" in text
              and "no change for more than 30/60/120 days for daily/weekly/monthly sources" in text
              and "quiet: no new or changed item for 31 days" in text and "shrunk: 32 → 12 items" in text
              and "deadline_missing 1, thin_source 1" in text
              and lines[-1] == "  API spend:        $4.00 of $10.00 (40%) in 2026-11, at standard prices")


def test_alerts():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp), SOURCES)) as conn:
        setup(conn)
        github = FakeGitHub()
        lines = []
        sync_alerts(conn, github, budget=10, run_url="https://github.test/run/1", clock=lambda: NOW, out=lines.append)
        check("a source that failed 3 checks in a row gets an issue, after the label is created; 1 failure doesn't",
              github.calls == [("label", LABEL), ("create", "Source failing: broken")]
              and "https://broken.test/calls" in github.issues[0]["body"] and "https://github.test/run/1" in github.issues[0]["body"]
              and lines == ["Opened: Source failing: broken"])

        sync_alerts(conn, github, budget=10, clock=lambda: NOW, out=lines.append)
        check("the next run opens no duplicate while the issue is open", len(github.calls) == 2)

        with conn:
            conn.execute("UPDATE sources SET consecutive_failures = 0, last_error = NULL WHERE id = 'broken'")
        sync_alerts(conn, github, budget=10, clock=lambda: NOW, out=lines.append)
        check("a source that works again gets its issue closed, with a comment",
              github.calls[-1] == ("close", 1) and github.issues[0]["state"] == "closed"
              and "works again" in github.issues[0]["comments"][0])

        with conn:
            conn.execute("UPDATE sources SET consecutive_failures = 4, last_error = 'HTTP 500' WHERE id = 'broken'")
        sync_alerts(conn, github, budget=10, clock=lambda: NOW, out=lines.append)
        check("if it fails 3 times again later, a new issue opens", github.calls[-1] == ("create", "Source failing: broken")
              and len(github.issues) == 2 and ("label", LABEL) not in github.calls[1:])

        add_spend(conn, ago(2), 3_900_000)                     # $7.80: under 80%
        sync_alerts(conn, github, budget=10, clock=lambda: NOW, out=lines.append)
        check("no spend issue under 80% of the budget", not any("API spend" in i["title"] for i in github.issues))
        add_spend(conn, ago(1), 100_000)                       # $8.00 in all: 80%
        sync_alerts(conn, github, budget=10, clock=lambda: NOW, out=lines.append)
        sync_alerts(conn, github, budget=10, clock=lambda: NOW, out=lines.append)
        spend = [i for i in github.issues if "API spend" in i["title"]]
        check("at 80% one spend issue opens, named after the month, and isn't opened twice",
              len(spend) == 1 and spend[0]["title"] == "API spend passed 80% of the monthly budget (2026-11)"
              and "$8.00" in spend[0]["body"])
        check("without a budget there is no spend alert",
              not [a for a in sync_alerts(conn, FakeGitHub([LABEL]), clock=lambda: NOW, out=lambda line: None)
                   if "API spend" in a[1]])

        before = list(github.calls)
        with conn:
            conn.execute("UPDATE sources SET consecutive_failures = 3, last_error = 'x' WHERE id = 'flaky'")
        actions = sync_alerts(conn, github, budget=10, dry_run=True, clock=lambda: NOW, out=lines.append)
        check("a dry run reports what it would do and changes nothing",
              actions == [("open", "Source failing: flaky")] and github.calls == before
              and lines[-1] == "Would open: Source failing: flaky")


def test_gh_commands():
    commands = []

    def fake_run(args, **options):
        commands.append(args)
        output = {"list": "[]"}.get(args[2], "")
        return SimpleNamespace(stdout=output if args[1:3] != ["label", "list"] else json.dumps([{"name": LABEL}]))

    github = GhCli(run=fake_run)
    names = github.labels()
    github.create_label(LABEL, "d93f0b", "Raised by the daily job's health check")
    github.open_issues(LABEL)
    github.create_issue("Title", "Body", LABEL)
    github.close_issue(7, "Fixed.")
    check("the gh commands: list labels, create the label, list open issues, create and close an issue",
          names == [LABEL] and commands == [
              ["gh", "label", "list", "--limit", "500", "--json", "name"],
              ["gh", "label", "create", LABEL, "--color", "d93f0b", "--description",
               "Raised by the daily job's health check"],
              ["gh", "issue", "list", "--label", LABEL, "--state", "open", "--limit", "500", "--json", "number,title,body"],
              ["gh", "issue", "create", "--title", "Title", "--body", "Body", "--label", LABEL],
              ["gh", "issue", "close", "7", "--comment", "Fixed."]])


def main():
    test_report()
    test_alerts()
    test_gh_commands()
    print("All checks passed.")


if __name__ == "__main__":
    main()
