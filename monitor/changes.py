"""Compare a fresh snapshot of a source with what was stored, and record the differences."""

from dataclasses import dataclass

from monitor.clean import fingerprint, section_diff


@dataclass
class Change:
    kind: str      # new, changed or removed
    item_key: str
    url: str
    title: str | None
    text: str
    item: object = None   # the snapshot Item; None for removed items


def stored_items(conn, source_id):
    rows = conn.execute("SELECT * FROM source_items WHERE source_id = ?", (source_id,))
    return {row["item_key"]: row for row in rows}


def plan_changes(stored, snapshot, sections):
    """List the new, changed and removed items. `sections` says whether items
    are page sections, whose changes are shown as added and removed lines."""
    changes = []
    for item in snapshot.items:
        old = stored.get(item.key)
        if old is None or old["removed_at"]:
            changes.append(Change("new", item.key, item.url, item.title, item.text, item))
        elif old["fingerprint"] != item.fingerprint:
            if item.needs_excerpt:
                # Keep the excerpt fetched when the entry was new.
                _, _, kept = old["text"].partition("\n\n")
                item.text = f"{item.title}\n\n{kept}" if kept else item.title
            text = section_diff(old["text"], item.text) if sections else item.text
            changes.append(Change("changed", item.key, item.url, item.title, text, item))
    if snapshot.tracks_removals and snapshot.complete:
        current = {item.key for item in snapshot.items}
        for key, old in stored.items():
            if key not in current and not old["removed_at"]:
                changes.append(Change("removed", key, old["url"], old["title"], old["text"]))
    return changes


def record_changes(conn, run_id, source_id, snapshot, changes, stored, now):
    """Store the snapshot, the changes and the source's state. Call inside a transaction."""
    changed_keys = {c.item_key for c in changes}
    for item in snapshot.items:
        if item.key in stored and item.key not in changed_keys:
            conn.execute("UPDATE source_items SET last_seen_at = ? WHERE source_id = ? AND item_key = ?",
                         (now, source_id, item.key))
    for change in changes:
        if change.kind == "removed":
            conn.execute("UPDATE source_items SET removed_at = ?, last_changed_at = ? "
                         "WHERE source_id = ? AND item_key = ?",
                         (now, now, source_id, change.item_key))
        else:
            item = change.item
            conn.execute(
                "INSERT INTO source_items (source_id, item_key, url, title, fingerprint, text, "
                "first_seen_at, last_seen_at, last_changed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source_id, item_key) DO UPDATE SET url = excluded.url, "
                "title = excluded.title, fingerprint = excluded.fingerprint, text = excluded.text, "
                "last_seen_at = excluded.last_seen_at, last_changed_at = excluded.last_changed_at, "
                "removed_at = NULL",
                (source_id, item.key, item.url, item.title, item.fingerprint, item.text, now, now, now))
        conn.execute("INSERT INTO item_changes (run_id, source_id, item_key, url, title, change, text, "
                     "detected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (run_id, source_id, change.item_key, change.url, change.title, change.kind,
                      change.text, now))
    content_hash = fingerprint(*sorted(f"{i.key}={i.fingerprint}" for i in snapshot.items))
    conn.execute("UPDATE sources SET content_hash = ?, last_checked_at = ?, "
                 "last_changed_at = CASE WHEN ? THEN ? ELSE last_changed_at END, "
                 "last_error = NULL, consecutive_failures = 0 WHERE id = ?",
                 (content_hash, now, bool(changes), now, source_id))


def record_failure(conn, source_id, message):
    with conn:
        conn.execute("UPDATE sources SET last_error = ?, consecutive_failures = consecutive_failures + 1 "
                     "WHERE id = ?", (message, source_id))
