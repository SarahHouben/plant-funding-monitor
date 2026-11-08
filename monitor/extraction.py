"""Extraction: ask Claude for the structured details of each candidate call.

Each candidate's call text (its page, PDF or EU topic) goes to the model, with
the fixed instructions and the tag list in a cached system prompt. The answer
must follow a JSON schema built from the tags table, and is checked in code
before anything is stored. A new call becomes a row in calls; a call whose URL
is already there is recorded in call_updates for step 13.
"""

import json
import re
import time
from collections import Counter
from datetime import date
from urllib.parse import urldefrag, urljoin

import anthropic

from monitor.calltext import CallTexts, thin_source
from monitor.db import ROOT, current_run, timestamp, utc_now
from monitor.dedupe import add_flag, add_listing, call_key, family_of, find_duplicate, resolve_flags

PROMPT_PATH = ROOT / "prompts" / "extraction.md"
DEFAULT_MODEL = "claude-sonnet-5-5"
EFFORT = "low"
MAX_TOKENS = 16000
# A candidate is skipped after this many failed attempts.
MAX_ATTEMPTS = 3
# Models that can hand a refused request to another model on the server. The
# option isn't available in Message Batches.
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# US dollars per million tokens: input, output, 5-minute cache write, cache read.
# Haiku 5.5's prices are for prompts up to 100,000 tokens.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
}
BATCH_DISCOUNT = 0.5
BATCH_POLL_SECONDS = 30
# For estimates only: characters per input token, and output per call with
# thinking included.
CHARS_PER_TOKEN = 2.6
ESTIMATED_OUTPUT_TOKENS = 2500

CALL_TYPES = ("deadline_bound", "recurring", "always_open")
STATUSES = ("forthcoming", "open", "extended", "closed", "withdrawn")
STILL_OPEN = ("forthcoming", "open", "extended")
IPK_ELIGIBLE = ("yes", "no", "unclear")
INSTITUTION_TYPES = ("university", "research_institute", "company", "sme", "non_profit",
                     "public_authority", "individual", "any")
EVIDENCE_FIELDS = ("title", "call_type", "status", "opening_date", "deadlines", "eligible_countries",
                   "eligible_institution_types", "eligibility_conditions", "ipk_eligible",
                   "funding_amount", "funding_duration", "apply_via", "apply_url", "more_info_links",
                   "contact_point", "relevance_score", "tags")
ANNUAL_DATE = re.compile(r"(\d{2})-(\d{2})")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
HAS_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://|^(mailto|tel):", re.I)


class AnswerError(Exception):
    """The model's answer can't be used at all."""


class StopExtraction(Exception):
    """An error that every further request would hit too, such as a bad API key."""


# --- Prompt and schema -------------------------------------------------------

def load_tags(conn):
    """The active tags, in the order of tags.csv."""
    return conn.execute("SELECT tag_id, tag_group, per_call, label, description FROM tags "
                        "WHERE retired = 0 ORDER BY rowid").fetchall()


def per_call_range(per_call):
    low, _, high = per_call.partition("-")
    return int(low), int(high or low)


def tags_block(tags):
    lines = ["tag_id | group | per_call | label | description"]
    lines += [f"{t['tag_id']} | {t['tag_group']} | {t['per_call']} | {t['label']} | {t['description'] or ''}"
              for t in tags]
    return "\n".join(lines)


def load_prompt(tags, path=PROMPT_PATH):
    """The fixed instructions with the tag list (cached), and the template for each call's input."""
    text = path.read_text(encoding="utf-8")
    instructions, marker, template = text.partition("\n## Input\n")
    if not marker:
        raise ValueError(f"{path} has no '## Input' section")
    return instructions.strip().replace("{{TAGS}}", tags_block(tags)), template.strip()


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def _choice(values):
    return {"type": "string", "enum": list(values)}


def output_schema(tags):
    """The JSON schema for an answer. Tags can only be tag_ids of their group,
    as a list where a call can have several and a single value otherwise."""
    groups = {}
    for tag in tags:
        groups.setdefault(tag["tag_group"], (tag["per_call"], []))[1].append(tag["tag_id"])
    tag_properties = {}
    for group, (per_call, tag_ids) in groups.items():
        tag_properties[group] = ({"type": "array", "items": _choice(tag_ids)}
                                 if per_call_range(per_call)[1] > 1 else _choice(tag_ids))
    text, day = {"type": "string"}, {"type": "string", "format": "date"}
    stage = _object({"stage": text, "date": _nullable(day), "annual_date": _nullable(text),
                     "note": _nullable(text)})
    call = _object({
        "title": text,
        "title_en": _nullable(text),
        "call_type": _choice(CALL_TYPES),
        "status": _choice(STATUSES),
        "opening_date": _nullable(day),
        "deadlines": {"type": "array", "items": stage},
        "summary_en": text,
        "summary_de": text,
        "eligible_countries": {"type": "array", "items": text},
        "eligible_institution_types": {"type": "array", "items": _choice(INSTITUTION_TYPES)},
        "eligibility_conditions": _nullable(text),
        "ipk_eligible": _choice(IPK_ELIGIBLE),
        "ipk_eligible_reason": text,
        "funding_amount": _nullable(text),
        "funding_duration": _nullable(text),
        "apply_via": _nullable(text),
        "apply_url": _nullable(text),
        "more_info_links": {"type": "array", "items": _object({"label": text, "url": text})},
        "contact_point": _nullable(text),
        "relevance_score": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "tags": _object(tag_properties),
        "evidence": {"type": "array", "items": _object({"field": _choice(EVIDENCE_FIELDS), "quote": text})},
    })
    return _object({"not_a_call": {"type": "boolean"}, "reason": _nullable(text), "call": _nullable(call)})


def user_message(template, source, candidate, call_text, today, note=None):
    """A call's input; the call text goes in last, so nothing in it is mistaken for a placeholder."""
    message = (template.replace("{{FUNDER}}", source["funder"])
               .replace("{{PROGRAMME}}", source["programme"])
               .replace("{{TODAY}}", today)
               .replace("{{CALL_TITLE}}", candidate["title"] or "")
               .replace("{{CALL_URL}}", candidate["url"])
               .replace("{{CALL_TEXT}}", call_text))
    if note:
        message += f"\n\n<note>\n{note}\n</note>"
    return message


# --- Requests ------------------------------------------------------------------

def request_params(model, instructions, message, schema):
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": instructions, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": message}],
        "output_config": {"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
    }


def ask(client, params):
    """One request. Models that support it get the server-side refusal fallback."""
    try:
        if params["model"] in FALLBACK_MODELS:
            return client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
        return client.messages.create(**params)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
            anthropic.BadRequestError, anthropic.NotFoundError) as e:
        raise StopExtraction(f"{type(e).__name__}: the API refused the request "
                             f"(status {e.status_code}): {e.message}") from None


def parse_answer(response):
    if response.stop_reason != "end_turn":
        raise AnswerError(f"the answer stopped early ({response.stop_reason})")
    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise AnswerError(f"the answer is not valid JSON ({e})") from None
    if not isinstance(data, dict) or "not_a_call" not in data:
        raise AnswerError("the answer is not in the expected shape")
    return data


# --- Checks --------------------------------------------------------------------

def parse_date(text):
    if not isinstance(text, str) or not ISO_DATE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def valid_annual_date(text):
    match = ANNUAL_DATE.fullmatch(text) if isinstance(text, str) else None
    if not match:
        return False
    try:
        date(2028, int(match.group(1)), int(match.group(2)))  # a leap year, so 02-29 counts
        return True
    except ValueError:
        return False


def _squash(text):
    return " ".join(text.split())


def check_answer(answer, call_text, tags, today):
    """Problems with an answer, as (check, message) pairs; none means it can be stored."""
    if answer.get("not_a_call"):
        return [] if (answer.get("reason") or "").strip() else [("format", "a not_a_call answer needs a reason")]
    call = answer.get("call")
    if not isinstance(call, dict):
        return [("format", "the answer has no call")]
    problems = []

    if call["opening_date"] is not None and parse_date(call["opening_date"]) is None:
        problems.append(("dates", f"opening_date {call['opening_date']!r} is not a valid YYYY-MM-DD date"))
    dated = []
    for n, stage in enumerate(call["deadlines"], 1):
        label = f"deadline stage {n} ({stage['stage']!r})"
        if (stage["date"] is None) == (stage["annual_date"] is None):
            problems.append(("dates", f"{label} needs exactly one of date and annual_date"))
        elif stage["date"] is not None:
            day = parse_date(stage["date"])
            if day is None:
                problems.append(("dates", f"{label} has an invalid date {stage['date']!r}"))
            else:
                dated.append(day)
        elif not valid_annual_date(stage["annual_date"]):
            problems.append(("dates", f"{label} has an invalid annual_date {stage['annual_date']!r} (MM-DD)"))
    if dated and all(day < today for day in dated) and call["status"] in STILL_OPEN:
        problems.append(("status", f"every dated deadline is before today ({today}), so the status "
                                   f"can't be {call['status']!r}; use 'closed' unless the text says "
                                   f"a deadline was extended to a later date"))

    for group, (low, high) in {t["tag_group"]: per_call_range(t["per_call"]) for t in tags}.items():
        value = call["tags"].get(group)
        chosen = set(value if isinstance(value, list) else [value] if value else [])
        if not low <= len(chosen) <= high:
            wanted = str(low) if low == high else f"{low} to {high}"
            problems.append(("tags", f"{group} needs {wanted} tag(s), but has {len(chosen)}"))

    text = _squash(call_text)
    for item in call["evidence"]:
        if not item["quote"].strip() or _squash(item["quote"]) not in text:
            problems.append(("evidence", f"the quote for {item['field']} is not verbatim in the call text: "
                                         f"{item['quote'][:100]!r}"))

    for url in [call["apply_url"]] + [link["url"] for link in call["more_info_links"]]:
        if url and url not in call_text:
            problems.append(("links", f"the URL {url!r} is not in the call text; copy URLs exactly"))
    return problems


def drop_undated_stages(answer):
    """The answer without deadline stages that have neither a date nor an annual
    date, and those stages' names. The prompt asks for dated stages only, but
    one that slips through shouldn't cost the whole call."""
    call = answer.get("call")
    if not isinstance(call, dict) or not isinstance(call.get("deadlines"), list):
        return answer, []
    dated = [stage for stage in call["deadlines"] if stage.get("date") is not None or stage.get("annual_date") is not None]
    dropped = [stage.get("stage") for stage in call["deadlines"] if stage not in dated]
    return ({**answer, "call": {**call, "deadlines": dated}} if dropped else answer), dropped


def evaluate(response, call_text, tags, today):
    """The answer with undated stages dropped, its problems, and the names of the dropped stages."""
    try:
        answer = parse_answer(response)
    except AnswerError as e:
        return None, [("format", str(e))], []
    answer, dropped = drop_undated_stages(answer)
    return answer, check_answer(answer, call_text, tags, today), dropped


def feedback(problems):
    return ("Your previous answer could not be used:\n"
            + "\n".join(f"- {check}: {message}" for check, message in problems)
            + "\nAnswer again, fixing these.")


# --- Storing -------------------------------------------------------------------

def next_occurrence(annual_date, today):
    """The next date on or after today with this month and day (29 February: the next leap year)."""
    month, day = int(annual_date[:2]), int(annual_date[3:])
    for year in range(today.year, today.year + 9):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    return None


def next_deadline(stages, today):
    """The earliest stage date from today on; for annual dates, their next occurrence."""
    upcoming = []
    for stage in stages:
        if stage.get("date"):
            day = date.fromisoformat(stage["date"])
            if day >= today:
                upcoming.append(day)
        elif stage.get("annual_date"):
            upcoming.append(next_occurrence(stage["annual_date"], today))
    upcoming = [day for day in upcoming if day is not None]
    return min(upcoming).isoformat() if upcoming else None


def call_identity(candidate, source):
    """The call's URL for display and its canonical key. A call without a link
    of its own shares the listing's URL with other calls, so its title is
    added: as a fragment to the URL, and normalised to the key."""
    url = urldefrag(candidate["url"])[0]
    if url == urldefrag(source["url"])[0]:
        return f"{url}#{candidate['title']}", call_key(url, candidate["title"] or "")
    return candidate["url"], call_key(candidate["url"])


def with_scheme(url, base):
    """A link as stored: one written without a scheme (www.ble.de) gets https://,
    and a relative one (/files/a.pdf) is resolved against the call's URL."""
    if HAS_SCHEME.match(url):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith(("/", "./", "../", "?", "#")):
        return urljoin(base, url)
    return "https://" + url


def complete_links(answer, base):
    """The answer with every link given a scheme. Done when storing, after the
    checks, which compare links with the call text as it is written."""
    call = answer.get("call")
    if not call:
        return answer
    call = {**call,
            "apply_url": with_scheme(call["apply_url"], base) if call["apply_url"] else None,
            "more_info_links": [{**link, "url": with_scheme(link["url"], base)}
                                for link in call["more_info_links"]]}
    return {**answer, "call": call}


def call_columns(call, today):
    """The calls columns for an extracted call (all but source, URLs and
    timestamps), and its tag_ids."""
    stages = [{key: value for key, value in stage.items() if value is not None} for stage in call["deadlines"]]
    evidence = {}
    for item in call["evidence"]:
        evidence.setdefault(item["field"], []).append(item["quote"])
    columns = {
        "title": call["title"], "title_en": call["title_en"],
        "call_type": call["call_type"], "status": call["status"], "opening_date": call["opening_date"],
        "deadlines": json.dumps(stages, ensure_ascii=False), "next_deadline": next_deadline(stages, today),
        "summary_en": call["summary_en"], "summary_de": call["summary_de"],
        "eligible_countries": json.dumps(call["eligible_countries"], ensure_ascii=False),
        "eligible_institution_types": json.dumps(call["eligible_institution_types"]),
        "eligibility_conditions": call["eligibility_conditions"], "ipk_eligible": call["ipk_eligible"],
        "ipk_eligible_reason": call["ipk_eligible_reason"], "funding_amount": call["funding_amount"],
        "funding_duration": call["funding_duration"], "apply_via": call["apply_via"],
        "apply_url": call["apply_url"],
        "more_info_links": json.dumps(call["more_info_links"], ensure_ascii=False),
        "contact_point": call["contact_point"], "relevance_score": call["relevance_score"],
        "evidence": json.dumps(evidence, ensure_ascii=False),
    }
    tag_ids = []
    for value in call["tags"].values():
        tag_ids += value if isinstance(value, list) else [value]
    return columns, list(dict.fromkeys(tag_ids))


def insert_call(conn, source_id, url, key, call, today, now):
    columns, tag_ids = call_columns(call, today)
    row = {"source_id": source_id, "original_url": url, "canonical_url": key, **columns,
           "first_seen_at": now, "last_verified_at": now}
    call_id = conn.execute(f"INSERT INTO calls ({', '.join(row)}) VALUES ({', '.join(':' + k for k in row)})",
                           row).lastrowid
    conn.executemany("INSERT INTO call_tags (call_id, tag_id) VALUES (?, ?)",
                     [(call_id, tag_id) for tag_id in tag_ids])
    return call_id


def known_call(conn, candidate, source):
    """The call a candidate's canonical URL already belongs to, or None."""
    _, key = call_identity(candidate, source)
    return conn.execute("SELECT id FROM calls WHERE canonical_url = ?", (key,)).fetchone()


def mark_extracted(conn, candidate, now):
    conn.execute("UPDATE candidates SET text = NULL, extracted_at = ?, last_error = NULL WHERE id = ?",
                 (now, candidate["id"]))


def store_result(conn, run_id, candidate, source, answer, today, now, thin=None):
    """Store a checked answer, then mark the candidate extracted and clear its text.

    A call whose canonical URL is known becomes an update for step 13 when its
    item changed, and otherwise just another listing. A new call that is
    clearly the same as a call of the same funder family is merged into it as
    a listing; a possible duplicate is stored and flagged. A new call or an
    update extracted from a thin text (thin: the reason, from
    calltext.thin_source) is flagged thin_source; an update from a full text
    resolves that flag. Returns what happened and the call's id.
    """
    answer = complete_links(answer, urldefrag(candidate["url"])[0])
    if answer["not_a_call"]:
        conn.execute("INSERT INTO screening_log (run_id, stage, source_id, title, url, category, reason, "
                     "screened_at) VALUES (?, 'extraction', ?, ?, ?, 'not_a_call', ?, ?)",
                     (run_id, candidate["source_id"], candidate["title"], candidate["url"],
                      answer["reason"], now))
        mark_extracted(conn, candidate, now)
        return "not a call", None

    url, key = call_identity(candidate, source)
    call = answer["call"]
    existing = conn.execute("SELECT id FROM calls WHERE canonical_url = ?", (key,)).fetchone()
    if existing and candidate["item_change"] == "changed":
        conn.execute("INSERT INTO call_updates (call_id, candidate_id, run_id, extracted, detected_at) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (existing["id"], candidate["id"], run_id, json.dumps(answer, ensure_ascii=False), now))
        outcome, call_id = "update for step 13", existing["id"]
        if thin:
            add_flag(conn, call_id, "thin_source", run_id, now, thin)
        else:
            resolve_flags(conn, call_id, "thin_source", now)
    elif existing:
        outcome, call_id = "known URL, listing added", existing["id"]
    else:
        match = find_duplicate(conn, family_of(conn, candidate["source_id"]), candidate["source_id"], key,
                               call["title"], call["title_en"], call["deadlines"])
        if match and match[0] == "same":
            # Its extraction can still fill gaps in the existing call (step 13).
            outcome, call_id = "merged into an existing call", match[1]["id"]
            conn.execute("INSERT INTO call_updates (call_id, candidate_id, run_id, kind, extracted, detected_at) "
                         "VALUES (?, ?, ?, 'merge', ?, ?)",
                         (call_id, candidate["id"], run_id, json.dumps(answer, ensure_ascii=False), now))
        else:
            call_id = insert_call(conn, candidate["source_id"], url, key, call, today, now)
            outcome = "new call"
            if match:
                add_flag(conn, call_id, "possible_duplicate", run_id, now, match[2], match[1]["id"])
                outcome = "new call, flagged as a possible duplicate"
            if thin:
                add_flag(conn, call_id, "thin_source", run_id, now, thin)
    add_listing(conn, call_id, candidate["source_id"], candidate["item_key"], url, now)
    mark_extracted(conn, candidate, now)
    return outcome, call_id


# --- Costs and the run record ----------------------------------------------------

def estimate_tokens(text):
    return int(len(text) / CHARS_PER_TOKEN) + 1


def thinking_tokens(usage):
    details = getattr(usage, "output_tokens_details", None)
    return (getattr(details, "thinking_tokens", None) or 0) if details else 0


def usage_cost(usage, model, batch=False):
    price_in, price_out, price_write, price_read = PRICES[model]
    dollars = (usage.input_tokens * price_in + usage.output_tokens * price_out
               + (usage.cache_creation_input_tokens or 0) * price_write
               + (usage.cache_read_input_tokens or 0) * price_read) / 1_000_000
    return dollars * (BATCH_DISCOUNT if batch else 1)


def estimate_cost(model, instructions, message, batch, cached):
    price_in, price_out, price_write, price_read = PRICES[model]
    dollars = (estimate_tokens(message) * price_in + ESTIMATED_OUTPUT_TOKENS * price_out
               + estimate_tokens(instructions) * (price_read if cached else price_write)) / 1_000_000
    return dollars * (BATCH_DISCOUNT if batch else 1)


class Tally:
    """What happened in one extraction run, for the report and the runs table."""

    def __init__(self, model, batch):
        self.model, self.batch = model, batch
        self.tokens = Counter()
        self.cost = self.thinking_cost = 0.0
        self.requests = 0
        self.outcomes = Counter()
        self.first_try_failures = Counter()   # check name -> candidates that failed it on the first try
        self.retried = 0
        self.problems = []                    # for runs.error_details
        self.notes = []                       # for runs.notes

    def add(self, conn, run_id, usage):
        thinking = thinking_tokens(usage)
        self.requests += 1
        self.tokens.update(input=usage.input_tokens, output=usage.output_tokens, thinking=thinking,
                           cache_read=usage.cache_read_input_tokens or 0,
                           cache_write=usage.cache_creation_input_tokens or 0)
        cost = usage_cost(usage, self.model, self.batch)
        self.cost += cost
        self.thinking_cost += thinking * PRICES[self.model][1] / 1_000_000 * (BATCH_DISCOUNT if self.batch else 1)
        with conn:
            conn.execute("UPDATE runs SET extraction_input_tokens = extraction_input_tokens + ?, "
                         "extraction_output_tokens = extraction_output_tokens + ?, "
                         "extraction_cache_read_tokens = extraction_cache_read_tokens + ?, "
                         "extraction_cache_write_tokens = extraction_cache_write_tokens + ?, "
                         "extraction_thinking_tokens = extraction_thinking_tokens + ? WHERE id = ?",
                         (usage.input_tokens, usage.output_tokens, usage.cache_read_input_tokens or 0,
                          usage.cache_creation_input_tokens or 0, thinking, run_id))
        return cost

    def first_try(self, problems):
        self.retried += 1
        self.first_try_failures.update({check for check, _ in problems})

    def report(self, out):
        out("Outcomes: " + ", ".join(f"{n} {outcome}" for outcome, n in self.outcomes.items()) if self.outcomes
            else "Outcomes: none")
        if self.retried:
            out(f"{self.retried} candidates failed a check on the first try and were retried; checks failed: "
                + ", ".join(f"{check} {n}" for check, n in self.first_try_failures.most_common()))
        else:
            out("No candidate needed a retry.")
        for note in self.notes:
            out(f"Note: {note['message']}")
        t = self.tokens
        share = f" ({self.thinking_cost / self.cost:.0%} of the cost)" if self.cost else ""
        out(f"Tokens: {t['input']} input, {t['cache_write']} cache write, {t['cache_read']} cache read, "
            f"{t['output']} output, of which {t['thinking']} thinking{share}.")
        out(f"Cost: ${self.cost:.4f}{' at batch prices' if self.batch else ''}; thinking ${self.thinking_cost:.4f}.")


# --- Running -------------------------------------------------------------------

def select_candidates(conn, ids=None, limit=None):
    """Candidates waiting for extraction, with the source item they came from."""
    sql = ("SELECT c.*, i.item_key, i.change AS item_change FROM candidates c "
           "JOIN item_changes i ON i.id = c.item_change_id WHERE c.extracted_at IS NULL AND c.attempts < ?")
    params = [MAX_ATTEMPTS]
    if ids:
        sql += f" AND c.id IN ({', '.join('?' * len(ids))})"
        params += list(ids)
    sql += " ORDER BY c.id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def extract(conn, client, fetcher, model=DEFAULT_MODEL, ids=None, limit=None, batch=False,
            max_cost=None, clock=utc_now, sleep=time.sleep, out=print):
    """Extract the selected candidates; return the Tally."""
    if model not in PRICES:
        raise ValueError(f"no prices for {model}; add them to PRICES in monitor/extraction.py")
    candidates = select_candidates(conn, ids, limit)
    if not candidates:
        out("Nothing to extract.")
        return None
    tags = load_tags(conn)
    instructions, template = load_prompt(tags)
    schema = output_schema(tags)
    sources = {row["id"]: row for row in conn.execute("SELECT * FROM sources")}
    run_id = current_run(conn, clock())
    today = clock().date()
    tally = Tally(model, batch)
    out(f"Extracting {len(candidates)} candidates with {model}"
        f"{' through Message Batches (half price)' if batch else ''}; recording in run {run_id}")

    texts = CallTexts(fetcher)
    jobs, planned = [], 0.0
    try:
        for position, candidate in enumerate(candidates):
            source = sources[candidate["source_id"]]
            known = known_call(conn, candidate, source)
            if known and candidate["item_change"] != "changed":
                # A call already stored under this URL: listed once more, not extracted again.
                with conn:
                    add_listing(conn, known["id"], candidate["source_id"], candidate["item_key"],
                                call_identity(candidate, source)[0], timestamp(clock()))
                    mark_extracted(conn, candidate, timestamp(clock()))
                tally.outcomes["known URL, listing added"] += 1
                out(f"  candidate {candidate['id']} ({candidate['source_id']}): already call #{known['id']}; "
                    f"listing added, not extracted again")
                continue
            text, origin = texts.get(candidate, source)
            message = user_message(template, source, candidate, text, today.isoformat())
            estimate = estimate_cost(model, instructions, message, batch, cached=bool(jobs))
            if max_cost is not None and planned + estimate > max_cost:
                out(f"Stopping before candidate {candidate['id']}: its estimated ${estimate:.3f} would take "
                    f"the run past --max-cost ${max_cost:.2f}.")
                tally.outcomes["not started (--max-cost)"] += len(candidates) - position
                break
            planned += estimate
            jobs.append({"candidate": candidate, "source": source, "text": text, "origin": origin,
                         "note": None})
            if not batch:
                _extract_one(conn, client, run_id, jobs[-1], instructions, template, schema, tags, today,
                             clock, tally, out)
                planned = tally.cost
        if batch and jobs:
            _extract_batch(conn, client, run_id, jobs, instructions, template, schema, tags, today,
                           clock, sleep, tally, out)
    except StopExtraction as e:
        tally.problems.append({"stage": "extraction", "message": str(e)})
        raise
    finally:
        with conn:
            details, notes = conn.execute("SELECT error_details, notes FROM runs WHERE id = ?", (run_id,)).fetchone()
            conn.execute("UPDATE runs SET finished_at = ?, errors = errors + ?, error_details = ?, notes = ? "
                         "WHERE id = ?",
                         (timestamp(clock()), len(tally.problems), json.dumps(json.loads(details) + tally.problems),
                          json.dumps(json.loads(notes) + tally.notes, ensure_ascii=False), run_id))
        tally.report(out)
    return tally


def _label(job):
    candidate = job["candidate"]
    return f"candidate {candidate['id']} ({candidate['source_id']}, {job['origin']}, {len(job['text'])} characters)"


def _finish(conn, run_id, job, answer, problems, today, clock, tally, out, cost=None):
    """Store an answer that passed the checks, or record the failure."""
    candidate = job["candidate"]
    if answer is not None and not problems:
        with conn:
            outcome, call_id = store_result(conn, run_id, candidate, job["source"], answer, today,
                                            timestamp(clock()), thin_source(job["text"], job["origin"]))
        tally.outcomes[outcome] += 1
        detail = f" #{call_id}" if call_id else ""
        if job.get("dropped"):
            stages = ", ".join(repr(stage) for stage in job["dropped"])
            message = (f"candidate {candidate['id']} ({candidate['source_id']}): dropped deadline stage(s) {stages}, "
                       f"which had neither a date nor an annual date")
            tally.notes.append({"stage": "extraction", "candidate_id": candidate["id"], "message": message})
            detail += f"; dropped undated stage(s) {stages}"
        out(f"  {_label(job)}: {outcome}{detail}" + (f"; ${cost:.4f}" if cost is not None else ""))
        return
    message = "; ".join(f"{check}: {text}" for check, text in problems)
    with conn:
        conn.execute("UPDATE candidates SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                     (message, candidate["id"]))
    tally.outcomes["failed"] += 1
    tally.problems.append({"stage": "extraction", "candidate_id": candidate["id"], "message": message})
    out(f"  {_label(job)}: FAILED after a retry: {message}")


def _extract_one(conn, client, run_id, job, instructions, template, schema, tags, today, clock, tally, out):
    """One candidate through the standard API: one request, and one retry with feedback."""
    note, cost = None, 0.0
    for attempt in (1, 2):
        message = user_message(template, job["source"], job["candidate"], job["text"], today.isoformat(), note)
        try:
            response = ask(client, request_params(tally.model, instructions, message, schema))
        except anthropic.APIError as e:
            # Not the candidate's fault, so it doesn't count as an attempt.
            tally.problems.append({"stage": "extraction", "candidate_id": job["candidate"]["id"],
                                   "message": f"API error: {type(e).__name__}"})
            tally.outcomes["API error"] += 1
            out(f"  {_label(job)}: API error ({type(e).__name__}); left for the next run")
            return
        cost += tally.add(conn, run_id, response.usage)
        answer, problems, job["dropped"] = evaluate(response, job["text"], tags, today)
        if not problems:
            break
        if attempt == 1:
            tally.first_try(problems)
            out(f"  {_label(job)}: retrying after " + "; ".join(f"{c}: {m}" for c, m in problems))
            note = feedback(problems)
    _finish(conn, run_id, job, answer, problems, today, clock, tally, out, cost)


def _extract_batch(conn, client, run_id, jobs, instructions, template, schema, tags, today, clock, sleep,
                   tally, out):
    """All candidates through Message Batches: one batch, then one more for the retries."""
    pending = jobs
    for round_number in (1, 2):
        requests = [{"custom_id": str(job["candidate"]["id"]),
                     "params": request_params(tally.model, instructions,
                                              user_message(template, job["source"], job["candidate"],
                                                           job["text"], today.isoformat(), job["note"]),
                                              schema)}
                    for job in pending]
        submitted = client.messages.batches.create(requests=requests)
        out(f"  Batch {submitted.id}: {len(requests)} requests submitted; checking every {BATCH_POLL_SECONDS} s")
        while client.messages.batches.retrieve(submitted.id).processing_status != "ended":
            sleep(BATCH_POLL_SECONDS)
        results = {result.custom_id: result for result in client.messages.batches.results(submitted.id)}
        retry = []
        for job in pending:
            result = results.get(str(job["candidate"]["id"]))
            if result is None or result.result.type != "succeeded":
                failure = result.result.type if result else "is missing"
                answer, problems, job["dropped"] = None, [("api", f"the batch request {failure}")], []
            else:
                tally.add(conn, run_id, result.result.message.usage)
                answer, problems, job["dropped"] = evaluate(result.result.message, job["text"], tags, today)
            if not problems or round_number == 2:
                _finish(conn, run_id, job, answer, problems, today, clock, tally, out)
            else:
                tally.first_try(problems)
                job["note"] = feedback(problems)
                retry.append(job)
        if not retry:
            break
        pending = retry


def dry_run(conn, fetcher, model=DEFAULT_MODEL, ids=None, limit=None, clock=utc_now, out=print):
    """Fetch the call texts and estimate tokens and cost, without calling the API."""
    if model not in PRICES:
        raise ValueError(f"no prices for {model}; add them to PRICES in monitor/extraction.py")
    candidates = select_candidates(conn, ids, limit)
    if not candidates:
        out("Nothing to extract.")
        return
    tags = load_tags(conn)
    instructions, template = load_prompt(tags)
    sources = {row["id"]: row for row in conn.execute("SELECT * FROM sources")}
    today = clock().date().isoformat()
    texts = CallTexts(fetcher)
    out(f"Dry run for {len(candidates)} candidates with {model}: fetching the call texts, no API calls. "
        f"Instructions with tags about {estimate_tokens(instructions)} tokens, cached.")
    standard = batch = 0.0
    origins = Counter()
    for candidate in candidates:
        source = sources[candidate["source_id"]]
        known = known_call(conn, candidate, source)
        if known and candidate["item_change"] != "changed":
            origins["known URL, not extracted"] += 1
            out(f"  candidate {candidate['id']:>4}  {candidate['source_id']:<20} already call #{known['id']}; "
                f"would only be listed")
            continue
        text, origin = texts.get(candidate, source)
        message = user_message(template, source, candidate, text, today)
        cached = sum(origins.values()) > origins["known URL, not extracted"]
        standard += estimate_cost(model, instructions, message, batch=False, cached=cached)
        batch += estimate_cost(model, instructions, message, batch=True, cached=cached)
        origins[origin.split(" (")[0]] += 1
        out(f"  candidate {candidate['id']:>4}  {candidate['source_id']:<20} {origin[:45]:<45} "
            f"{len(text):>6} characters  ~{estimate_tokens(message):>6} tokens")
    out("Texts from: " + ", ".join(f"{origin} {n}" for origin, n in origins.most_common()))
    out(f"Estimate at {ESTIMATED_OUTPUT_TOKENS} output tokens per call (thinking included): "
        f"${standard:.2f} with the standard API, ${batch:.2f} with --batch.")
