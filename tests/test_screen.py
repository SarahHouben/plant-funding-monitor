"""Tests for screen.py and monitor/screening.py, with a fake API client: no network, no spending.

Run from the repo root, after installing requirements.txt:
    .venv/bin/python tests/test_screen.py
"""

import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from monitor import screening  # noqa: E402
from monitor.screening import (BATCH_ITEMS, MODEL, dry_run, load_api_key, load_prompt,  # noqa: E402
                               make_batches, screen)

NOW = datetime(2026, 11, 7, 8, 0, tzinfo=timezone.utc)
SOURCE_COLUMNS = ["id", "funder", "programme", "url", "access_method", "content_selector",
                  "scope", "check_frequency", "status", "notes"]
FAKE_KEY = "sk-ant-test-not-a-real-key-123"


def check(label, ok):
    if not ok:
        print(f"FAIL {label}")
        sys.exit(1)
    print(f"PASS {label}")


def make_db(work, run_started="2026-11-07T06:00:00Z"):
    """A database with two sources and one fetch run, made by init_db.py."""
    for name in ["schema.sql", "init_db.py", "tags.csv"]:
        shutil.copy(REPO / name, work / name)
    with (work / "sources.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, SOURCE_COLUMNS)
        writer.writeheader()
        for source_id in ["agency", "funder"]:
            writer.writerow({"id": source_id, "funder": f"Funder {source_id}", "programme": "Calls",
                             "url": f"https://{source_id}.test/calls", "access_method": "html",
                             "content_selector": "", "scope": "national", "check_frequency": "daily",
                             "status": "active", "notes": ""})
    subprocess.run([sys.executable, str(work / "init_db.py"), "--db", "test.db"],
                   check=True, capture_output=True)
    conn = sqlite3.connect(work / "test.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.execute("INSERT INTO runs (started_at) VALUES (?)", (run_started,))
    return conn


def add_item(conn, source_id, title, text, change="new", url=None):
    """An item_changes row, as the fetch step leaves it."""
    url = url or f"https://{source_id}.test/calls"
    with conn:
        return conn.execute(
            "INSERT INTO item_changes (run_id, source_id, item_key, url, title, change, text, detected_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, '2026-11-07T06:00:00Z')",
            (source_id, f"{url}#{title}", url, title, change, text)).lastrowid


def reply(results=None, text=None, stop_reason="end_turn", usage=(1000, 200, 0, 0)):
    """A response like the SDK's: a thinking block, then the JSON text."""
    body = text if text is not None else json.dumps({"results": results})
    input_tokens, output_tokens, cache_read, cache_write = usage
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=body)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                              cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write))


def items_in(request):
    """The items a request sends, read back from its user message."""
    message = request["messages"][0]["content"]
    return json.loads(message.split("<items>\n", 1)[1].split("\n</items>", 1)[0])


def answer_each(category="not_a_call"):
    """An answer giving every item in the request one result with its own URL."""
    def answer(request):
        return reply([{"item_id": i["item_id"], "url": i["url"], "title": i["title"], "category": category,
                       "reason": "Test reason."} for i in items_in(request)])
    return answer


class FakeClient:
    """Stands in for anthropic.Anthropic. Answers are given in order: a response,
    a function of the request, or an exception to raise. Every request is kept."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []
        self.messages = self

    def create(self, **request):
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer(request) if callable(answer) else answer


def run_screen(conn, client, **options):
    lines = []
    run_id = screen(conn, client, clock=lambda: NOW, out=lines.append, **options)
    return run_id, "\n".join(lines)


def test_prompt_and_batches():
    instructions, template = load_prompt()
    check("the prompt splits into cached instructions and a per-request input template",
          "## Categories" in instructions and "{{ITEMS}}" not in instructions
          and "{{ITEMS}}" in template and "{{TODAY}}" in template and "## Categories" not in template)
    items = ([{"id": n, "source_id": "a", "text": "x" * 100} for n in range(45)]
             + [{"id": 100 + n, "source_id": "b", "text": "x" * 100} for n in range(3)])
    check("batches hold one source each and at most 20 items",
          [(b[0]["source_id"], len(b)) for b in make_batches(items)]
          == [("a", BATCH_ITEMS), ("a", BATCH_ITEMS), ("a", 5), ("b", 3)])
    big = [{"id": n, "source_id": "a", "text": "x" * 60_000} for n in range(3)]
    check("batches also stay under the token budget", [len(b) for b in make_batches(big)] == [1, 1, 1])


def test_screening():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp))) as conn:
        listing = add_item(conn, "agency", "Offene Bekanntmachungen",
                           "## Offene Bekanntmachungen\n\n- [Getreidezüchtung](https://agency.test/getreide)\n"
                           "- [Ladeinfrastruktur](https://agency.test/laden)")
        news = add_item(conn, "agency", "Neuigkeiten", "## Neuigkeiten\n\nUnser Jahresbericht ist erschienen.")
        removed = add_item(conn, "agency", "Alter Aufruf", "No longer listed: Alter Aufruf", change="removed")
        call = add_item(conn, "funder", "Call B", "Call B: crop data\n\nFunding for crop data infrastructure.",
                        url="https://funder.test/calls/b")
        dry = []
        dry_run(conn, clock=lambda: NOW, out=dry.append)
        check("--dry-run shows the batches and a cost estimate, without a client to call",
              "2 batches" in dry[0] and any("agency" in line and "2 items" in line for line in dry)
              and "Estimated cost $" in dry[-1])

        client = FakeClient(
            reply([{"item_id": str(listing), "url": "https://agency.test/getreide", "title": "Getreidezüchtung",
                    "category": "call", "reason": "Breeding cereals."},
                   {"item_id": str(listing), "url": "https://agency.test/laden", "title": "Ladeinfrastruktur",
                    "category": "not_relevant", "reason": "Charging stations."},
                   {"item_id": str(news), "url": "https://agency.test/calls", "title": "Neuigkeiten",
                    "category": "not_a_call", "reason": "An annual report."}],
                  usage=(900, 150, 0, 2000)),
            reply([{"item_id": str(call), "url": "https://funder.test/calls/b", "title": "Call B",
                    "category": "call", "reason": "Crop data infrastructure."}],
                  usage=(500, 80, 2000, 0)))
        run_id, out = run_screen(conn, client)

        first = client.requests[0]
        check("requests use Claude Haiku 5.5 at low effort with the JSON schema",
              first["model"] == MODEL == "claude-haiku-5-5" and first["output_config"]["effort"] == "low"
              and first["output_config"]["format"]["type"] == "json_schema")
        check("the fixed instructions are the system prompt, marked for caching",
              first["system"][0]["text"] == load_prompt()[0]
              and first["system"][0]["cache_control"] == {"type": "ephemeral"})
        check("each request holds one source's items, with ids, texts and today's date",
              [i["item_id"] for i in items_in(first)] == [str(listing), str(news)]
              and "Funder: Funder agency" in first["messages"][0]["content"]
              and "<today>2026-11-07</today>" in first["messages"][0]["content"]
              and [i["item_id"] for i in items_in(client.requests[1])] == [str(call)])
        check("removed items are not screened",
              all(str(removed) not in [i["item_id"] for i in items_in(r)] for r in client.requests)
              and conn.execute("SELECT text IS NOT NULL AND screened_at IS NULL FROM item_changes WHERE id = ?",
                               (removed,)).fetchone()[0] == 1)

        candidates = conn.execute("SELECT * FROM candidates ORDER BY id").fetchall()
        check("each call becomes a candidate with its own link and the item's text for extraction",
              [(c["url"], c["item_change_id"]) for c in candidates]
              == [("https://agency.test/getreide", listing), ("https://funder.test/calls/b", call)]
              and candidates[0]["text"].startswith("## Offene Bekanntmachungen")
              and candidates[0]["run_id"] == run_id and candidates[0]["extracted_at"] is None)
        log = conn.execute("SELECT stage, category, url FROM screening_log ORDER BY id").fetchall()
        check("dropped results go to the screening log, one per call, stage 'screening'",
              [tuple(row) for row in log] == [("screening", "not_relevant", "https://agency.test/laden"),
                                              ("screening", "not_a_call", "https://agency.test/calls")])
        cleared = conn.execute("SELECT count(*) FROM item_changes WHERE change != 'removed' "
                               "AND text IS NULL AND screened_at = '2026-11-07T08:00:00Z'").fetchone()[0]
        check("screened items lose their text and get screened_at", cleared == 3)
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        check("tokens are added to the fetch run that started within 6 hours",
              run_id == 1 and (run["screening_input_tokens"], run["screening_output_tokens"],
                               run["screening_cache_read_tokens"], run["screening_cache_write_tokens"])
              == (1400, 230, 2000, 2000))
        check("the summary counts results per source and estimates the cost",
              "agency               1 / 1 / 1" in out and "funder               1 / 0 / 0" in out
              and "Estimated cost $" in out)

        again = add_item(conn, "funder", "Call B again", "Call B is also listed here: https://agency.test/getreide")
        run_screen(conn, FakeClient(reply([{"item_id": str(again), "url": "https://agency.test/getreide",
                                            "title": "Getreidezüchtung", "category": "call",
                                            "reason": "Same call."}])))
        check("a call already waiting for extraction is not queued twice",
              conn.execute("SELECT count(*) FROM candidates WHERE url = 'https://agency.test/getreide'"
                           ).fetchone()[0] == 1)

        page = add_item(conn, "funder", "Programmes A to Z", "## Programme A\n\nFor postdocs.\n\n"
                        "## Programme B\n\nFor professors.")
        run_screen(conn, FakeClient(reply([
            {"item_id": str(page), "url": "https://funder.test/calls", "title": title, "category": "call",
             "reason": "A programme without a link of its own."} for title in ["Programme A", "Programme B"]])))
        check("calls without links of their own share the page URL but stay separate candidates",
              conn.execute("SELECT count(*) FROM candidates WHERE url = 'https://funder.test/calls'"
                           ).fetchone()[0] == 2)


def test_retries():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp))) as conn:
        good = add_item(conn, "agency", "Good", "A call: [Call](https://agency.test/good)")
        bad = add_item(conn, "agency", "Bad", "Another call: [Call](https://agency.test/bad)")
        client = FakeClient(
            reply([{"item_id": str(good), "url": "https://agency.test/good", "title": "Good",
                    "category": "call", "reason": "Fine."},
                   {"item_id": str(bad), "url": "https://agency.test/made-up", "title": "Bad",
                    "category": "call", "reason": "Invented link."}]),
            answer_each("call"))
        run_screen(conn, client)
        retry = client.requests[1]
        check("an item whose URL isn't in its text is sent again on its own, with the problem named",
              [i["item_id"] for i in items_in(retry)] == [str(bad)]
              and "<note>" in retry["messages"][0]["content"]
              and "https://agency.test/made-up" in retry["messages"][0]["content"])
        check("after the retry both items are screened",
              conn.execute("SELECT count(*) FROM item_changes WHERE screened_at IS NOT NULL").fetchone()[0] == 2)

        broken = add_item(conn, "agency", "Broken JSON", "Text")
        missing = add_item(conn, "agency", "Missing", "Text")
        client = FakeClient(reply(text="{not json"), answer_each())
        run_screen(conn, client)
        check("an answer that isn't JSON is retried for all its items",
              [i["item_id"] for i in items_in(client.requests[1])] == [str(broken), str(missing)]
              and "not valid JSON" in client.requests[1]["messages"][0]["content"])

        lost = add_item(conn, "agency", "Lost", "Text")
        kept = add_item(conn, "agency", "Kept", "Text")
        client = FakeClient(reply([{"item_id": str(kept), "url": "https://agency.test/calls", "title": "Kept",
                                    "category": "not_a_call", "reason": "News."},
                                   {"item_id": "999", "url": "https://agency.test/calls", "title": "?",
                                    "category": "call", "reason": "Unknown id."}]),
                            answer_each())
        run_screen(conn, client)
        check("an item missing from the answer is retried; results for unknown ids are ignored",
              [i["item_id"] for i in items_in(client.requests[1])] == [str(lost)]
              and "no result for this item" in client.requests[1]["messages"][0]["content"]
              and conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 2)

        refused = add_item(conn, "agency", "Refused", "Text")
        run_id, out = run_screen(conn, FakeClient(reply([], stop_reason="refusal")))
        row = conn.execute("SELECT text, screened_at FROM item_changes WHERE id = ?", (refused,)).fetchone()
        details = json.loads(conn.execute("SELECT error_details FROM runs WHERE id = ?", (run_id,)).fetchone()[0])
        check("an item that fails twice stays unscreened, with the error recorded in the run",
              row["text"] == "Text" and row["screened_at"] is None
              and any(d.get("item_change_id") == refused and "refusal" in d["message"] for d in details)
              and "stay unscreened" in out)


def test_api_errors_and_runs():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp), "2026-11-06T20:00:00Z")) as conn:
        first = add_item(conn, "agency", "First", "Text")
        second = add_item(conn, "funder", "Second", "Text")
        client = FakeClient(anthropic.APIConnectionError(request=request), answer_each())
        run_id, out = run_screen(conn, client)
        screened = {row["id"]: row["screened_at"] for row in conn.execute("SELECT id, screened_at FROM item_changes")}
        check("a connection error leaves that batch unscreened and the run carries on",
              screened[first] is None and screened[second] is not None and "APIConnectionError" in out)
        check("a fetch run that started over 6 hours ago gets a new run for screening",
              run_id == 2 and conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 2)

        denied = anthropic.AuthenticationError("invalid x-api-key", body=None,
                                               response=httpx2.Response(401, request=request))
        try:
            run_screen(conn, FakeClient(denied))
            stopped = False
        except screening.StopScreening:
            stopped = True
        run = conn.execute("SELECT finished_at, error_details FROM runs WHERE id = 2").fetchone()
        check("an authentication error stops screening, and the run still records it",
              stopped and run["finished_at"] is not None and "AuthenticationError" in run["error_details"])


def test_api_key():
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(f'# local settings\nOTHER=1\nANTHROPIC_API_KEY="{FAKE_KEY}"\n', encoding="utf-8")
            check("the API key is read from .env", load_api_key(env_file) == FAKE_KEY)
            env_file.write_text(f"export ANTHROPIC_API_KEY={FAKE_KEY}  # comment\n", encoding="utf-8")
            check("an export prefix and a trailing comment are handled", load_api_key(env_file) == FAKE_KEY)
            os.environ["ANTHROPIC_API_KEY"] = "sk-ant-from-environment"
            check("the environment variable takes precedence over .env",
                  load_api_key(env_file) == "sk-ant-from-environment")
            del os.environ["ANTHROPIC_API_KEY"]
            check("no key in the environment or a .env gives None", load_api_key(Path(tmp) / "missing") is None)

            # The real screen.py, with a made-up key and an API address where nothing listens.
            conn = make_db(Path(tmp))
            add_item(conn, "agency", "Item", "Text")
            conn.close()
            env = {**os.environ, "ANTHROPIC_API_KEY": FAKE_KEY, "ANTHROPIC_BASE_URL": "http://127.0.0.1:9"}
            result = subprocess.run([sys.executable, str(REPO / "screen.py"), "--db", str(Path(tmp) / "test.db")],
                                    capture_output=True, text=True, env=env, timeout=120)
            check("screen.py never prints the API key, even when the API can't be reached",
                  "APIConnectionError" in result.stdout and FAKE_KEY not in result.stdout + result.stderr)
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved


def main():
    test_prompt_and_batches()
    test_screening()
    test_retries()
    test_api_errors_and_runs()
    test_api_key()
    print("All checks passed.")


if __name__ == "__main__":
    main()
