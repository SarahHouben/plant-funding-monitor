-- Schema for the database (data/dev.db locally, data/funding.db in the
-- GitHub job). init_db.py applies it and loads the CSVs.
--
-- Dates are YYYY-MM-DD and timestamps are UTC, YYYY-MM-DDTHH:MM:SSZ; the
-- CHECKs reject anything else. JSON columns must hold the JSON type noted.
--
-- There is no migration system yet. When this schema changes, raise
-- user_version and rebuild the database.

PRAGMA user_version = 7;

-- Keep SQLite's default rollback journal, not WAL, so the committed
-- funding.db is always complete on its own.
PRAGMA journal_mode = DELETE;

-- Funder pages the pipeline watches. The columns up to notes come from
-- sources.csv, which is the master copy, and init_db.py overwrites them. The
-- pipeline owns the columns after notes, and init_db.py never touches them.
CREATE TABLE IF NOT EXISTS sources (
    id                    TEXT NOT NULL PRIMARY KEY,
    funder                TEXT NOT NULL,
    programme             TEXT NOT NULL,
    url                   TEXT NOT NULL,
    access_method         TEXT NOT NULL CHECK (access_method IN ('rss', 'api', 'html', 'pdf')),
    -- CSS selector for the main content of the source's page or, for rss
    -- sources, of the pages their items link to. Empty means: try <main>,
    -- then the element marked role="main", then <article>, then the body.
    content_selector      TEXT,
    scope                 TEXT NOT NULL CHECK (scope IN ('national', 'eu', 'international')),
    -- Sources that list each other's calls share a family (federal funders and
    -- the Förderinfo portal, for example), so step 13 looks for duplicates
    -- across them. Empty means the source is a family of its own.
    family                TEXT,
    check_frequency      TEXT NOT NULL CHECK (check_frequency IN ('daily', 'weekly', 'monthly')),
    -- Set by people in sources.csv; the pipeline reports problems in
    -- last_error instead. init_db.py sets 'removed' when a row disappears
    -- from the CSV.
    status                TEXT NOT NULL CHECK (status IN ('active', 'needs_url', 'paused', 'broken', 'removed')),
    notes                 TEXT,
    content_hash          TEXT,
    last_checked_at       TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_checked_at) IS last_checked_at),
    last_changed_at       TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_changed_at) IS last_changed_at),
    last_error            TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0)
);

-- The tag vocabulary, loaded from tags.csv. Tags on a call must come from
-- here (see call_tags).
CREATE TABLE IF NOT EXISTS tags (
    tag_id       TEXT NOT NULL PRIMARY KEY,
    -- "group" in tags.csv, renamed because GROUP is an SQL keyword. The
    -- calls_search view has one column per group, so a new group needs a
    -- column there as well as a value here.
    tag_group    TEXT NOT NULL CHECK (tag_group IN ('research_area', 'organism', 'funding_type', 'career_stage')),
    -- How many tags from this group a call can have: "1" or a range like "1-3".
    per_call     TEXT NOT NULL CHECK (per_call GLOB '[0-9]' OR per_call GLOB '[0-9]-[0-9]'),
    label        TEXT NOT NULL,
    description  TEXT,
    -- 1 when the tag has been removed from tags.csv. It is kept because calls
    -- may still use it, but the pipeline must not assign it.
    retired      INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1))
);

-- One row per pipeline run.
CREATE TABLE IF NOT EXISTS runs (
    id                        INTEGER PRIMARY KEY,
    started_at                TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', started_at) IS started_at),
    -- NULL while the run is going, or if it crashed.
    finished_at               TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', finished_at) IS finished_at),
    sources_checked           INTEGER NOT NULL DEFAULT 0,
    -- Sources with at least one new, changed or removed item.
    pages_changed             INTEGER NOT NULL DEFAULT 0,
    new_calls                 INTEGER NOT NULL DEFAULT 0,
    errors                    INTEGER NOT NULL DEFAULT 0,
    -- [{"source_id": "dfg-calls", "message": "..."}, ...]
    error_details             TEXT NOT NULL DEFAULT '[]'
                              CHECK (json_valid(error_details) AND json_type(error_details) = 'array'),
    -- Things worth knowing that aren't errors, such as a deadline stage the
    -- extraction step dropped because it had no date:
    -- [{"stage": "extraction", "candidate_id": 142, "message": "..."}, ...]
    notes                     TEXT NOT NULL DEFAULT '[]'
                              CHECK (json_valid(notes) AND json_type(notes) = 'array'),
    -- Input tokens not read from or written to the cache; cached input is
    -- counted separately because it is priced differently.
    screening_input_tokens    INTEGER NOT NULL DEFAULT 0,
    screening_output_tokens   INTEGER NOT NULL DEFAULT 0,
    screening_cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    screening_cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    extraction_input_tokens   INTEGER NOT NULL DEFAULT 0,
    -- Includes the thinking tokens, which are also counted on their own.
    extraction_output_tokens  INTEGER NOT NULL DEFAULT 0,
    extraction_cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    extraction_cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    extraction_thinking_tokens     INTEGER NOT NULL DEFAULT 0
);

-- One row per funding call: the results table in PLAN.md.
CREATE TABLE IF NOT EXISTS calls (
    id                          INTEGER PRIMARY KEY,
    source_id                   TEXT NOT NULL REFERENCES sources (id),
    -- As the funder wrote it.
    title                       TEXT NOT NULL,
    -- English title, only when the original isn't in English.
    title_en                    TEXT,
    -- The call's URL as found, for display.
    original_url                TEXT NOT NULL,
    -- The URL normalised for comparison (see monitor/urls.py), which identifies
    -- the call. A call without a link of its own gets its listing's URL with
    -- its normalised title as the fragment.
    canonical_url               TEXT NOT NULL UNIQUE,
    -- recurring: fixed dates every year, such as 1 March and 1 August.
    call_type                   TEXT NOT NULL CHECK (call_type IN ('deadline_bound', 'recurring', 'always_open')),
    status                      TEXT NOT NULL CHECK (status IN ('forthcoming', 'open', 'extended', 'closed', 'withdrawn')),
    opening_date                TEXT CHECK (date(opening_date) IS opening_date),
    -- Every stage, e.g. [{"stage": "sketch", "date": "2027-01-14", "note": "12:00 CET"},
    -- {"stage": "full proposal", "date": "2027-06-30"}]. Recurring calls use
    -- "annual_date": "MM-DD" instead of "date". A CHECK can't look inside each
    -- stage, so the pipeline validates them.
    deadlines                   TEXT NOT NULL DEFAULT '[]'
                                CHECK (json_valid(deadlines) AND json_type(deadlines) = 'array'),
    -- The earliest stage date that hasn't passed, or the next occurrence of an
    -- annual_date for recurring calls. NULL for always-open calls. The pipeline
    -- updates it on every run.
    next_deadline               TEXT CHECK (date(next_deadline) IS next_deadline),
    summary_en                  TEXT NOT NULL,
    summary_de                  TEXT NOT NULL,
    -- Who may apply, as JSON arrays of strings.
    eligible_countries          TEXT NOT NULL DEFAULT '[]'
                                CHECK (json_valid(eligible_countries) AND json_type(eligible_countries) = 'array'),
    eligible_institution_types  TEXT NOT NULL DEFAULT '[]'
                                CHECK (json_valid(eligible_institution_types) AND json_type(eligible_institution_types) = 'array'),
    -- Any other condition, e.g. "Lower Saxony only".
    eligibility_conditions      TEXT,
    ipk_eligible                TEXT NOT NULL DEFAULT 'unclear' CHECK (ipk_eligible IN ('yes', 'no', 'unclear')),
    ipk_eligible_reason         TEXT,
    -- Free text, since calls are often vague: "up to EUR 1.5 million", "5 years".
    funding_amount              TEXT,
    funding_duration            TEXT,
    -- How to apply, e.g. "easy-Online", and the link to the application
    -- system or form if the call gives one.
    apply_via                   TEXT,
    apply_url                   TEXT,
    -- Further links from the call, e.g. [{"label": "Funding guideline", "url": "https://..."}].
    more_info_links             TEXT NOT NULL DEFAULT '[]'
                                CHECK (json_valid(more_info_links) AND json_type(more_info_links) = 'array'),
    -- The responsible organisation or unit and any functional contact, such
    -- as a team mailbox. Never a named person.
    contact_point               TEXT,
    -- The model's judgement of fit to plant genomics.
    relevance_score             INTEGER NOT NULL CHECK (relevance_score BETWEEN 1 AND 5),
    review_status               TEXT NOT NULL DEFAULT 'new' CHECK (review_status IN ('new', 'checked', 'rejected')),
    first_seen_at               TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', first_seen_at) IS first_seen_at),
    last_verified_at            TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_verified_at) IS last_verified_at),
    -- The source sentences each extracted field came from, e.g.
    -- {"deadlines": ["Sketches are due on 14 January 2027."], "ipk_eligible": [...]}.
    evidence                    TEXT NOT NULL DEFAULT '{}'
                                CHECK (json_valid(evidence) AND json_type(evidence) = 'object')
);

CREATE TABLE IF NOT EXISTS call_tags (
    call_id  INTEGER NOT NULL REFERENCES calls (id) ON DELETE CASCADE,
    tag_id   TEXT    NOT NULL REFERENCES tags (tag_id),
    PRIMARY KEY (call_id, tag_id)
);

-- Every item the pipeline drops, so misses can be checked later.
CREATE TABLE IF NOT EXISTS screening_log (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES runs (id),
    -- The step that dropped it. Extraction drops an item when the full text
    -- turns out not to describe a call.
    stage        TEXT    NOT NULL CHECK (stage IN ('screening', 'extraction')),
    source_id    TEXT    NOT NULL REFERENCES sources (id),
    title        TEXT,
    url          TEXT    NOT NULL,
    -- The question it failed: is it a call, is it relevant? Extraction only
    -- answers the first.
    category     TEXT    NOT NULL CHECK (category IN ('not_a_call', 'not_relevant')),
    reason       TEXT    NOT NULL,
    screened_at  TEXT    NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', screened_at) IS screened_at)
);

-- What the fetch step last saw of each source, item by item: a feed entry
-- (keyed by its ID, or its link if it has none), an EU topic (keyed by its
-- identifier) or a section of an HTML page (keyed by the source URL, "#" and
-- the section's heading path). Only fingerprints and titles are kept, never
-- texts, because the database is public: no copies of funder pages.
CREATE TABLE IF NOT EXISTS source_items (
    source_id        TEXT NOT NULL REFERENCES sources (id),
    item_key         TEXT NOT NULL,
    url              TEXT NOT NULL,
    title            TEXT,
    -- Hash of the item's content after volatile parts and personal contact
    -- details are removed. For feed entries it covers only what the feed
    -- says, not the excerpt of the linked page.
    fingerprint      TEXT NOT NULL,
    first_seen_at    TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', first_seen_at) IS first_seen_at),
    last_seen_at     TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_seen_at) IS last_seen_at),
    last_changed_at  TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_changed_at) IS last_changed_at),
    -- When a page section or EU topic disappeared. Cleared if it comes back.
    -- Feed entries are never marked, since feeds only show their newest items.
    removed_at       TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', removed_at) IS removed_at),
    PRIMARY KEY (source_id, item_key)
);

-- What the fetch step found new, changed or removed: the input for screening.
CREATE TABLE IF NOT EXISTS item_changes (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES runs (id),
    source_id    TEXT    NOT NULL REFERENCES sources (id),
    item_key     TEXT    NOT NULL,
    url          TEXT    NOT NULL,
    title        TEXT,
    change       TEXT    NOT NULL CHECK (change IN ('new', 'changed', 'removed')),
    -- The hand-over to screening: the item's current text when new or changed
    -- (personal contact details already removed), or "No longer listed: ..."
    -- when removed. The screening step sets it to NULL once the item is
    -- screened, so the public database keeps no copies of funder pages.
    text         TEXT,
    detected_at  TEXT    NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', detected_at) IS detected_at),
    -- Set by the screening step once it has looked at the item. Removed items
    -- skip screening; step 13 sets this when it has dealt with them.
    screened_at  TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', screened_at) IS screened_at),
    CHECK (text IS NOT NULL OR screened_at IS NOT NULL)
);

-- Calls found by screening, waiting for extraction (step 12): one row per call,
-- so a listing section with three calls gives three rows.
CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES runs (id),
    item_change_id  INTEGER NOT NULL REFERENCES item_changes (id),
    source_id       TEXT    NOT NULL REFERENCES sources (id),
    -- The call's own link, or the item's URL if the call has none.
    url             TEXT    NOT NULL,
    title           TEXT,
    reason          TEXT    NOT NULL,
    -- The text of the item the call was found in: the hand-over to
    -- extraction, for calls whose own page is a PDF or needs JavaScript. The
    -- extraction step sets it to NULL once it is done, so the public database
    -- keeps no copies of funder pages.
    text            TEXT,
    created_at      TEXT    NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    extracted_at    TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', extracted_at) IS extracted_at),
    -- Failed extraction attempts; the extraction step skips a candidate after
    -- three, so a broken page doesn't cost money every day.
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error      TEXT,
    CHECK (text IS NOT NULL OR extracted_at IS NOT NULL)
);

-- Extraction results for calls already in the calls table: the existing row is
-- kept, and step 13 decides what to update from the new result.
CREATE TABLE IF NOT EXISTS call_updates (
    id            INTEGER PRIMARY KEY,
    call_id       INTEGER NOT NULL REFERENCES calls (id),
    candidate_id  INTEGER NOT NULL REFERENCES candidates (id),
    run_id        INTEGER NOT NULL REFERENCES runs (id),
    -- update: a new extraction of the call's own page, after its item changed.
    -- merge: the extraction of a duplicate merged into the call, which only
    -- fills fields the call has empty or 'unclear'.
    kind          TEXT    NOT NULL DEFAULT 'update' CHECK (kind IN ('update', 'merge')),
    -- The extraction result as JSON, in the shape the extraction prompt returns.
    extracted     TEXT    NOT NULL CHECK (json_valid(extracted) AND json_type(extracted) = 'object'),
    detected_at   TEXT    NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', detected_at) IS detected_at),
    -- Set by step 13 once it has dealt with the update.
    handled_at    TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', handled_at) IS handled_at)
);

-- Every place a call is listed: the source item where it was found, and the URL
-- it was listed under. The search page shows these as "also listed at", and
-- step 13 notices from them when a call is no longer listed anywhere.
CREATE TABLE IF NOT EXISTS call_listings (
    id             INTEGER PRIMARY KEY,
    call_id        INTEGER NOT NULL REFERENCES calls (id),
    source_id      TEXT    NOT NULL REFERENCES sources (id),
    item_key       TEXT    NOT NULL,
    url            TEXT    NOT NULL,
    first_seen_at  TEXT    NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', first_seen_at) IS first_seen_at),
    last_seen_at   TEXT    NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_seen_at) IS last_seen_at),
    -- When the source item disappeared. Cleared if the call is listed there again.
    removed_at     TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', removed_at) IS removed_at),
    UNIQUE (call_id, source_id, item_key)
);

-- Every change step 13 makes to a call, field by field. Lists and objects are
-- stored as JSON, as in calls; tags as a JSON list of tag_ids.
CREATE TABLE IF NOT EXISTS call_history (
    id          INTEGER PRIMARY KEY,
    call_id     INTEGER NOT NULL REFERENCES calls (id),
    field       TEXT    NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    run_id      INTEGER NOT NULL REFERENCES runs (id),
    changed_at  TEXT    NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', changed_at) IS changed_at)
);

-- Things the reviewer should look at. A flag stays open until resolved_at is set.
CREATE TABLE IF NOT EXISTS call_flags (
    id             INTEGER PRIMARY KEY,
    call_id        INTEGER NOT NULL REFERENCES calls (id),
    -- possible_duplicate: another call looks like the same one (other_call_id).
    -- section_removed: the call is no longer listed anywhere but hasn't ended.
    -- not_a_call: a new extraction of the call's page found no call there.
    -- merge_conflict: a merged duplicate gives different deadlines, status or
    -- eligibility; the call kept its own values.
    -- deadline_missing: a call that hasn't ended has no deadline and doesn't
    -- say applications are possible at any time. Resolved by the update stage
    -- once the call has a deadline or has ended.
    -- thin_source: the call was extracted from the listing's stored text or
    -- from a short text, so details may be missing.
    kind           TEXT    NOT NULL CHECK (kind IN ('possible_duplicate', 'section_removed', 'not_a_call',
                                                    'merge_conflict', 'deadline_missing', 'thin_source')),
    other_call_id  INTEGER REFERENCES calls (id),
    detail         TEXT,
    run_id         INTEGER NOT NULL REFERENCES runs (id),
    created_at     TEXT    NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
    resolved_at    TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', resolved_at) IS resolved_at)
);

-- The same open flag isn't raised twice.
CREATE UNIQUE INDEX IF NOT EXISTS call_flags_open
    ON call_flags (call_id, kind, IFNULL(other_call_id, 0)) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS call_listings_item  ON call_listings (source_id, item_key);
CREATE INDEX IF NOT EXISTS call_history_call   ON call_history (call_id);

-- A call waits for extraction only once, even if several items list it. URL and
-- title together identify it, since calls without a link of their own share
-- the URL of the page that lists them.
CREATE UNIQUE INDEX IF NOT EXISTS candidates_waiting ON candidates (url, title) WHERE extracted_at IS NULL;

CREATE INDEX IF NOT EXISTS calls_next_deadline ON calls (next_deadline);
CREATE INDEX IF NOT EXISTS calls_source        ON calls (source_id);
CREATE INDEX IF NOT EXISTS call_tags_tag       ON call_tags (tag_id);
CREATE INDEX IF NOT EXISTS screening_log_run   ON screening_log (run_id);
CREATE INDEX IF NOT EXISTS item_changes_run    ON item_changes (run_id);

-- What the search page shows: funder and scope from the source, one JSON
-- array of tag labels per tag group (Datasette can facet these with
-- _facet_array), and no rejected calls. It holds no data, so it is rebuilt
-- every time.
DROP VIEW IF EXISTS calls_search;
CREATE VIEW calls_search AS
SELECT
    c.id,
    c.title,
    c.title_en,
    s.funder,
    s.programme,
    s.scope,
    c.call_type,
    c.status,
    c.opening_date,
    c.next_deadline,
    c.deadlines,
    c.summary_en,
    c.summary_de,
    c.ipk_eligible,
    c.ipk_eligible_reason,
    c.eligible_countries,
    c.eligible_institution_types,
    c.eligibility_conditions,
    c.funding_amount,
    c.funding_duration,
    c.apply_via,
    c.apply_url,
    c.more_info_links,
    c.contact_point,
    c.relevance_score,
    (SELECT json_group_array(t.label) FROM call_tags ct JOIN tags t USING (tag_id)
      WHERE ct.call_id = c.id AND t.tag_group = 'research_area') AS research_areas,
    (SELECT json_group_array(t.label) FROM call_tags ct JOIN tags t USING (tag_id)
      WHERE ct.call_id = c.id AND t.tag_group = 'organism') AS organisms,
    (SELECT json_group_array(t.label) FROM call_tags ct JOIN tags t USING (tag_id)
      WHERE ct.call_id = c.id AND t.tag_group = 'funding_type') AS funding_types,
    (SELECT json_group_array(t.label) FROM call_tags ct JOIN tags t USING (tag_id)
      WHERE ct.call_id = c.id AND t.tag_group = 'career_stage') AS career_stages,
    c.review_status,
    c.original_url,
    c.first_seen_at,
    c.last_verified_at
FROM calls c
JOIN sources s ON s.id = c.source_id
WHERE c.review_status <> 'rejected';
