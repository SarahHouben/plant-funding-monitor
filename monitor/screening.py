"""Screening: ask Claude Haiku which new and changed items contain relevant funding calls.

Unscreened items go to the model in batches, one source per batch. A result
judged a call becomes a candidate for extraction; every other result goes to
the screening log. Then the item's text is cleared, because the database is
public.
"""

import json
import os
from collections import Counter

import anthropic

from monitor.db import ROOT, current_run, timestamp, utc_now

PROMPT_PATH = ROOT / "prompts" / "screening.md"
ENV_FILE = ROOT / ".env"
MODEL = "claude-haiku-5-5"
EFFORT = "low"
# Room for adaptive thinking as well as the answer.
MAX_TOKENS = 16000
BATCH_ITEMS = 20
BATCH_TOKENS = 30_000
# Characters per token for the items as sent (German and English Markdown in
# JSON), measured on the first full run; only used for batching and --dry-run.
CHARS_PER_TOKEN = 2.6
CATEGORIES = ("call", "not_a_call", "not_relevant")
# Claude Haiku 5.5, US dollars per million tokens, for prompts up to 100,000
# tokens. Cache writes (5-minute TTL) cost 1.25 times input, reads 0.1 times.
PRICE_INPUT = 0.10
PRICE_OUTPUT = 0.50
PRICE_CACHE_WRITE = 0.125
PRICE_CACHE_READ = 0.01
# Output per item assumed by --dry-run (results plus thinking), as measured on
# the first full run.
ESTIMATED_OUTPUT_PER_ITEM = 180

RESULTS_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "reason": {"type": "string"},
                },
                "required": ["item_id", "url", "title", "category", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}


class AnswerError(Exception):
    """The model's answer can't be used at all."""


class StopScreening(Exception):
    """An error that every further request would hit too, such as a bad API key."""


def load_api_key(env_file=ENV_FILE):
    """ANTHROPIC_API_KEY from the environment (the GitHub job), or else from .env.

    The key is never printed or logged, and no message here contains it.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and name.strip() == "ANTHROPIC_API_KEY":
            value = value.strip()
            if value[:1] in ("'", '"'):
                return value[1:].split(value[0], 1)[0] or None
            return value.split(" #", 1)[0].strip() or None
    return None


def load_prompt(path=PROMPT_PATH):
    """The fixed instructions (cached) and the template for each request's input."""
    text = path.read_text(encoding="utf-8")
    instructions, marker, template = text.partition("\n## Input\n")
    if not marker:
        raise ValueError(f"{path} has no '## Input' section")
    return instructions.strip(), template.strip()


def unscreened_items(conn, limit=None):
    """New and changed items not yet screened, oldest first. Removed items are left for step 13."""
    sql = ("SELECT * FROM item_changes WHERE screened_at IS NULL AND change IN ('new', 'changed') "
           "ORDER BY id")
    rows = conn.execute(sql + (" LIMIT ?" if limit else ""), (limit,) if limit else ())
    return rows.fetchall()


def estimate_tokens(text):
    return int(len(text) / CHARS_PER_TOKEN) + 1


def make_batches(items):
    """Group items by source, with at most BATCH_ITEMS and about BATCH_TOKENS per batch."""
    by_source = {}
    for item in items:
        by_source.setdefault(item["source_id"], []).append(item)
    batches = []
    for group in by_source.values():
        batch, tokens = [], 0
        for item in group:
            size = estimate_tokens(item["text"])
            if batch and (len(batch) == BATCH_ITEMS or tokens + size > BATCH_TOKENS):
                batches.append(batch)
                batch, tokens = [], 0
            batch.append(item)
            tokens += size
        if batch:
            batches.append(batch)
    return batches


def user_message(template, source, items, today, note=None):
    """The request's input: the source, today's date and the items, plus what
    went wrong last time when items are sent again."""
    payload = json.dumps([{"item_id": str(item["id"]), "change": item["change"], "url": item["url"],
                           "title": item["title"] or "", "text": item["text"]} for item in items],
                         ensure_ascii=False, indent=1)
    message = (template.replace("{{FUNDER}}", source["funder"])
               .replace("{{PROGRAMME}}", source["programme"])
               .replace("{{SOURCE_URL}}", source["url"])
               .replace("{{TODAY}}", today)
               .replace("{{ITEMS}}", payload))
    if note:
        message += f"\n\n<note>\n{note}\n</note>"
    return message


def ask(client, instructions, message):
    return client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": instructions, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": message}],
        output_config={"effort": EFFORT,
                       "format": {"type": "json_schema", "schema": RESULTS_SCHEMA}},
    )


def parse_answer(response):
    """The list of results in a response, or AnswerError if there is none to use."""
    if response.stop_reason != "end_turn":
        raise AnswerError(f"the answer stopped early ({response.stop_reason})")
    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise AnswerError(f"the answer is not valid JSON ({e})") from None
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise AnswerError("the answer has no list of results")
    return results


def check_results(items, results):
    """Sort the results by item and check them.

    Returns the accepted results by item id, and a problem by item id for each
    item to send again: no result at all, a result with a missing field or an
    unknown category, or a URL that is neither in the item's text nor the
    item's own URL. Results for ids not in the batch are ignored.
    """
    by_id = {str(item["id"]): item for item in items}
    grouped = {item_id: [] for item_id in by_id}
    problems = {}
    for result in results:
        item = by_id.get(str(result.get("item_id"))) if isinstance(result, dict) else None
        if item is None:
            continue
        item_id = str(item["id"])
        missing = [key for key in ("url", "title", "category", "reason")
                   if not isinstance(result.get(key), str)]
        url = (result.get("url") or "").strip() if not missing else ""
        if missing:
            problems[item_id] = f"a result is missing {', '.join(missing)}"
        elif result["category"] not in CATEGORIES:
            problems[item_id] = f"unknown category {result['category']!r}"
        elif not url or (url != item["url"] and url not in item["text"]):
            problems[item_id] = (f"the URL {url!r} is neither in the item's text nor the item's "
                                 f"own URL; copy URLs exactly")
        else:
            grouped[item_id].append({**result, "url": url})
    for item_id, found in grouped.items():
        if not found and item_id not in problems:
            problems[item_id] = "no result for this item"
    accepted = {item_id: found for item_id, found in grouped.items() if item_id not in problems}
    return accepted, problems


def record_usage(conn, run_id, usage):
    conn.execute("UPDATE runs SET screening_input_tokens = screening_input_tokens + ?, "
                 "screening_output_tokens = screening_output_tokens + ?, "
                 "screening_cache_read_tokens = screening_cache_read_tokens + ?, "
                 "screening_cache_write_tokens = screening_cache_write_tokens + ? WHERE id = ?",
                 (usage.input_tokens, usage.output_tokens, usage.cache_read_input_tokens or 0,
                  usage.cache_creation_input_tokens or 0, run_id))


def store_results(conn, run_id, item, results, now):
    """Calls become candidates, everything else goes to the screening log, and
    the item is marked screened with its text cleared."""
    for result in results:
        if result["category"] == "call":
            # A call already waiting for extraction (same URL and title) is kept once.
            conn.execute("INSERT INTO candidates (run_id, item_change_id, source_id, url, title, reason, "
                         "text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                         (run_id, item["id"], item["source_id"], result["url"], result["title"],
                          result["reason"], item["text"], now))
        else:
            conn.execute("INSERT INTO screening_log (run_id, stage, source_id, title, url, category, "
                         "reason, screened_at) VALUES (?, 'screening', ?, ?, ?, ?, ?, ?)",
                         (run_id, item["source_id"], result["title"], result["url"],
                          result["category"], result["reason"], now))
    conn.execute("UPDATE item_changes SET text = NULL, screened_at = ? WHERE id = ?", (now, item["id"]))


def cost(input_tokens, output_tokens, cache_read=0, cache_write=0):
    """Estimated cost in US dollars."""
    return (input_tokens * PRICE_INPUT + output_tokens * PRICE_OUTPUT
            + cache_read * PRICE_CACHE_READ + cache_write * PRICE_CACHE_WRITE) / 1_000_000


def screen(conn, client, limit=None, clock=utc_now, out=print):
    """Screen unscreened items; return the id of the run the numbers went to."""
    items = unscreened_items(conn, limit)
    if not items:
        out("Nothing to screen.")
        return None
    instructions, template = load_prompt()
    sources = {row["id"]: row for row in conn.execute("SELECT * FROM sources")}
    run_id = current_run(conn, clock())
    batches = make_batches(items)
    out(f"Screening {len(items)} items in {len(batches)} batches with {MODEL}; recording in run {run_id}")

    counts, problems = Counter(), []
    usage_totals = Counter()
    try:
        screen_batches(conn, client, run_id, batches, sources, instructions, template, clock,
                       counts, problems, usage_totals, out)
    except StopScreening as e:
        problems.append({"stage": "screening", "message": str(e)})
        raise
    finally:
        with conn:
            details = json.loads(conn.execute("SELECT error_details FROM runs WHERE id = ?",
                                              (run_id,)).fetchone()[0])
            conn.execute("UPDATE runs SET finished_at = ?, errors = errors + ?, error_details = ? "
                         "WHERE id = ?", (timestamp(clock()), len(problems),
                                          json.dumps(details + problems), run_id))
        report(counts, problems, usage_totals, out)
    return run_id


def screen_batches(conn, client, run_id, batches, sources, instructions, template, clock,
                   counts, problems, usage_totals, out):
    """Screen each batch, retrying failed items once. Updates counts, problems
    and usage_totals as it goes, so they are complete even if it stops early."""
    for number, batch in enumerate(batches, 1):
        source = sources[batch[0]["source_id"]]
        pending, note = batch, None
        batch_counts = Counter()
        for _attempt in range(2):
            message = user_message(template, source, pending, clock().date().isoformat(), note)
            try:
                response = ask(client, instructions, message)
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                    anthropic.BadRequestError, anthropic.NotFoundError) as e:
                raise StopScreening(f"{type(e).__name__}: the API refused the request "
                                    f"(status {e.status_code}); stopping") from None
            except anthropic.APIError as e:
                failure = {str(item["id"]): f"API error: {type(e).__name__}" for item in pending}
                break
            with conn:
                record_usage(conn, run_id, response.usage)
            usage_totals.update(input=response.usage.input_tokens, output=response.usage.output_tokens,
                                cache_read=response.usage.cache_read_input_tokens or 0,
                                cache_write=response.usage.cache_creation_input_tokens or 0)
            try:
                accepted, failure = check_results(pending, parse_answer(response))
            except AnswerError as e:
                accepted, failure = {}, {str(item["id"]): str(e) for item in pending}
            now = timestamp(clock())
            with conn:
                for item in pending:
                    found = accepted.get(str(item["id"]))
                    if found is not None:
                        store_results(conn, run_id, item, found, now)
                        batch_counts.update(r["category"] for r in found)
            pending = [item for item in pending if str(item["id"]) in failure]
            if not pending:
                break
            note = ("Your previous answer for these items could not be used:\n"
                    + "\n".join(f"- item {item_id}: {problem}" for item_id, problem in failure.items()))
        for item in pending:
            problems.append({"stage": "screening", "source_id": item["source_id"],
                             "item_change_id": item["id"], "message": failure[str(item["id"])]})
        counts.update({(source["id"], category): n for category, n in batch_counts.items()})
        out(f"  batch {number:>2}/{len(batches)}  {source['id']:<20} {len(batch):>3} items: "
            + ", ".join(f"{batch_counts[c]} {c}" for c in CATEGORIES)
            + (f"; {len(pending)} left unscreened" if pending else ""))


def report(counts, problems, usage, out):
    out("Results by source (call / not_a_call / not_relevant):")
    for source_id in sorted({s for s, _ in counts}):
        out(f"  {source_id:<20} " + " / ".join(str(counts[(source_id, c)]) for c in CATEGORIES))
    total = Counter()
    for (_, category), n in counts.items():
        total[category] += n
    out(f"  {'all':<20} " + " / ".join(str(total[c]) for c in CATEGORIES))
    if problems:
        out(f"{len(problems)} problems; these items stay unscreened:")
        for problem in problems:
            where = (f"item {problem['item_change_id']} ({problem['source_id']})"
                     if "item_change_id" in problem else "run")
            out(f"  {where}: {problem['message']}")
    dollars = cost(usage["input"], usage["output"], usage["cache_read"], usage["cache_write"])
    out(f"Tokens: {usage['input']} input, {usage['cache_write']} cache write, "
        f"{usage['cache_read']} cache read, {usage['output']} output. Estimated cost ${dollars:.4f}")


def dry_run(conn, limit=None, clock=utc_now, out=print):
    """Show the batches and an estimate of tokens and cost, without calling the API."""
    items = unscreened_items(conn, limit)
    if not items:
        out("Nothing to screen.")
        return
    instructions, template = load_prompt()
    sources = {row["id"]: row for row in conn.execute("SELECT * FROM sources")}
    batches = make_batches(items)
    today = clock().date().isoformat()
    instruction_tokens = estimate_tokens(instructions)
    out(f"Dry run: {len(items)} items in {len(batches)} batches; no API calls. "
        f"Instructions about {instruction_tokens} tokens, cached after the first batch.")
    input_tokens = 0
    for number, batch in enumerate(batches, 1):
        source = sources[batch[0]["source_id"]]
        tokens = estimate_tokens(user_message(template, source, batch, today))
        input_tokens += tokens
        out(f"  batch {number:>2}  {source['id']:<20} {len(batch):>3} items  ~{tokens:>6} tokens")
    output_tokens = len(items) * ESTIMATED_OUTPUT_PER_ITEM
    cache_write, cache_read = instruction_tokens, instruction_tokens * (len(batches) - 1)
    out(f"Estimate: ~{input_tokens} input tokens for the items, {cache_write} cache write, "
        f"{cache_read} cache read, ~{output_tokens} output tokens "
        f"(at {ESTIMATED_OUTPUT_PER_ITEM} per item). "
        f"Estimated cost ${cost(input_tokens, output_tokens, cache_read, cache_write):.4f}")
