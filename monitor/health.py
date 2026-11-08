"""The health report: failing, silent and shrinking sources, open review flags
and the month's API spend.

Read-only. The pipeline prints it at the end of every run, and alerts.py turns
the problems that need a person into GitHub issues.
"""

from collections import Counter

from monitor.db import parse_timestamp, utc_now
from monitor.extraction import DEFAULT_MODEL, PRICES
from monitor.screening import cost as screening_cost

# Days without a new or changed item after which a source counts as silent.
SILENT_DAYS = {"daily": 30, "weekly": 60, "monthly": 120}
# Failed checks in a row after which a source gets a GitHub issue.
ALERT_FAILURES = 3
# Share of the monthly budget after which the spend gets a GitHub issue.
ALERT_SPEND = 0.8


def failing_sources(conn, run_id=None):
    """Active sources whose last check failed, with their failures in a row and
    whether they failed in this run."""
    failed_now = set()
    if run_id is not None:
        failed_now = {row[0] for row in conn.execute(
            "SELECT source_id FROM source_checks WHERE run_id = ? AND error IS NOT NULL", (run_id,))}
    return [{"id": row["id"], "funder": row["funder"], "url": row["url"], "failures": row["consecutive_failures"],
             "error": row["last_error"], "last_checked_at": row["last_checked_at"], "this_run": row["id"] in failed_now}
            for row in conn.execute("SELECT * FROM sources WHERE status = 'active' AND consecutive_failures > 0 "
                                    "ORDER BY consecutive_failures DESC, id")]


def silent_sources(conn, now):
    """Active sources without a new or changed item for longer than SILENT_DAYS
    for their check frequency. A source that never changed counts from its
    first recorded check."""
    silent = []
    for row in conn.execute("SELECT s.id, s.check_frequency, COALESCE(s.last_changed_at, "
                            "(SELECT min(checked_at) FROM source_checks k WHERE k.source_id = s.id)) AS since "
                            "FROM sources s WHERE s.status = 'active' ORDER BY s.id"):
        if row["since"] is None:
            continue
        days = (now - parse_timestamp(row["since"])).days
        if days > SILENT_DAYS[row["check_frequency"]]:
            silent.append({"id": row["id"], "days": days, "frequency": row["check_frequency"]})
    return silent


def item_drops(conn):
    """Active sources whose latest successful check found no items, or fewer than
    half as many as the successful check before it: likely a redesigned page."""
    drops = []
    for (source_id,) in conn.execute("SELECT id FROM sources WHERE status = 'active' ORDER BY id"):
        checks = conn.execute("SELECT item_count, complete, checked_at FROM source_checks WHERE source_id = ? "
                              "AND item_count IS NOT NULL ORDER BY id DESC LIMIT 2", (source_id,)).fetchall()
        if len(checks) < 2:
            continue
        latest, previous = checks
        if previous["item_count"] and (latest["item_count"] == 0 or latest["item_count"] < previous["item_count"] / 2):
            drops.append({"id": source_id, "before": previous["item_count"], "after": latest["item_count"],
                          "checked_at": latest["checked_at"], "complete": bool(latest["complete"])})
    return drops


def open_flags(conn):
    """Open review flags on calls that aren't rejected, counted by kind."""
    return dict(sorted(Counter(row[0] for row in conn.execute(
        "SELECT f.kind FROM call_flags f JOIN calls c ON c.id = f.call_id "
        "WHERE f.resolved_at IS NULL AND c.review_status <> 'rejected'")).items()))


def month_spend(conn, now):
    """API spend of the runs started this calendar month (UTC), worked out from
    their tokens at standard prices: exact for the standard API, an upper bound
    for runs that used Message Batches (half price)."""
    price_in, price_out, price_write, price_read = PRICES[DEFAULT_MODEL]
    dollars = 0.0
    for run in conn.execute("SELECT * FROM runs WHERE started_at >= ?", (now.strftime("%Y-%m-01T00:00:00Z"),)):
        dollars += screening_cost(run["screening_input_tokens"], run["screening_output_tokens"],
                                  run["screening_cache_read_tokens"], run["screening_cache_write_tokens"])
        dollars += (run["extraction_input_tokens"] * price_in + run["extraction_output_tokens"] * price_out
                    + run["extraction_cache_write_tokens"] * price_write
                    + run["extraction_cache_read_tokens"] * price_read) / 1_000_000
    return dollars


def health_report(conn, run_id=None, budget=None, clock=utc_now):
    """Everything the health section shows, as a dict."""
    now = clock()
    return {"failing": failing_sources(conn, run_id), "silent": silent_sources(conn, now), "drops": item_drops(conn),
            "flags": open_flags(conn), "spend": month_spend(conn, now), "budget": budget,
            "month": now.strftime("%Y-%m")}


def format_health(report):
    """The health section as lines for the pipeline summary."""
    lines = ["Health"]
    if report["failing"]:
        lines.append(f"  Failing sources:  {len(report['failing'])}")
        lines += [f"    {s['id']}: {s['failures']} check{'s' if s['failures'] > 1 else ''} in a row"
                  f"{' (this run)' if s['this_run'] else ''}: {(s['error'] or '')[:150]}" for s in report["failing"]]
    else:
        lines.append("  Failing sources:  none")
    thresholds = (f"no change for more than {'/'.join(map(str, SILENT_DAYS.values()))} days "
                  f"for {'/'.join(SILENT_DAYS)} sources")
    if report["silent"]:
        lines.append(f"  Silent sources:   {len(report['silent'])} ({thresholds})")
        lines += [f"    {s['id']}: no new or changed item for {s['days']} days" for s in report["silent"]]
    else:
        lines.append(f"  Silent sources:   none ({thresholds})")
    if report["drops"]:
        lines.append(f"  Item drops:       {len(report['drops'])}")
        lines += [f"    {d['id']}: {d['before']} → {d['after']} items at its last check ({d['checked_at'][:10]})"
                  + ("" if d["complete"] else ", listing cut off") for d in report["drops"]]
    else:
        lines.append("  Item drops:       none")
    flags = ", ".join(f"{kind} {n}" for kind, n in report["flags"].items())
    lines.append(f"  Open flags:       {flags or 'none'}")
    spend = f"${report['spend']:.2f}"
    if report["budget"]:
        spend += f" of ${report['budget']:.2f} ({report['spend'] / report['budget']:.0%})"
    lines.append(f"  API spend:        {spend} in {report['month']}, at standard prices")
    return lines
