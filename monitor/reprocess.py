"""Reprocessing without a full run: screen one source again, or extract chosen calls again.

Each records into a run of its own, and changes calls only the way a normal
run would: a re-screen rejects calls whose items are now not relevant, and a
re-extraction goes through call_updates and the update stage, so every change
lands in call_history. A dry run calls no API and leaves the database as it was.
"""

import json
import sqlite3
import time
from collections import Counter

from monitor.changes import Change, plan_changes, record_changes, stored_items
from monitor.db import timestamp, utc_now
from monitor.dedupe import call_key, deadline_dates
from monitor.extraction import call_identity, extract, known_call
from monitor.extraction import dry_run as extraction_dry_run
from monitor.readers import linked_page_excerpt, read_source
from monitor.screening import estimate as screening_estimate
from monitor.screening import screen
from monitor.updates import record_change, run_update

FLAG_KINDS = ("possible_duplicate", "section_removed", "not_a_call", "merge_conflict", "deadline_missing",
              "thin_source")
REEXTRACT_REASON = "Extraction again, asked for with reprocess.py"


def start_run(conn, now):
    """A run of its own, which the screening, extraction and update steps then record into."""
    return conn.execute("INSERT INTO runs (started_at) VALUES (?)", (timestamp(now),)).lastrowid


def active_source(conn, source_id):
    source = conn.execute("SELECT * FROM sources WHERE id = ? AND status = 'active'", (source_id,)).fetchone()
    if source is None:
        raise SystemExit(f"Not an active source: {source_id}")
    return source


# --- Screening a source again ------------------------------------------------------

def rescreen_source(conn, fetcher, client, source_id, dry_run=False, max_cost=None, clock=utc_now, out=print):
    """Read a source again and screen every item: changed items as changed, all
    others as new. Calls whose result is now not_relevant are rejected, with a
    history entry. Calls found again wait for extraction as usual: known ones
    only become listings. Returns the rejected calls' ids."""
    source = active_source(conn, source_id)
    snapshot = read_source(fetcher, source)
    stored = stored_items(conn, source_id)
    real = plan_changes(stored, snapshot)
    changed_keys = {change.item_key for change in real}
    as_new = [Change("new", item.key, item.url, item.title, item.text, item)
              for item in snapshot.items if item.key not in changed_keys]
    to_screen = [change for change in real if change.kind != "removed"] + as_new
    removed = sum(change.kind == "removed" for change in real)
    listed = conn.execute("SELECT count(DISTINCT call_id) FROM call_listings WHERE source_id = ? "
                          "AND removed_at IS NULL", (source_id,)).fetchone()[0]

    if dry_run:
        out(f"Dry run: {source_id} has {len(snapshot.items)} items ({len(to_screen) - len(as_new)} new or changed, "
            f"{removed} removed); all would be screened again, and calls now found not relevant rejected. "
            f"{listed} calls are listed at this source.")
        if any(change.item.needs_excerpt for change in to_screen):
            out("Feed entries without a description would also get an excerpt of their page, which a dry run "
                "doesn't fetch, so the estimate is low.")
        items = [{"id": n, "source_id": source_id, "change": change.kind, "url": change.url, "title": change.title,
                  "text": change.text} for n, change in enumerate(to_screen, 1)]
        screening_estimate(items, {source_id: source}, clock().date().isoformat(), out)
        return []

    now = clock()
    stamp = timestamp(now)
    problems = []
    for change in to_screen:
        if change.item.needs_excerpt:
            try:
                page = linked_page_excerpt(fetcher, change.url, source["content_selector"])
            except Exception as e:
                problems.append({"source_id": source_id, "url": change.url, "message": f"no excerpt: {e}"})
                continue
            change.item.text = change.text = f"{change.item.title}\n\n{page}"
    with conn:
        run_id = start_run(conn, now)
        record_changes(conn, run_id, source_id, snapshot, real, stored, stamp)
        for change in as_new:
            conn.execute("INSERT INTO item_changes (run_id, source_id, item_key, url, title, change, text, "
                         "detected_at) VALUES (?, ?, ?, ?, ?, 'new', ?, ?)",
                         (run_id, source_id, change.item_key, change.url, change.title, change.text, stamp))
        conn.execute("UPDATE runs SET sources_checked = 1, pages_changed = ?, errors = ?, error_details = ? "
                     "WHERE id = ?", (int(bool(real)), len(problems), json.dumps(problems), run_id))
    out(f"Read {source_id} again: {len(snapshot.items)} items, all screened again "
        f"({len(to_screen) - len(as_new)} new or changed); {removed} removed, left for the next update stage.")

    screen(conn, client, clock=clock, out=out, max_cost=max_cost, source_id=source_id)
    with conn:
        rejected = reject_not_relevant(conn, run_id, source, timestamp(clock()))
    for call in rejected:
        out(f"  rejected #{call['id']} {call['title']}: now not relevant")
    waiting = conn.execute("SELECT * FROM candidates WHERE run_id = ? AND source_id = ? AND extracted_at IS NULL",
                           (run_id, source_id)).fetchall()
    known = sum(known_call(conn, candidate, source) is not None for candidate in waiting)
    out(f"Rejected {len(rejected)} calls. {len(waiting)} calls wait for extraction: {known} known ones, which "
        f"only become listings, and {len(waiting) - known} new ones.")
    return [call["id"] for call in rejected]


def _calls_matching(conn, source, listings, url, title):
    """The calls a screening result (url, title) of the source stands for: by
    canonical key, or by a listing of the source under the same URL."""
    display, key = call_identity({"url": url, "title": title}, source)
    ids = {row[0] for row in conn.execute("SELECT id FROM calls WHERE canonical_url = ?", (key,))}
    ids |= {row["call_id"] for row in listings
            if row["url"] == display or ("#" not in row["url"] and call_key(row["url"]) == key)}
    return ids


def reject_not_relevant(conn, run_id, source, now):
    """Reject the calls this run's screening found not relevant at the source,
    unless the same screening also found them as calls. Returns the rejected
    calls. Call inside a transaction."""
    listings = conn.execute("SELECT call_id, url FROM call_listings WHERE source_id = ?", (source["id"],)).fetchall()
    dropped, kept = set(), set()
    for row in conn.execute("SELECT url, title FROM screening_log WHERE run_id = ? AND source_id = ? "
                            "AND stage = 'screening' AND category = 'not_relevant'", (run_id, source["id"])):
        dropped |= _calls_matching(conn, source, listings, row["url"], row["title"])
    for row in conn.execute("SELECT url, title FROM candidates WHERE run_id = ? AND source_id = ?",
                            (run_id, source["id"])):
        kept |= _calls_matching(conn, source, listings, row["url"], row["title"])
    rejected = []
    for call_id in sorted(dropped - kept):
        call = conn.execute("SELECT id, title, review_status FROM calls WHERE id = ?", (call_id,)).fetchone()
        if call["review_status"] == "rejected":
            continue
        record_change(conn, call_id, "review_status", call["review_status"], "rejected", run_id, now)
        conn.execute("UPDATE calls SET review_status = 'rejected' WHERE id = ?", (call_id,))
        rejected.append(call)
    return rejected


# --- Extracting calls again ----------------------------------------------------------

def select_calls(conn, call_ids=(), flag=None, closed_undated=False):
    """The calls to extract again: those named, those with an open flag of the
    kind, and closed calls without any dated stage. Rejected calls are left
    out unless named."""
    ids = set(call_ids)
    missing = ids - {row[0] for row in conn.execute(
        f"SELECT id FROM calls WHERE id IN ({', '.join('?' * len(ids))})", tuple(ids))}
    if missing:
        raise SystemExit(f"No such call: {', '.join(map(str, sorted(missing)))}")
    if flag:
        ids |= {row[0] for row in conn.execute(
            "SELECT f.call_id FROM call_flags f JOIN calls c ON c.id = f.call_id WHERE f.kind = ? "
            "AND f.resolved_at IS NULL AND c.review_status <> 'rejected'", (flag,))}
    if closed_undated:
        ids |= {row["id"] for row in conn.execute("SELECT id, deadlines FROM calls WHERE status = 'closed' "
                                                 "AND review_status <> 'rejected'")
                if not deadline_dates(json.loads(row["deadlines"]))}
    return conn.execute(f"SELECT * FROM calls WHERE id IN ({', '.join('?' * len(ids))}) ORDER BY id",
                        tuple(ids)).fetchall()


def reextraction_link(call):
    """The URL and title a re-extraction candidate needs for the call's own
    canonical key: a call without a link of its own is known by its listing's
    URL and the title it was listed under (the fragment of its original URL)."""
    if "#" in call["canonical_url"]:
        url, _, title = call["original_url"].partition("#")
        return url, title
    return call["original_url"], call["title"]


def _listing_texts(conn, fetcher, calls, out):
    """For each call, a listing item (source, item key) and the item's current
    text, reading each source involved once. A call's own source comes first;
    a call listed nowhere gets its own source and its key."""
    found, snapshots = {}, {}
    for call in calls:
        listings = conn.execute("SELECT source_id, item_key FROM call_listings WHERE call_id = ? "
                                "AND removed_at IS NULL ORDER BY source_id <> ?, id",
                                (call["id"], call["source_id"])).fetchall()
        if not listings:
            found[call["id"]] = (call["source_id"], call["canonical_url"], "")
            continue
        source_id, item_key = listings[0]["source_id"], listings[0]["item_key"]
        if source_id not in snapshots:
            try:
                snapshot = read_source(fetcher, conn.execute("SELECT * FROM sources WHERE id = ?",
                                                             (source_id,)).fetchone())
                snapshots[source_id] = {item.key: item.text for item in snapshot.items}
            except Exception as e:
                out(f"  {source_id}: couldn't be read ({e}); its calls are extracted without the listing text")
                snapshots[source_id] = {}
        found[call["id"]] = (source_id, item_key, snapshots[source_id].get(item_key, ""))
    return found


def reextract(conn, fetcher, client, call_ids=(), flag=None, closed_undated=False, dry_run=False, max_cost=None,
              batch=False, clock=utc_now, sleep=time.sleep, out=print):
    """Extract the chosen calls again, as if their items had changed, and apply
    the results through call_updates and the update stage. Requests that
    weren't extracted (over --max-cost, or failed) are removed again, so the
    daily run doesn't pick them up. Returns the update stage's counts."""
    calls = select_calls(conn, call_ids, flag, closed_undated)
    if not calls:
        out("No calls match.")
        return Counter()
    sources = {row["id"]: row for row in conn.execute("SELECT * FROM sources")}
    texts = _listing_texts(conn, fetcher, calls, out)
    now = clock()
    stamp = timestamp(now)
    conn.commit()
    run_id = start_run(conn, now)
    candidate_ids = []
    for call in calls:
        source_id, item_key, text = texts[call["id"]]
        url, title = reextraction_link(call)
        if call_identity({"url": url, "title": title}, sources[source_id])[1] != call["canonical_url"]:
            out(f"  #{call['id']}: skipped, its URL doesn't lead back to the call")
            continue
        if "#" in call["canonical_url"] and not text:
            out(f"  #{call['id']}: skipped, it has no link of its own and its listing text is gone")
            continue
        change_id = conn.execute(
            "INSERT INTO item_changes (run_id, source_id, item_key, url, title, change, detected_at, screened_at) "
            "VALUES (?, ?, ?, ?, ?, 'changed', ?, ?)", (run_id, source_id, item_key, url, title, stamp, stamp)).lastrowid
        try:
            candidate_ids.append(conn.execute(
                "INSERT INTO candidates (run_id, item_change_id, source_id, url, title, reason, text, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, change_id, source_id, url, title, REEXTRACT_REASON, text, stamp)).lastrowid)
        except sqlite3.IntegrityError:
            conn.execute("DELETE FROM item_changes WHERE id = ?", (change_id,))
            out(f"  #{call['id']}: skipped, it is already waiting for extraction")
    out(f"{'Dry run: would extract' if dry_run else 'Extracting'} {len(candidate_ids)} of {len(calls)} chosen "
        f"calls again: " + ", ".join(f"#{call['id']}" for call in calls))

    if dry_run:
        try:
            if candidate_ids:
                extraction_dry_run(conn, fetcher, ids=candidate_ids, clock=clock, out=out)
        finally:
            conn.rollback()
        return Counter()
    conn.commit()
    if not candidate_ids:
        return Counter()
    try:
        extract(conn, client, fetcher, ids=candidate_ids, batch=batch, max_cost=max_cost, clock=clock, sleep=sleep,
                out=out)
    finally:
        with conn:
            left = [row[0] for row in conn.execute(
                f"SELECT id FROM candidates WHERE id IN ({', '.join('?' * len(candidate_ids))}) "
                "AND extracted_at IS NULL", candidate_ids)]
            for candidate_id in left:
                change_id = conn.execute("SELECT item_change_id FROM candidates WHERE id = ?",
                                         (candidate_id,)).fetchone()[0]
                conn.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))
                conn.execute("DELETE FROM item_changes WHERE id = ?", (change_id,))
        if left:
            out(f"{len(left)} calls weren't extracted again (over the cost limit, or failed); run again for them.")
    return run_update(conn, clock=clock, out=out)
