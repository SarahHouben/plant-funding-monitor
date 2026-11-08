"""Tests for extract.py, monitor/extraction.py and monitor/calltext.py, with a fake
API client and saved pages: no network, no spending.

Run from the repo root, after installing requirements.txt:
    .venv/bin/python tests/test_extract.py
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
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from monitor import calltext  # noqa: E402
from monitor.calltext import CallTexts, cap, pdf_text, thin_source  # noqa: E402
from monitor.extraction import (FALLBACK_BETA, StopExtraction, check_answer, dry_run, extract,  # noqa: E402
                                load_prompt, load_tags, next_deadline, output_schema, usage_cost,
                                with_scheme)
from test_fetch import EU_URL, FakeWeb, fixture  # noqa: E402

NOW = datetime(2026, 11, 8, 8, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
SOURCE_COLUMNS = ["id", "funder", "programme", "url", "access_method", "content_selector",
                  "scope", "family", "check_frequency", "status", "notes"]
CALL_URL = "https://agency.test/aufrufe/saatgut"
CALL_PAGE = """<html><body><header><nav><a href="/">Start</a></nav></header><main>
<h1>Gesundes Saatgut (Beispiel)</h1>
<p>Gefördert werden Verbundvorhaben zur Saatgutgesundheit von Gerste. Die Zuwendung beträgt bis zu
500.000 Euro für drei Jahre. Antragsberechtigt sind Hochschulen und außeruniversitäre
Forschungseinrichtungen mit Sitz in Deutschland.</p>
<p>Skizzen bis 1. März 2027, 12 Uhr, über easy-Online.</p>
<p>Anträge über foerderportal.bund.de/easyonline, Fragen und Antworten unter www.agency.test/faq.</p>
<p><a href="/files/richtlinie.pdf">Förderrichtlinie (PDF)</a></p>
<p>Kontakt: Beispielagentur, foerderung@agency.test. Ansprechpartnerin: Dr. Erika Musterfrau,
Tel. 0228 123456, erika.musterfrau@agency.test</p>
<p>Die Förderung soll dazu beitragen, Saatgut mit hoher Keimfähigkeit und geringer Belastung durch
samenbürtige Krankheitserreger bereitzustellen. Erwartet werden Vorhaben, die Diagnostik, Züchtung und
Saatgutbehandlung verbinden und deren Ergebnisse in der Praxis erprobt werden.</p>
</main></body></html>"""
FAKE_KEY = "sk-ant-test-not-a-real-key-456"


def check(label, ok):
    if not ok:
        print(f"FAIL {label}")
        sys.exit(1)
    print(f"PASS {label}")


def tiny_pdf(lines):
    """A one-page PDF with some lines of text, built by hand."""
    stream = "\n".join(f"BT /F1 10 Tf 50 {760 - 14 * n} Td ({line}) Tj ET"
                       for n, line in enumerate(lines)).encode("latin-1")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    pdf, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    pdf += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return pdf


def make_db(work):
    for name in ["schema.sql", "init_db.py", "tags.csv"]:
        shutil.copy(REPO / name, work / name)
    rows = [("agency", "https://agency.test/calls", "html"), ("eu", EU_URL, "api")]
    with (work / "sources.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, SOURCE_COLUMNS)
        writer.writeheader()
        for source_id, url, method in rows:
            writer.writerow({"id": source_id, "funder": f"Funder {source_id}", "programme": "Calls", "url": url,
                             "access_method": method, "content_selector": "", "scope": "national",
                             "check_frequency": "daily", "status": "active", "notes": ""})
    subprocess.run([sys.executable, str(work / "init_db.py"), "--db", "test.db"], check=True, capture_output=True)
    conn = sqlite3.connect(work / "test.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.execute("INSERT INTO runs (started_at) VALUES ('2026-11-08T06:00:00Z')")
    return conn


def add_candidate(conn, url=CALL_URL, title="Gesundes Saatgut (Beispiel)", source_id="agency",
                  text="## Aufrufe\n\n- Gesundes Saatgut (Beispiel), Skizzen bis 1. März 2027", change="new"):
    with conn:
        change = conn.execute("INSERT INTO item_changes (run_id, source_id, item_key, url, title, change, "
                              "detected_at, screened_at) VALUES (1, ?, ?, ?, ?, ?, "
                              "'2026-11-08T06:00:00Z', '2026-11-08T06:30:00Z')",
                              (source_id, f"{url}#{title}", url, title, change)).lastrowid
        return conn.execute("INSERT INTO candidates (run_id, item_change_id, source_id, url, title, reason, text, "
                            "created_at) VALUES (1, ?, ?, ?, ?, 'A call.', ?, '2026-11-08T06:30:00Z')",
                            (change, source_id, url, title, text)).lastrowid


def call_answer(**changes):
    call = {
        "title": "Gesundes Saatgut (Beispiel)", "title_en": "Healthy seed (example)",
        "call_type": "deadline_bound", "status": "open", "opening_date": None,
        "deadlines": [{"stage": "sketch", "date": "2027-03-01", "annual_date": None, "note": "12:00"}],
        "summary_en": "Joint projects on seed health in barley.", "summary_de": "Verbundvorhaben zur Saatgutgesundheit.",
        "eligible_countries": ["DE"], "eligible_institution_types": ["university", "research_institute"],
        "eligibility_conditions": None, "ipk_eligible": "yes",
        "ipk_eligible_reason": "Non-university research institutes in Germany can apply.",
        "funding_amount": "up to EUR 500,000", "funding_duration": "3 years", "apply_via": "easy-Online",
        "apply_url": None,
        "more_info_links": [{"label": "Funding guideline", "url": "https://agency.test/files/richtlinie.pdf"}],
        "contact_point": "Beispielagentur, foerderung@agency.test", "relevance_score": 4,
        "tags": {"research_area": ["breeding", "seeds-yield"], "organism": "barley",
                 "funding_type": "collaborative-project", "career_stage": "any-stage"},
        "evidence": [{"field": "deadlines", "quote": "Skizzen bis 1. März 2027, 12 Uhr"},
                     {"field": "funding_amount", "quote": "Die Zuwendung beträgt bis zu 500.000 Euro"}],
    }
    call.update(changes)
    return {"not_a_call": False, "reason": None, "call": call}


def reply(answer=None, text=None, stop_reason="end_turn", usage=(3000, 900, 0, 4000, 200)):
    input_tokens, output_tokens, cache_read, cache_write, thinking = usage
    return SimpleNamespace(
        stop_reason=stop_reason, model="claude-sonnet-5-5",
        content=[SimpleNamespace(type="thinking", thinking=""),
                 SimpleNamespace(type="text", text=text if text is not None else json.dumps(answer))],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                              cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write,
                              output_tokens_details=SimpleNamespace(thinking_tokens=thinking)))


class FakeClient:
    """Stands in for anthropic.Anthropic: the standard and beta message endpoints
    and Message Batches. Answers come in order (the last one repeats); an
    exception is raised. Every request is kept, with the endpoint it went to."""

    def __init__(self, *answers):
        self.answers, self.requests, self.batches = list(answers), [], []
        self.messages = SimpleNamespace(create=self._standard, batches=SimpleNamespace(
            create=self._batch_create, retrieve=self._batch_retrieve, results=self._batch_results))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._beta))

    def _next(self):
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _standard(self, **request):
        self.requests.append(("standard", request))
        return self._next()

    def _beta(self, **request):
        self.requests.append(("beta", request))
        return self._next()

    def _batch_create(self, requests):
        self.batches.append({"requests": requests, "polls": 0})
        return SimpleNamespace(id=f"batch_{len(self.batches)}")

    def _batch_retrieve(self, batch_id):
        batch = self.batches[int(batch_id.split("_")[1]) - 1]
        batch["polls"] += 1
        return SimpleNamespace(processing_status="ended" if batch["polls"] > 1 else "in_progress")

    def _batch_results(self, batch_id):
        batch = self.batches[int(batch_id.split("_")[1]) - 1]
        return [SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(type="succeeded", message=self._next()))
                for r in batch["requests"]]


def web_with_call():
    web = FakeWeb()
    web.set(CALL_URL, CALL_PAGE)
    return web


def run_extract(conn, client, web=None, **options):
    lines = []
    tally = extract(conn, client, (web or web_with_call()).fetcher(), clock=lambda: NOW,
                    sleep=lambda seconds: None, out=lines.append, **options)
    return tally, "\n".join(lines)


def test_prompt_and_schema():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp))) as conn:
        with conn:
            conn.execute("UPDATE tags SET retired = 1 WHERE tag_id = 'rye'")
        tags = load_tags(conn)
        instructions, template = load_prompt(tags)
        check("the cached instructions hold the tag list and the EU note; the input template the call text",
              "barley | organism | 1 | Barley" in instructions and "{{TAGS}}" not in instructions
              and "associated countries" in instructions and "{{CALL_TEXT}}" in template
              and "{{CALL_TITLE}}" in template and "barley |" not in template)
        props = output_schema(tags)["properties"]["call"]["anyOf"][0]["properties"]
        check("the schema allows only tag_ids of each group: a list for research areas, one value otherwise",
              props["tags"]["properties"]["research_area"]["type"] == "array"
              and "breeding" in props["tags"]["properties"]["research_area"]["items"]["enum"]
              and props["tags"]["properties"]["organism"]["enum"][:3] == ["barley", "wheat", "other-cereals"]
              and "rye" not in props["tags"]["properties"]["organism"]["enum"])
        check("the schema limits call_type, status, ipk_eligible, institution types and relevance",
              props["call_type"]["enum"] == ["deadline_bound", "recurring", "always_open"]
              and "closed" in props["status"]["enum"] and props["ipk_eligible"]["enum"] == ["yes", "no", "unclear"]
              and "research_institute" in props["eligible_institution_types"]["items"]["enum"]
              and props["relevance_score"]["enum"] == [1, 2, 3, 4, 5])


def test_call_texts():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp))) as conn:
        sources = {row["id"]: row for row in conn.execute("SELECT * FROM sources")}
        web = web_with_call()
        pdf_lines = ["Bekanntmachung Gesundes Saatgut (Beispiel)"] + [
            f"Absatz {n}: Gefoerdert werden Vorhaben zur Saatgutgesundheit von Gerste." for n in range(12)]
        web.set("https://agency.test/files/aufruf.pdf", tiny_pdf(pdf_lines), content_type="application/pdf")
        web.set(EU_URL + "&pageNumber=1", fixture("eu_page1.json"), content_type="application/json")
        web.set(EU_URL + "&pageNumber=2", fixture("eu_page2.json"), content_type="application/json")
        web.set("https://agency.test/aufrufe/js", "<html><body><main><p>Loading…</p></main></body></html>")
        texts = CallTexts(web.fetcher())

        def get(url, source="agency", stored="Stored section text."):
            row = {"url": url, "title": "T", "text": stored, "source_id": source}
            return texts.get(row, sources[source])

        text, origin = get(CALL_URL)
        check("a call page becomes Markdown with absolute links, plus the listing as context",
              origin == "page" and "Skizzen bis 1. März 2027" in text
              and "(https://agency.test/files/richtlinie.pdf)" in text
              and text.endswith("Where the call was listed on the funder's site:\n\nStored section text."))
        check("personal contact details are scrubbed from call texts",
              "Musterfrau" not in text and "0228 123456" not in text and "foerderung@agency.test" in text)
        text, origin = get("https://agency.test/files/aufruf.pdf")
        check("a PDF's text is extracted", origin == "PDF" and "Bekanntmachung Gesundes Saatgut" in text
              and "Absatz 11" in text)
        topic = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/"
        first, origin = get(topic + "HORIZON-INFRA-2027-01-EXAMPLE-02", source="eu")
        get(topic + "ERC-2027-STG-EXAMPLE", source="eu")
        check("EU topics come from the search API, read once for all of them",
              origin == "EU topic details" and "Topic: HORIZON-INFRA-2027-01-EXAMPLE-02" in first
              and "Deadlines: 2027-04-22, 2027-09-16" in first and "open phenotyping facilities" in first
              and sum(r["method"] == "POST" for r in web.requests) == 2)
        since = len(web.requests)
        text, origin = get("https://agency.test/calls", stored="The section that lists the call.")
        check("a call without a link of its own uses the stored section text, fetching nothing",
              text == "The section that lists the call." and "no link of its own" in origin
              and len(web.requests) == since)
        text, origin = get("https://agency.test/missing")
        check("a page that can't be fetched falls back to the stored text",
              text == "Stored section text." and "fetching failed" in origin)
        text, origin = get("https://agency.test/aufrufe/js")
        check("a page with almost no text (JavaScript) falls back to the stored text",
              text == "Stored section text." and "had only" in origin)
        long_text = "A" * 50_000 + "MIDDLE" + "Z" * 20_000
        check("long texts keep their start and end, with the gap marked",
              len(cap(long_text)) < 61_000 and cap(long_text).startswith("A" * 100)
              and cap(long_text).endswith("Z" * 100) and "text shortened" in cap(long_text)
              and "MIDDLE" not in cap(long_text))
        check("pdf_text reads the hand-built PDF", "Absatz 0" in pdf_text(tiny_pdf(pdf_lines)))
        hyphenated = pdf_text(tiny_pdf(["bis zu drei aufeinander fol-", "genden Foerderphasen", "Pflanzen-",
                                        "und Tierzucht"]))
        check("words hyphenated at a PDF line break are joined, except before 'und'",
              "aufeinander folgenden Foerderphasen" in hyphenated and "Pflanzen-\nund Tierzucht" in hyphenated)
        check("EU topic texts include the topic page, so a link to it can be checked",
              f"): {topic}HORIZON-INFRA-2027-01-EXAMPLE-02" in first)
        listed = "x" * 2000
        check("thin_source: stored text standing in for the call's page is thin, however long",
              "fetching failed" in thin_source(listed, "stored text (fetching failed: HTTP 404)"))
        check("thin_source: a short call text is thin, not counting the listing added for context",
              "has only 1000 characters" in thin_source("y" * 1000 + calltext.LISTING_HEADER + listed, "page"))
        check("thin_source: a long text, or the long section of a call without a link of its own, is not",
              thin_source("y" * 1500, "PDF") is None and thin_source(listed, calltext.NO_OWN_LINK) is None)


def test_checks_and_deadlines():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp))) as conn:
        tags = load_tags(conn)
    text = calltext.html_to_markdown(CALL_PAGE, CALL_URL)

    def problems(**changes):
        return check_answer(call_answer(**changes), text, tags, TODAY)

    def checks(**changes):
        return {name for name, _ in problems(**changes)}

    check("a correct answer passes every check", problems() == [])
    check("invalid dates fail", checks(opening_date="2026-02-30") == {"dates"}
          and checks(deadlines=[{"stage": "a", "date": "01.03.2027", "annual_date": None, "note": None}]) == {"dates"}
          and checks(deadlines=[{"stage": "a", "date": None, "annual_date": "13-01", "note": None}]) == {"dates"})
    check("each stage needs exactly one of date and annual_date",
          checks(deadlines=[{"stage": "a", "date": None, "annual_date": None, "note": None}]) == {"dates"}
          and checks(deadlines=[{"stage": "a", "date": "2027-03-01", "annual_date": "03-01", "note": None}]) == {"dates"})
    check("an open call whose dated deadlines have all passed fails the status check",
          checks(deadlines=[{"stage": "a", "date": "2026-10-01", "annual_date": None, "note": None}]) == {"status"}
          and checks(status="closed", deadlines=[{"stage": "a", "date": "2026-10-01", "annual_date": None,
                                                  "note": None}]) == set()
          and checks(call_type="recurring", deadlines=[{"stage": "a", "date": None, "annual_date": "03-01",
                                                        "note": None}]) == set())
    check("tag counts must match per_call",
          checks(tags={"research_area": [], "organism": "barley", "funding_type": "prize",
                       "career_stage": "any-stage"}) == {"tags"}
          and checks(tags={"research_area": ["breeding", "phenotyping", "domestication", "genome-editing"],
                           "organism": "barley", "funding_type": "prize", "career_stage": "any-stage"}) == {"tags"})
    check("evidence must be verbatim in the call text, though whitespace may differ",
          checks(evidence=[{"field": "deadlines", "quote": "Skizzen bis Ende März"}]) == {"evidence"}
          and checks(evidence=[{"field": "deadlines", "quote": "Skizzen  bis 1. März\n2027,   12 Uhr"}]) == set())
    check("apply_url and more_info_links must appear in the call text",
          checks(apply_url="https://agency.test/apply") == {"links"}
          and checks(more_info_links=[{"label": "FAQ", "url": "https://agency.test/faq"}]) == {"links"})
    check("a not_a_call answer needs a reason",
          check_answer({"not_a_call": True, "reason": "", "call": None}, text, tags, TODAY) != []
          and check_answer({"not_a_call": True, "reason": "A news item.", "call": None}, text, tags, TODAY) == [])

    check("links without a scheme get https://; relative ones are resolved against the call's URL",
          with_scheme("www.ble.de", CALL_URL) == "https://www.ble.de"
          and with_scheme("foerderportal.bund.de/easyonline", CALL_URL) == "https://foerderportal.bund.de/easyonline"
          and with_scheme("www.ble.de:8080/x", CALL_URL) == "https://www.ble.de:8080/x"
          and with_scheme("//cdn.agency.test/a.pdf", CALL_URL) == "https://cdn.agency.test/a.pdf"
          and with_scheme("/files/a.pdf", CALL_URL) == "https://agency.test/files/a.pdf"
          and with_scheme("http://agency.test/x", CALL_URL) == "http://agency.test/x"
          and with_scheme("mailto:grants@agency.test", CALL_URL) == "mailto:grants@agency.test")

    stages = [{"stage": "a", "date": "2026-11-01"}, {"stage": "b", "date": "2027-01-14"}]
    check("next_deadline is the earliest stage date from today on", next_deadline(stages, TODAY) == "2027-01-14")
    check("next_deadline is None when every stage has passed",
          next_deadline([{"stage": "a", "date": "2026-11-01"}], TODAY) is None and next_deadline([], TODAY) is None)
    check("recurring calls get the next occurrence of their annual date",
          next_deadline([{"stage": "a", "annual_date": "03-01"}, {"stage": "b", "annual_date": "08-01"}], TODAY)
          == "2027-03-01"
          and next_deadline([{"stage": "a", "annual_date": "12-01"}], TODAY) == "2026-12-01"
          and next_deadline([{"stage": "a", "annual_date": "11-08"}], TODAY) == "2026-11-08"
          and next_deadline([{"stage": "a", "annual_date": "02-29"}], TODAY) == "2028-02-29"
          and next_deadline([{"stage": "a", "annual_date": "02-29"}], date(2027, 6, 1)) == "2028-02-29")


def test_extraction():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp))) as conn:
        first = add_candidate(conn)
        links = [{"label": "Funding guideline", "url": "https://agency.test/files/richtlinie.pdf"},
                 {"label": "FAQ", "url": "www.agency.test/faq"}]
        client = FakeClient(reply(call_answer(apply_url="foerderportal.bund.de/easyonline", more_info_links=links)))
        tally, out = run_extract(conn, client)
        endpoint, request = client.requests[0]
        check("Sonnet 5.5 requests go to the beta endpoint with the server-side refusal fallback",
              endpoint == "beta" and request["fallbacks"] == "default" and request["betas"] == [FALLBACK_BETA]
              and request["model"] == "claude-sonnet-5-5")
        check("requests cache the instructions and use low effort with the JSON schema",
              request["system"][0]["cache_control"] == {"type": "ephemeral"}
              and "barley | organism" in request["system"][0]["text"]
              and request["output_config"]["effort"] == "low"
              and request["output_config"]["format"]["type"] == "json_schema")
        message = request["messages"][0]["content"]
        check("the user message holds the title, URL, today's date and the call text",
              "<call_title>Gesundes Saatgut (Beispiel)</call_title>" in message and CALL_URL in message
              and "<today>2026-11-08</today>" in message and "Skizzen bis 1. März 2027" in message)
        row = conn.execute("SELECT * FROM calls").fetchone()
        check("a valid answer becomes a calls row with next_deadline, deadlines and evidence",
              row["original_url"] == CALL_URL and row["next_deadline"] == "2027-03-01"
              and json.loads(row["deadlines"]) == [{"stage": "sketch", "date": "2027-03-01", "note": "12:00"}]
              and json.loads(row["evidence"])["deadlines"] == ["Skizzen bis 1. März 2027, 12 Uhr"]
              and row["ipk_eligible"] == "yes" and row["review_status"] == "new" and row["source_id"] == "agency")
        check("links written without a scheme pass the checks as written and are stored with https://",
              row["apply_url"] == "https://foerderportal.bund.de/easyonline"
              and [link["url"] for link in json.loads(row["more_info_links"])]
              == ["https://agency.test/files/richtlinie.pdf", "https://www.agency.test/faq"])
        check("its tags go to call_tags",
              {r[0] for r in conn.execute("SELECT tag_id FROM call_tags")}
              == {"breeding", "seeds-yield", "barley", "collaborative-project", "any-stage"})
        candidate = conn.execute("SELECT * FROM candidates WHERE id = ?", (first,)).fetchone()
        check("the candidate's text is cleared and extracted_at filled",
              candidate["text"] is None and candidate["extracted_at"] == "2026-11-08T08:00:00Z")
        run = conn.execute("SELECT * FROM runs WHERE id = 1").fetchone()
        check("tokens, cache and thinking are added to the run that started within 6 hours",
              (run["extraction_input_tokens"], run["extraction_output_tokens"], run["extraction_cache_write_tokens"],
               run["extraction_thinking_tokens"]) == (3000, 900, 4000, 200)
              and abs(tally.cost - (3000 * 2 + 900 * 10 + 4000 * 2.5) / 1e6) < 1e-9
              and "200 thinking" in out)

        again = add_candidate(conn, title="Gesundes Saatgut, neue Fassung", change="changed")
        news = add_candidate(conn, url="https://agency.test/aufrufe/news", title="Jahresbericht")
        web = web_with_call()
        web.set("https://agency.test/aufrufe/news", CALL_PAGE.replace("Gesundes Saatgut", "Jahresbericht"))
        run_extract(conn, FakeClient(reply(call_answer(title="Neue Fassung")),
                                     reply({"not_a_call": True, "reason": "An annual report.", "call": None})), web)
        update = conn.execute("SELECT * FROM call_updates").fetchone()
        check("a changed item for a known URL is extracted again into call_updates for step 13",
              conn.execute("SELECT count(*), max(title) FROM calls").fetchone()[:] == (1, "Gesundes Saatgut (Beispiel)")
              and update["candidate_id"] == again and json.loads(update["extracted"])["call"]["title"] == "Neue Fassung")

        elsewhere = add_candidate(conn, url="http://AGENCY.test/aufrufe/saatgut/?nn=4711&utm_source=x",
                                  title="Gesundes Saatgut", source_id="eu")
        client = FakeClient(reply(call_answer()))
        tally, out = run_extract(conn, client)
        listings = conn.execute("SELECT source_id, url FROM call_listings WHERE call_id = 1 ORDER BY id").fetchall()
        check("a new item for a known canonical URL becomes a listing, without fetching or extracting it",
              client.requests == [] and tally.outcomes == {"known URL, listing added": 1}
              and [row[0] for row in listings] == ["agency", "agency", "eu"]
              and listings[2][1] == "http://AGENCY.test/aufrufe/saatgut/?nn=4711&utm_source=x"
              and conn.execute("SELECT extracted_at FROM candidates WHERE id = ?", (elsewhere,)).fetchone()[0])
        log = conn.execute("SELECT stage, category, reason FROM screening_log").fetchone()
        check("a not_a_call answer goes to the screening log with stage 'extraction'",
              tuple(log) == ("extraction", "not_a_call", "An annual report.")
              and conn.execute("SELECT extracted_at FROM candidates WHERE id = ?", (news,)).fetchone()[0] is not None)

        listed = add_candidate(conn, url="https://agency.test/calls", title="Programm ohne eigene Seite",
                               text="## Programm ohne eigene Seite\n\nSkizzen bis 1. März 2027, 12 Uhr. "
                                    "Die Zuwendung beträgt bis zu 500.000 Euro. "
                                    "[Förderrichtlinie](https://agency.test/files/richtlinie.pdf)")
        run_extract(conn, FakeClient(reply(call_answer(title="Programm ohne eigene Seite",
                                                       title_en="Programme without a page of its own"))))
        check("a call without a link of its own gets the listing URL with its title as a fragment",
              conn.execute("SELECT count(*) FROM calls WHERE original_url = ? AND canonical_url = ?",
                           ("https://agency.test/calls#Programm ohne eigene Seite",
                            "https://agency.test/calls#programm ohne eigene seite")).fetchone()[0] == 1
              and conn.execute("SELECT extracted_at FROM candidates WHERE id = ?", (listed,)).fetchone()[0])


def test_thin_source_flags():
    long_page = CALL_PAGE.replace("</main>", "<p>" + " ".join(
        ["Die Vorhaben sollen Diagnostik, Züchtung und Saatgutbehandlung verbinden."] * 25) + "</p></main>")
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp))) as conn:
        add_candidate(conn)
        add_candidate(conn, url="https://agency.test/missing", title="Zweiter Aufruf")
        web = FakeWeb()
        web.set(CALL_URL, long_page)
        second = call_answer(title="Zweiter Aufruf", title_en=None, evidence=[], more_info_links=[])
        run_extract(conn, FakeClient(reply(call_answer(evidence=[], more_info_links=[])), reply(second)), web)
        flags = conn.execute("SELECT c.original_url, f.detail FROM call_flags f JOIN calls c ON c.id = f.call_id "
                             "WHERE f.kind = 'thin_source'").fetchall()
        check("a call extracted from the listing's stored text is flagged thin_source; one from its full page isn't",
              [row[0] for row in flags] == ["https://agency.test/missing"]
              and "stored text (fetching failed" in flags[0][1])

        add_candidate(conn, url="https://agency.test/missing", title="Zweiter Aufruf", change="changed")
        web.set("https://agency.test/missing", long_page)
        run_extract(conn, FakeClient(reply(second)), web)
        check("an update extracted from the full page resolves the thin_source flag",
              conn.execute("SELECT count(*) FROM call_flags WHERE resolved_at IS NULL").fetchone()[0] == 0
              and conn.execute("SELECT count(*) FROM call_updates").fetchone()[0] == 1)


def test_undated_stages():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp))) as conn:
        candidate = add_candidate(conn)
        stages = [{"stage": "sketch", "date": "2027-03-01", "annual_date": None, "note": "12:00"},
                  {"stage": "letter of intent", "date": None, "annual_date": None, "note": "dates updated each cycle"}]
        client = FakeClient(reply(call_answer(deadlines=stages)))
        tally, out = run_extract(conn, client)
        row = conn.execute("SELECT deadlines FROM calls").fetchone()
        notes = json.loads(conn.execute("SELECT notes FROM runs WHERE id = 1").fetchone()[0])
        check("a deadline stage with neither date nor annual date is dropped, without a retry, and the call stored",
              row is not None and [s["stage"] for s in json.loads(row["deadlines"])] == ["sketch"]
              and len(client.requests) == 1 and tally.retried == 0)
        check("the dropped stage is noted in the run, not counted as an error",
              len(notes) == 1 and notes[0]["candidate_id"] == candidate and "'letter of intent'" in notes[0]["message"]
              and conn.execute("SELECT errors FROM runs WHERE id = 1").fetchone()[0] == 0
              and "Note: candidate" in out)


def test_retries_and_failures():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp))) as conn:
        past = [{"stage": "sketch", "date": "2026-10-01", "annual_date": None, "note": None}]
        add_candidate(conn)
        client = FakeClient(reply(call_answer(deadlines=past)), reply(call_answer()))
        tally, out = run_extract(conn, client)
        retry = client.requests[1][1]["messages"][0]["content"]
        check("an answer that fails a check is retried once, with the problem named",
              "<note>" in retry and "status" in retry and "before today" in retry
              and conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 1)
        check("the report counts retries and the checks that failed",
              tally.retried == 1 and tally.first_try_failures == {"status": 1}
              and "1 candidates failed a check on the first try" in out and "status 1" in out)

        bad = add_candidate(conn, url="https://agency.test/aufrufe/zweiter", title="Zweiter Aufruf")
        wrong = call_answer(evidence=[{"field": "deadlines", "quote": "Erfundenes Zitat"}])
        tally, out = run_extract(conn, FakeClient(reply(wrong)))
        candidate = conn.execute("SELECT * FROM candidates WHERE id = ?", (bad,)).fetchone()
        details = json.loads(conn.execute("SELECT error_details FROM runs WHERE id = 1").fetchone()[0])
        check("a candidate that fails twice stores nothing, keeps its text and counts an attempt",
              conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 1
              and candidate["attempts"] == 1 and "evidence" in candidate["last_error"]
              and candidate["text"] is not None and candidate["extracted_at"] is None
              and any(d.get("candidate_id") == bad for d in details) and "FAILED after a retry" in out)
        with conn:
            conn.execute("UPDATE candidates SET attempts = 3 WHERE id = ?", (bad,))
        tally, out = run_extract(conn, FakeClient(reply(call_answer())))
        check("a candidate is skipped after three failed attempts", tally is None and "Nothing to extract" in out)


def test_options_and_errors():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp))) as conn:
        urls = [f"https://agency.test/aufrufe/{n}" for n in range(4)]
        ids = [add_candidate(conn, url=url, title=f"Aufruf {n}") for n, url in enumerate(urls)]
        web = FakeWeb()
        for url in urls:
            web.set(url, CALL_PAGE)
        client = FakeClient(reply(call_answer(title="x")))
        run_extract(conn, client, web, model="claude-haiku-5-5", ids=[ids[2]])
        endpoint, sent = client.requests[0]
        check("other models use the standard endpoint without a fallback; --candidate picks one",
              endpoint == "standard" and "fallbacks" not in sent and sent["model"] == "claude-haiku-5-5"
              and len(client.requests) == 1 and "Aufruf 2" in sent["messages"][0]["content"])

        tally, out = run_extract(conn, FakeClient(reply(call_answer())), web, max_cost=0.001)
        check("--max-cost stops cleanly before a candidate that would go over it",
              "Stopping before candidate" in out and tally.requests == 0
              and conn.execute("SELECT count(*) FROM candidates WHERE extracted_at IS NULL").fetchone()[0] == 3)

        client = FakeClient(anthropic.APIConnectionError(request=request), reply(call_answer()))
        tally, out = run_extract(conn, client, web, limit=2)
        attempts = [r[0] for r in conn.execute("SELECT attempts FROM candidates WHERE id IN (?, ?)", (ids[0], ids[1]))]
        check("an API error leaves the candidate for the next run without counting an attempt; --limit applies",
              attempts == [0, 0] and "API error" in out and len(client.requests) == 2)

        denied = anthropic.AuthenticationError("invalid x-api-key", body=None,
                                               response=httpx2.Response(401, request=request))
        try:
            run_extract(conn, FakeClient(denied), web)
            stopped = False
        except StopExtraction:
            stopped = True
        run = conn.execute("SELECT finished_at, error_details FROM runs ORDER BY id DESC").fetchone()
        check("an authentication error stops extraction, and the run still records it",
              stopped and run["finished_at"] is not None and "AuthenticationError" in run["error_details"])

        lines = []
        dry_run(conn, web_with_call().fetcher(), clock=lambda: NOW, out=lines.append)
        check("--dry-run fetches the texts and estimates both prices, without a client",
              "no API calls" in lines[0] and "with --batch" in lines[-1] and "standard API" in lines[-1])


def test_batch():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp))) as conn:
        add_candidate(conn)
        add_candidate(conn, title="Zweiter Aufruf")
        past = [{"stage": "sketch", "date": "2026-10-01", "annual_date": None, "note": None}]
        client = FakeClient(reply(call_answer(deadlines=past)), reply(call_answer(title="Zweiter Aufruf")),
                            reply(call_answer()))
        tally, out = run_extract(conn, client, batch=True)
        check("--batch submits one batch, polls until it ends, and retries failures in a second batch",
              len(client.batches) == 2 and len(client.batches[0]["requests"]) == 2
              and len(client.batches[1]["requests"]) == 1 and "<note>" in
              client.batches[1]["requests"][0]["params"]["messages"][0]["content"]
              and client.batches[0]["polls"] == 2 and client.requests == [])
        check("batch requests have no fallback, and are costed at half price",
              all("fallbacks" not in r["params"] for b in client.batches for r in b["requests"])
              and abs(tally.cost - 3 * usage_cost(reply().usage, "claude-sonnet-5-5", batch=True)) < 1e-9
              and conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 1
              and conn.execute("SELECT count(*) FROM call_listings").fetchone()[0] == 2
              and tally.outcomes["known URL, listing added"] == 1)


def test_api_key_not_printed():
    with tempfile.TemporaryDirectory() as tmp:
        conn = make_db(Path(tmp))
        add_candidate(conn, url="https://agency.test/calls", title="Ohne eigene Seite")
        conn.close()
        env = {**os.environ, "ANTHROPIC_API_KEY": FAKE_KEY, "ANTHROPIC_BASE_URL": "http://127.0.0.1:9"}
        result = subprocess.run([sys.executable, str(REPO / "extract.py"), "--db", str(Path(tmp) / "test.db")],
                                capture_output=True, text=True, env=env, timeout=120)
        check("extract.py never prints the API key, even when the API can't be reached",
              "API error" in result.stdout and FAKE_KEY not in result.stdout + result.stderr)


def main():
    test_prompt_and_schema()
    test_call_texts()
    test_checks_and_deadlines()
    test_extraction()
    test_thin_source_flags()
    test_undated_stages()
    test_retries_and_failures()
    test_options_and_errors()
    test_batch()
    test_api_key_not_printed()
    print("All checks passed.")


if __name__ == "__main__":
    main()
