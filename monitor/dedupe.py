"""Recognising the same call: by canonical URL, or by title and deadline.

No model calls: titles are normalised and compared as text. A call is merged
into another only when it is clearly the same (same funder family, nearly
identical title, a shared deadline); a weaker match is stored and flagged for
the reviewer.
"""

import json
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urldefrag

from monitor.urls import canonical_url

# Character similarity of normalised titles above which, with a shared
# deadline, two calls are the same.
SAME_TITLE = 0.9
# Similarity above which two calls of a family may be the same; with a shared
# deadline, the lower one is enough.
POSSIBLE_TITLE = 0.75
POSSIBLE_WITH_DEADLINE = 0.5
# Word overlap only counts for titles with at least this many distinctive words,
# since short titles such as "Research Grants" overlap easily.
MIN_WORDS_FOR_OVERLAP = 3

# Wording that announcements add around a call's real title.
BOILERPLATE = re.compile(
    r"\b(bekanntmachung(en)?( des \w+)?|richtlinie(n)?( nr \w+)?|über die förderung( von| der| des)?|"
    r"zur förderung( von| der| des)?|förderung( von| der| des)?|förderaufruf|call for proposals|"
    r"programm zur innovationsförderung|programm zu innovationsförderung|beispiel|example)\b")
# A trailing clause naming the programme a call belongs to ("... im Rahmen des
# Programms zur Innovationsförderung", "... under the Innovation Funding
# Programme"): context, not part of the call's own title.
PROGRAMME_CLAUSE = re.compile(r"\b(im rahmen (des|der|von)\b.*|(under|within) the\b.*\bprogramme)$")
STOPWORDS = {"und", "der", "die", "das", "für", "von", "zur", "zum", "im", "in", "des", "den", "dem", "mit",
             "auf", "an", "aus", "the", "of", "and", "for", "on", "to", "a", "an", "with"}


def normalise_title(title):
    """Lower case, without punctuation, quotes, announcement boilerplate or a
    trailing programme clause."""
    text = unicodedata.normalize("NFKC", title or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = " ".join(text.split())
    text = PROGRAMME_CLAUSE.sub("", text)
    text = BOILERPLATE.sub(" ", text)
    return " ".join(text.split())


def call_key(url, title=None):
    """The canonical URL that identifies a call. A call without a link of its
    own (title given) is identified by its listing and its normalised title."""
    key = canonical_url(urldefrag(url)[0])
    return f"{key}#{normalise_title(title)}" if title is not None else key


def title_similarity(a, b):
    """(character similarity, best of character similarity and word overlap) of two titles."""
    na, nb = normalise_title(a), normalise_title(b)
    if not na or not nb:
        return 0.0, 0.0
    ratio = SequenceMatcher(None, na, nb, autojunk=False).ratio()
    words_a = {w for w in na.split() if w not in STOPWORDS}
    words_b = {w for w in nb.split() if w not in STOPWORDS}
    overlap = 0.0
    if min(len(words_a), len(words_b)) >= MIN_WORDS_FOR_OVERLAP:
        overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
    return ratio, max(ratio, overlap)


def deadline_dates(stages):
    """The dates and annual dates of a call's deadline stages."""
    return {stage.get("date") or f"annual {stage.get('annual_date')}" for stage in stages
            if stage.get("date") or stage.get("annual_date")}


def find_duplicate(conn, family, title, title_en, stages):
    """The existing call of this funder family that looks most like a new one.

    Returns (kind, call row, detail) with kind 'same' or 'possible', or None.
    """
    best = None
    rows = conn.execute("SELECT c.* FROM calls c JOIN sources s ON s.id = c.source_id "
                        "WHERE COALESCE(s.family, s.id) = ?", (family,)).fetchall()
    for row in rows:
        ratio, score = title_similarity(title, row["title"])
        if title_en and row["title_en"]:
            ratio_en, score_en = title_similarity(title_en, row["title_en"])
            ratio, score = max(ratio, ratio_en), max(score, score_en)
        shared = deadline_dates(stages) & deadline_dates(json.loads(row["deadlines"]))
        if ratio >= SAME_TITLE and shared:
            kind = "same"
        elif score >= POSSIBLE_TITLE or (shared and score >= POSSIBLE_WITH_DEADLINE):
            kind = "possible"
        else:
            continue
        detail = (f"title similarity {score:.2f}; "
                  + (f"shared deadline {', '.join(sorted(shared))}" if shared else "no shared deadline"))
        rank = (kind == "same", score)
        if best is None or rank > best[0]:
            best = (rank, kind, row, detail)
    return best[1:] if best else None


def family_of(conn, source_id):
    row = conn.execute("SELECT COALESCE(family, id) FROM sources WHERE id = ?", (source_id,)).fetchone()
    return row[0]


def add_listing(conn, call_id, source_id, item_key, url, now):
    """Record that a call is listed at a source item (again)."""
    conn.execute("INSERT INTO call_listings (call_id, source_id, item_key, url, first_seen_at, last_seen_at) "
                 "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (call_id, source_id, item_key) DO UPDATE SET "
                 "url = excluded.url, last_seen_at = excluded.last_seen_at, removed_at = NULL",
                 (call_id, source_id, item_key, url, now, now))


def add_flag(conn, call_id, kind, run_id, now, detail=None, other_call_id=None):
    """Raise a flag for the reviewer, unless the same one is already open."""
    conn.execute("INSERT INTO call_flags (call_id, kind, other_call_id, detail, run_id, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                 (call_id, kind, other_call_id, detail, run_id, now))
