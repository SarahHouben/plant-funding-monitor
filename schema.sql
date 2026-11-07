-- Schema for data/funding.db. init_db.py applies it and loads the CSVs.
--
-- Dates are YYYY-MM-DD and timestamps are UTC, YYYY-MM-DDTHH:MM:SSZ; the
-- CHECKs reject anything else. JSON columns must hold the JSON type noted.
--
-- There is no migration system yet. When this schema changes, raise
-- user_version and rebuild the database.

PRAGMA user_version = 1;

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
    scope                 TEXT NOT NULL CHECK (scope IN ('national', 'eu', 'international')),
    check_frequency       TEXT NOT NULL CHECK (check_frequency IN ('daily', 'weekly', 'monthly')),
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
    pages_changed             INTEGER NOT NULL DEFAULT 0,
    new_calls                 INTEGER NOT NULL DEFAULT 0,
    errors                    INTEGER NOT NULL DEFAULT 0,
    -- [{"source_id": "dfg-calls", "message": "..."}, ...]
    error_details             TEXT NOT NULL DEFAULT '[]'
                              CHECK (json_valid(error_details) AND json_type(error_details) = 'array'),
    screening_input_tokens    INTEGER NOT NULL DEFAULT 0,
    screening_output_tokens   INTEGER NOT NULL DEFAULT 0,
    extraction_input_tokens   INTEGER NOT NULL DEFAULT 0,
    extraction_output_tokens  INTEGER NOT NULL DEFAULT 0
);

-- One row per funding call: the results table in PLAN.md.
CREATE TABLE IF NOT EXISTS calls (
    id                          INTEGER PRIMARY KEY,
    source_id                   TEXT NOT NULL REFERENCES sources (id),
    -- As the funder wrote it.
    title                       TEXT NOT NULL,
    -- English title, only when the original isn't in English.
    title_en                    TEXT,
    original_url                TEXT NOT NULL UNIQUE,
    -- recurring: fixed dates every year, such as 1 March and 1 August.
    call_type                   TEXT NOT NULL CHECK (call_type IN ('deadline_bound', 'recurring', 'always_open')),
    status                      TEXT NOT NULL CHECK (status IN ('forthcoming', 'open', 'extended', 'closed', 'withdrawn')),
    opening_date                TEXT CHECK (date(opening_date) IS opening_date),
    -- Every stage, e.g. [{"stage": "sketch", "date": "2027-01-14"},
    -- {"stage": "full proposal", "date": "2027-06-30"}].
    deadlines                   TEXT NOT NULL DEFAULT '[]'
                                CHECK (json_valid(deadlines) AND json_type(deadlines) = 'array'),
    -- The earliest stage date that hasn't passed, or the next occurrence for
    -- recurring calls. NULL for always-open calls. The pipeline updates it on
    -- every run.
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

-- Every item the screening step drops, so misses can be checked later.
CREATE TABLE IF NOT EXISTS screening_log (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES runs (id),
    source_id    TEXT    NOT NULL REFERENCES sources (id),
    title        TEXT,
    url          TEXT    NOT NULL,
    -- The screening question it failed: is it a call, is it relevant?
    category     TEXT    NOT NULL CHECK (category IN ('not_a_call', 'not_relevant')),
    reason       TEXT    NOT NULL,
    screened_at  TEXT    NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', screened_at) IS screened_at)
);

CREATE INDEX IF NOT EXISTS calls_next_deadline ON calls (next_deadline);
CREATE INDEX IF NOT EXISTS calls_source        ON calls (source_id);
CREATE INDEX IF NOT EXISTS call_tags_tag       ON call_tags (tag_id);
CREATE INDEX IF NOT EXISTS screening_log_run   ON screening_log (run_id);

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
