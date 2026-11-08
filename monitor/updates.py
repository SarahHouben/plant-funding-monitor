"""Step 13: apply re-extracted calls, close calls that are gone, refresh
deadlines and flag calls without one.

No model calls. Every change to a call is recorded in call_history, and the
whole stage runs in one transaction, so a dry run can roll it back.
"""

import json
from collections import Counter
from datetime import date

from monitor.db import current_run, timestamp, utc_now
from monitor.dedupe import add_flag, call_key, deadline_dates, resolve_flags
from monitor.extraction import STILL_OPEN, call_columns, call_identity, next_deadline

# Fields an update changes on their own.
SUBSTANTIVE = ("title", "title_en", "call_type", "status", "opening_date", "deadlines", "eligible_countries",
               "eligible_institution_types", "eligibility_conditions", "ipk_eligible", "funding_amount",
               "funding_duration", "apply_via", "apply_url", "more_info_links", "contact_point", "relevance_score")
# The model words these a little differently every time, so they are updated
# only along with a substantive field; otherwise every update would fill the
# history with rewording.
WORDING = ("summary_en", "summary_de", "ipk_eligible_reason", "evidence")
# Changes that send a checked call back to the reviewer.
REVIEW_FIELDS = {"deadlines", "status", "eligible_countries", "eligible_institution_types",
                 "eligibility_conditions", "ipk_eligible"}
ENDED = ("closed", "withdrawn")
# A merged duplicate disagrees with its call when these have different
# non-empty values. eligibility_conditions isn't among them: it is the model's
# prose, which two extractions almost always word differently.
CONFLICT_FIELDS = ("deadlines", "status", "eligible_countries", "eligible_institution_types", "ipk_eligible")


def record_change(conn, call_id, field, old, new, run_id, now):
    conn.execute("INSERT INTO call_history (call_id, field, old_value, new_value, run_id, changed_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (call_id, field, None if old is None else str(old), None if new is None else str(new),
                  run_id, now))


def set_status(conn, call, status, run_id, now):
    record_change(conn, call["id"], "status", call["status"], status, run_id, now)
    conn.execute("UPDATE calls SET status = ?, next_deadline = ? WHERE id = ?",
                 (status, None if status in ENDED else call["next_deadline"], call["id"]))


def _stage_name(stage):
    return " ".join((stage.get("stage") or "").lower().split())


def moved_later(old_stages, new_stages):
    """Whether a deadline stage now has a later date: matched by stage name, or
    else by position among the dated stages."""
    old_dated = [stage for stage in old_stages if stage.get("date")]
    by_name = {_stage_name(stage): stage for stage in old_dated}
    for position, stage in enumerate(s for s in new_stages if s.get("date")):
        before = by_name.get(_stage_name(stage)) or (old_dated[position] if position < len(old_dated) else None)
        if before and stage["date"] > before["date"]:
            return True
    return False


def _tags(conn, call_id):
    return json.dumps(sorted(row[0] for row in conn.execute("SELECT tag_id FROM call_tags WHERE call_id = ?",
                                                            (call_id,))))


def apply_update(conn, call, extracted, run_id, now, today, stats):
    """Bring a call in line with a new extraction of it."""
    new, tag_ids = call_columns(extracted, today)
    old_stages, new_stages = json.loads(call["deadlines"]), json.loads(new["deadlines"])
    if new["status"] in STILL_OPEN and (moved_later(old_stages, new_stages) or call["status"] == "extended"):
        # A deadline moved later; a call stays 'extended' until it ends.
        new["status"] = "extended"
    changed = [field for field in SUBSTANTIVE if new[field] != call[field]]
    old_tags, new_tags = _tags(conn, call["id"]), json.dumps(sorted(tag_ids))
    if changed or old_tags != new_tags:
        changed += [field for field in WORDING if new[field] != call[field]]
    values = {field: new[field] for field in changed}
    for field in changed:
        record_change(conn, call["id"], field, call[field], new[field], run_id, now)
    if old_tags != new_tags:
        record_change(conn, call["id"], "tags", old_tags, new_tags, run_id, now)
        conn.execute("DELETE FROM call_tags WHERE call_id = ?", (call["id"],))
        conn.executemany("INSERT INTO call_tags (call_id, tag_id) VALUES (?, ?)",
                         [(call["id"], tag_id) for tag_id in tag_ids])
    status = values.get("status", call["status"])
    values["next_deadline"] = None if status in ENDED else next_deadline(new_stages, today)
    values["last_verified_at"] = now
    if call["review_status"] == "checked" and REVIEW_FIELDS & set(changed):
        values["review_status"] = "new"
        record_change(conn, call["id"], "review_status", "checked", "new", run_id, now)
    conn.execute(f"UPDATE calls SET {', '.join(f'{field} = ?' for field in values)} WHERE id = ?",
                 (*values.values(), call["id"]))
    if changed or old_tags != new_tags:
        stats["calls updated"] += 1
        stats["fields changed"] += len(changed) + (old_tags != new_tags)
    else:
        stats["calls re-verified, unchanged"] += 1
    if status == "extended" and call["status"] != "extended":
        stats["calls extended"] += 1
    if values.get("review_status") == "new":
        stats["calls sent back for review"] += 1


def _is_empty(field, value):
    return value is None or value in ("", "[]", "{}") or (field == "ipk_eligible" and value == "unclear")


def _comparable(field, value):
    """A value as compared for conflicts: deadlines by their dates, lists as sets."""
    if field == "deadlines":
        return sorted(deadline_dates(json.loads(value)))
    if field in ("eligible_countries", "eligible_institution_types"):
        return sorted(json.loads(value))
    return value


def _short(value, length=120):
    text = str(value)
    return text if len(text) <= length else text[:length] + "…"


def is_listing_page(conn, url, key):
    """Whether a call's URL is a listing page rather than the call's own page:
    the call had no link of its own (its key carries its title), or the URL is
    a page the pipeline watches as a source."""
    return "#" in key or any(call_key(row[0]) == call_key(url) for row in conn.execute("SELECT url FROM sources"))


def apply_merge(conn, call, extracted, run_id, now, today, stats, link=None):
    """Fill a call's empty or 'unclear' fields from a duplicate merged into it.

    Nothing the call already has is overwritten. Where the duplicate gives
    different deadlines, status or eligibility, the call is flagged for review
    instead. A filled IPK eligibility brings its reason, and filled fields
    bring their evidence quotes. A call known only by a listing page takes the
    duplicate's own link (link: its display URL and key), such as the PDF of
    the announcement; the listing page stays among the call's listings.
    """
    new, _ = call_columns(extracted, today)
    conflicts = [field for field in CONFLICT_FIELDS
                 if not _is_empty(field, call[field]) and not _is_empty(field, new[field])
                 and _comparable(field, call[field]) != _comparable(field, new[field])]
    filled = [field for field in SUBSTANTIVE if _is_empty(field, call[field]) and not _is_empty(field, new[field])]
    if "ipk_eligible" in filled:
        filled.append("ipk_eligible_reason")
    values = {field: new[field] for field in filled}
    evidence, merged_evidence = json.loads(call["evidence"]), json.loads(new["evidence"])
    if any(field in merged_evidence for field in filled):
        evidence.update({field: merged_evidence[field] for field in filled if field in merged_evidence})
        values["evidence"] = json.dumps(evidence, ensure_ascii=False)
    if (link and is_listing_page(conn, call["original_url"], call["canonical_url"])
            and not is_listing_page(conn, *link)
            and not conn.execute("SELECT 1 FROM calls WHERE canonical_url = ?", (link[1],)).fetchone()):
        values["original_url"], values["canonical_url"] = link
        stats["calls given the merged duplicate's own link"] += 1
    for field, value in values.items():
        record_change(conn, call["id"], field, call[field], value, run_id, now)
    if "deadlines" in values:
        values["next_deadline"] = (None if call["status"] in ENDED
                                   else next_deadline(json.loads(values["deadlines"]), today))
    values["last_verified_at"] = now
    if call["review_status"] == "checked" and REVIEW_FIELDS & set(filled):
        values["review_status"] = "new"
        record_change(conn, call["id"], "review_status", "checked", "new", run_id, now)
        stats["calls sent back for review"] += 1
    conn.execute(f"UPDATE calls SET {', '.join(f'{field} = ?' for field in values)} WHERE id = ?",
                 (*values.values(), call["id"]))
    if conflicts:
        add_flag(conn, call["id"], "merge_conflict", run_id, now,
                 "; ".join(f"{field}: {_short(call[field])} here, {_short(new[field])} in the merged duplicate"
                           for field in conflicts))
        stats["calls flagged: merged duplicate disagrees"] += 1
    stats["merged duplicates applied"] += 1
    stats["fields filled from merged duplicates"] += len(filled)


def apply_updates(conn, run_id, now, today, stats):
    """Apply every unhandled call update, oldest first."""
    for update in conn.execute("SELECT * FROM call_updates WHERE handled_at IS NULL ORDER BY id").fetchall():
        call = conn.execute("SELECT * FROM calls WHERE id = ?", (update["call_id"],)).fetchone()
        answer = json.loads(update["extracted"])
        if update["kind"] == "merge":
            if not answer.get("not_a_call"):
                candidate = conn.execute("SELECT * FROM candidates WHERE id = ?", (update["candidate_id"],)).fetchone()
                source = conn.execute("SELECT * FROM sources WHERE id = ?", (candidate["source_id"],)).fetchone()
                apply_merge(conn, call, answer["call"], run_id, now, today, stats,
                            call_identity(candidate, source))
        elif answer.get("not_a_call"):
            add_flag(conn, call["id"], "not_a_call", run_id, now, answer.get("reason"))
            stats["calls flagged: page no longer describes a call"] += 1
        else:
            apply_update(conn, call, answer["call"], run_id, now, today, stats)
        conn.execute("UPDATE call_updates SET handled_at = ? WHERE id = ?", (now, update["id"]))


def _all_dated_deadlines_passed(stages, today):
    dated = [date.fromisoformat(stage["date"]) for stage in stages if stage.get("date")]
    annual = any(stage.get("annual_date") for stage in stages)
    return bool(dated) and not annual and all(day < today for day in dated)


def handle_removed(conn, run_id, now, today, stats):
    """Deal with items the fetch step found removed.

    An EU topic leaves the list of open and forthcoming topics when it closes
    (the fetch step reports removals only when the totals add up), so its call
    is closed. A removed page section only means the call is no longer listed
    there: the call is closed if it is listed nowhere else and all its
    deadlines have passed, and flagged for review if it is listed nowhere else
    but hasn't ended. Förderinfo, for example, only shows its newest calls.
    """
    rows = conn.execute("SELECT i.*, s.access_method FROM item_changes i JOIN sources s ON s.id = i.source_id "
                        "WHERE i.change = 'removed' AND i.screened_at IS NULL ORDER BY i.id").fetchall()
    for row in rows:
        listings = conn.execute("SELECT * FROM call_listings WHERE source_id = ? AND item_key = ? "
                                "AND removed_at IS NULL", (row["source_id"], row["item_key"])).fetchall()
        for listing in listings:
            conn.execute("UPDATE call_listings SET removed_at = ? WHERE id = ?", (now, listing["id"]))
            call = conn.execute("SELECT * FROM calls WHERE id = ?", (listing["call_id"],)).fetchone()
            if call["status"] in ENDED:
                continue
            if row["access_method"] == "api":
                set_status(conn, call, "closed", run_id, now)
                stats["calls closed: EU topic no longer open or forthcoming"] += 1
                continue
            remaining = conn.execute("SELECT count(*) FROM call_listings WHERE call_id = ? AND removed_at IS NULL",
                                     (call["id"],)).fetchone()[0]
            if remaining:
                stats["listings removed, call still listed elsewhere"] += 1
            elif _all_dated_deadlines_passed(json.loads(call["deadlines"]), today):
                set_status(conn, call, "closed", run_id, now)
                stats["calls closed: no longer listed, deadlines passed"] += 1
            else:
                add_flag(conn, call["id"], "section_removed", run_id, now,
                         f"No longer listed at {row['source_id']}: {row['item_key']}")
                stats["calls flagged: no longer listed anywhere"] += 1
        conn.execute("UPDATE item_changes SET screened_at = ?, text = NULL WHERE id = ?", (now, row["id"]))
        stats["removed items handled"] += 1


def refresh(conn, run_id, now, today, stats):
    """Recompute next_deadline and status for every call: forthcoming calls
    open on their opening date, open calls close once all their dated deadlines
    have passed, and recurring calls move on to their next annual date."""
    for call in conn.execute("SELECT * FROM calls").fetchall():
        stages = json.loads(call["deadlines"])
        status = call["status"]
        if status == "forthcoming" and call["opening_date"] and call["opening_date"] <= today.isoformat():
            status = "open"
        if status in STILL_OPEN and _all_dated_deadlines_passed(stages, today):
            status = "closed"
        upcoming = None if status in ENDED else next_deadline(stages, today)
        if status != call["status"]:
            record_change(conn, call["id"], "status", call["status"], status, run_id, now)
            stats[f"refresh: {call['status']} became {status}"] += 1
        if upcoming != call["next_deadline"] and status == call["status"]:
            stats["refresh: next deadline moved on or cleared"] += 1
        if (status, upcoming) != (call["status"], call["next_deadline"]):
            conn.execute("UPDATE calls SET status = ?, next_deadline = ? WHERE id = ?",
                         (status, upcoming, call["id"]))


def flag_missing_deadlines(conn, run_id, now, stats):
    """Flag each call that hasn't ended, has no deadline and isn't always open,
    once: a flag the reviewer resolved isn't raised again. The flag is
    resolved once the call has a deadline, is always open, or has ended."""
    calls = conn.execute(
        "SELECT c.id, c.call_type, c.status, c.deadlines, "
        "EXISTS (SELECT 1 FROM call_flags f WHERE f.call_id = c.id AND f.kind = 'deadline_missing') AS flagged "
        "FROM calls c").fetchall()
    for call in calls:
        missing = (call["call_type"] == "deadline_bound" and call["status"] not in ENDED
                   and not json.loads(call["deadlines"]))
        if missing and not call["flagged"]:
            add_flag(conn, call["id"], "deadline_missing", run_id, now,
                     "No deadline is stated, and the call doesn't say applications are possible at any time")
            stats["calls flagged: deadline missing"] += 1
        elif not missing and resolve_flags(conn, call["id"], "deadline_missing", now):
            stats["deadline_missing flags resolved"] += 1


def run_update(conn, clock=utc_now, out=print, dry_run=False):
    """Apply updates, closures, the refresh and the deadline flags in one
    transaction; a dry run rolls it back. Returns the counts of what happened
    (or would have)."""
    now = clock()
    stamp, today = timestamp(now), now.date()
    stats = Counter()
    conn.commit()
    conn.execute("BEGIN")
    try:
        run_id = current_run(conn, now)
        apply_updates(conn, run_id, stamp, today, stats)
        handle_removed(conn, run_id, stamp, today, stats)
        refresh(conn, run_id, stamp, today, stats)
        flag_missing_deadlines(conn, run_id, stamp, stats)
        conn.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (stamp, run_id))
    except BaseException:
        conn.rollback()
        raise
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    heading = "Update (dry run, nothing saved)" if dry_run else "Update"
    out(f"{heading}: " + (", ".join(f"{n} {what}" for what, n in sorted(stats.items())) or "nothing to do"))
    return stats
