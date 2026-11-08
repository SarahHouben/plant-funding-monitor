"""Tests for step 13 (canonical URLs, duplicates, updates, closures, the daily
refresh) and pipeline.py, with a fake web and a fake API client: no network,
no spending.

Run from the repo root, after installing requirements.txt:
    .venv/bin/python tests/test_update.py
"""

import csv
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from monitor.dedupe import call_key, title_similarity  # noqa: E402
from monitor.updates import run_update  # noqa: E402
from monitor.urls import canonical_url  # noqa: E402
from pipeline import run_pipeline  # noqa: E402
from test_fetch import EU_URL, FakeWeb  # noqa: E402

NOW = datetime(2026, 11, 8, 8, 0, tzinfo=timezone.utc)
SOURCE_COLUMNS = ["id", "funder", "programme", "url", "access_method", "content_selector",
                  "scope", "family", "check_frequency", "status", "notes"]
FILLER = ("Gefördert werden Verbundvorhaben zur Saatgutgesundheit von Getreide und Leguminosen. Erwartet werden "
          "Vorhaben, die Diagnostik, Züchtung und Saatgutbehandlung verbinden und deren Ergebnisse in der Praxis "
          "erprobt werden. Antragsberechtigt sind Hochschulen, außeruniversitäre Forschungseinrichtungen und "
          "Unternehmen mit Sitz in Deutschland. Die Laufzeit beträgt bis zu drei Jahre, die Förderung erfolgt als "
          "nicht rückzahlbarer Zuschuss. Skizzen werden in einem zweistufigen Verfahren begutachtet.")
TABLES = ["calls", "call_listings", "call_tags", "call_history", "call_flags", "call_updates", "candidates",
          "screening_log", "item_changes", "source_items"]


def check(label, ok):
    if not ok:
        print(f"FAIL {label}")
        sys.exit(1)
    print(f"PASS {label}")


def make_db(work, sources):
    """A database made by init_db.py; sources are (id, url, access_method, family)."""
    for name in ["schema.sql", "init_db.py", "tags.csv"]:
        shutil.copy(REPO / name, work / name)
    with (work / "sources.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, SOURCE_COLUMNS)
        writer.writeheader()
        for source_id, url, method, family in sources:
            writer.writerow({"id": source_id, "funder": f"Funder {source_id}", "programme": "Calls", "url": url,
                             "access_method": method, "content_selector": "", "scope": "national",
                             "family": family, "check_frequency": "daily", "status": "active", "notes": ""})
    subprocess.run([sys.executable, str(work / "init_db.py"), "--db", "test.db"], check=True, capture_output=True)
    conn = sqlite3.connect(work / "test.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def counts(conn):
    return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES}


# --- A fake web of three funders, and a fake API client --------------------------

def listing(sections):
    body = "".join(f'<h2>{title}</h2><p>Frist {deadline}. Ein Förderaufruf der Agentur. <a href="{href}">Mehr</a></p>'
                   for title, deadline, href in sections)
    return (f"<html><body><main><h1>Aufrufe</h1><p>Aktuelle Förderaufrufe dieser Agentur, laufend ergänzt.</p>"
            f"{body}</main></body></html>")


def call_page(title, deadline):
    return f"<html><body><main><h1>{title}</h1><p>Frist {deadline}.</p><p>{FILLER}</p></main></body></html>"


def funders_web(saatgut_deadline="2027-03-01"):
    """agency and agency2 share a funder family, agency3 is on its own.

    agency2 announces agency's Saatgut call under its own URL and a longer
    title (to be merged), lists it under agency's URL in another form (to be a
    listing), and has a similar call with the same deadline (to be flagged).
    agency3 has a call with the same title in another family (left alone).
    """
    web = FakeWeb()
    web.set("https://agency.test/calls", listing([
        ("Aufruf Saatgut (Beispiel)", saatgut_deadline, "/aufrufe/saatgut"),
        ("Aufruf Gerste (Beispiel)", "2027-04-01", "/aufrufe/gerste")]))
    web.set("https://agency2.test/calls", listing([
        ("Bekanntmachung: Aufruf Saatgut (Beispiel)", "2027-03-01", "/files/saatgut"),
        ("Saatgut bei der Agentur", "2027-03-01", "http://AGENCY.test/aufrufe/saatgut/?nn=4711"),
        ("Aufruf Saatgut und Sortenprüfung (Beispiel)", "2027-03-01", "/aufrufe/sorten")]))
    web.set("https://agency3.test/calls", listing([("Aufruf Saatgut (Beispiel)", "2027-03-01", "/aufrufe/saatgut")]))
    web.set("https://agency.test/aufrufe/saatgut", call_page("Aufruf Saatgut (Beispiel)", saatgut_deadline))
    web.set("https://agency.test/aufrufe/gerste", call_page("Aufruf Gerste (Beispiel)", "2027-04-01"))
    web.set("https://agency2.test/files/saatgut", call_page("Bekanntmachung: Aufruf Saatgut (Beispiel)", "2027-03-01"))
    web.set("https://agency2.test/aufrufe/sorten", call_page("Aufruf Saatgut und Sortenprüfung (Beispiel)", "2027-03-01"))
    web.set("https://agency3.test/aufrufe/saatgut", call_page("Aufruf Saatgut (Beispiel)", "2027-03-01"))
    return web


def _reply(answer):
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(answer))],
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0, output_tokens_details=None))


class FakeClient:
    """Answers screening by calling every linked item a call, and extraction
    from the call text: the title screening gave and the 'Frist' date."""

    def __init__(self):
        self.requests = []
        self.messages = SimpleNamespace(create=self._screen)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._extract))

    def _screen(self, **request):
        self.requests.append("screening")
        message = request["messages"][0]["content"]
        items = json.loads(message.split("<items>\n", 1)[1].split("\n</items>", 1)[0])
        results = []
        for item in items:
            links = re.findall(r"\]\((https?://[^)\s]+)\)", item["text"])
            results += ([{"item_id": item["item_id"], "url": link, "title": item["title"], "category": "call",
                          "reason": "A call."} for link in links]
                        or [{"item_id": item["item_id"], "url": item["url"], "title": item["title"],
                             "category": "not_a_call", "reason": "No call."}])
        return _reply({"results": results})

    def _extract(self, **request):
        self.requests.append("extraction")
        message = request["messages"][0]["content"]
        title = message.split("<call_title>", 1)[1].split("</call_title>", 1)[0]
        text = message.split("<call_text>\n", 1)[1]
        deadline = re.search(r"Frist (\d{4}-\d{2}-\d{2})", text).group(1)
        return _reply({"not_a_call": False, "reason": None, "call": {
            "title": title, "title_en": None, "call_type": "deadline_bound", "status": "open", "opening_date": None,
            "deadlines": [{"stage": "sketch", "date": deadline, "annual_date": None, "note": None}],
            "summary_en": "Seed health projects.", "summary_de": "Vorhaben zur Saatgutgesundheit.",
            "eligible_countries": ["DE"], "eligible_institution_types": ["research_institute"],
            "eligibility_conditions": None, "ipk_eligible": "yes", "ipk_eligible_reason": "Research institutes apply.",
            "funding_amount": None, "funding_duration": "up to 3 years", "apply_via": None, "apply_url": None,
            "more_info_links": [], "contact_point": None, "relevance_score": 4,
            "tags": {"research_area": ["seeds-yield"], "organism": "other-cereals",
                     "funding_type": "collaborative-project", "career_stage": "any-stage"},
            "evidence": [{"field": "deadlines", "quote": f"Frist {deadline}"}]}})


def pipeline(conn, web, client, **options):
    lines = []
    result = run_pipeline(conn, web.fetcher(), client, clock=lambda: NOW, sleep=lambda s: None,
                          out=lines.append, **options)
    return result, "\n".join(lines)


# --- Tests -----------------------------------------------------------------------

def test_canonical_urls():
    check("canonical URLs: lower-case host, https, no default port, fragment or trailing slash, sorted parameters",
          canonical_url("HTTP://Agency.Test:80/Aufrufe/Saatgut/?b=2&a=1#oben")
          == "https://agency.test/Aufrufe/Saatgut?a=1&b=2")
    check("canonical URLs drop tracking and session parameters, nn and view=renderNewsletterHtml",
          canonical_url("https://www.bmftr.bund.de/SharedDocs/x.html;jsessionid=ABC?nn=123&utm_source=y")
          == "https://www.bmftr.bund.de/SharedDocs/x.html"
          and canonical_url("https://foo.test/n?view=renderNewsletterHtml&id=7") == "https://foo.test/n?id=7"
          and canonical_url("https://foo.test/n?view=list") == "https://foo.test/n?view=list"
          and canonical_url("https://foo.test:8443/a/") == "https://foo.test:8443/a")
    check("a call without a link of its own is keyed by its listing and its normalised title",
          call_key("https://agency.test/calls/#x", "Programm „Saatgut“ (Beispiel)")
          == "https://agency.test/calls#programm saatgut")
    ratio, _ = title_similarity("Bekanntmachung über die Förderung innovativer Pflanzenschutzmaßnahmen und "
                                "Pflanzenschutzverfahren im Rahmen des Programms zur Innovationsförderung",
                                "Innovative Pflanzenschutzmaßnahmen und Pflanzenschutzverfahren")
    ratio_en, _ = title_similarity("Funding innovative plant protection measures under the Innovation Funding "
                                   "Programme", "Innovative plant protection measures")
    check("announcement boilerplate and a trailing programme clause don't hide that two titles are the same",
          ratio > 0.9 and ratio_en > 0.85)
    check("titles that differ before the programme clause still differ",
          title_similarity("Gesundes Saatgut im Rahmen des Programms X", "Robuste Gerste im Rahmen des Programms X")[0]
          < 0.6)


def test_pipeline():
    sources = [("agency", "https://agency.test/calls", "html", "bund"),
               ("agency2", "https://agency2.test/calls", "html", "bund"),
               ("agency3", "https://agency3.test/calls", "html", "")]
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp), sources)) as conn:
        web, client = funders_web(), FakeClient()
        (lines, stopped), out = pipeline(conn, web, client)
        calls = {row["title"]: row for row in conn.execute("SELECT * FROM calls ORDER BY id DESC")}
        saatgut = conn.execute("SELECT * FROM calls WHERE canonical_url = 'https://agency.test/aufrufe/saatgut'"
                               ).fetchone()
        listings = conn.execute("SELECT source_id, url FROM call_listings WHERE call_id = ? ORDER BY id",
                                (saatgut["id"],)).fetchall()
        check("the pipeline runs every stage and prints one summary",
              stopped is None and [line.split(":")[0] for line in lines] == ["Fetch", "Screening", "Extraction", "Update"]
              and out.startswith("Pipeline run 1") and "Cost:" in out)
        check("a duplicate under a different URL that is clearly the same call is merged into it",
              conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 4
              and "Bekanntmachung: Aufruf Saatgut (Beispiel)" not in calls
              and ("agency2", "https://agency2.test/files/saatgut") in [tuple(row) for row in listings])
        check("the same URL in another form isn't extracted again, but becomes a listing",
              ("agency2", "http://AGENCY.test/aufrufe/saatgut/?nn=4711") in [tuple(row) for row in listings]
              and len(listings) == 3 and client.requests.count("extraction") == 5)
        flags = conn.execute("SELECT * FROM call_flags").fetchall()
        sorten = calls["Aufruf Saatgut und Sortenprüfung (Beispiel)"]
        check("a call that may be the same is stored and flagged as a possible duplicate",
              len(flags) == 1 and flags[0]["call_id"] == sorten["id"] and flags[0]["other_call_id"] == saatgut["id"]
              and flags[0]["kind"] == "possible_duplicate" and "shared deadline 2027-03-01" in flags[0]["detail"])
        merges = conn.execute("SELECT call_id, handled_at FROM call_updates WHERE kind = 'merge'").fetchall()
        check("the merged duplicate's extraction goes to call_updates as a merge, which the update stage handles",
              len(merges) == 1 and merges[0]["call_id"] == saatgut["id"] and merges[0]["handled_at"] is not None)
        check("the same title in another funder family is a call of its own",
              conn.execute("SELECT count(*) FROM calls WHERE title = 'Aufruf Saatgut (Beispiel)'").fetchone()[0] == 2)

        before, asked = counts(conn), len(client.requests)
        (lines, stopped), out = pipeline(conn, web, client, force=True)
        check("running the pipeline again creates no duplicate rows and calls no model",
              counts(conn) == before and len(client.requests) == asked and stopped is None
              and "Extraction: nothing waiting" in out)

        with conn:
            conn.execute("UPDATE calls SET review_status = 'checked' WHERE id = ?", (saatgut["id"],))
        web = funders_web(saatgut_deadline="2027-04-15")
        (lines, stopped), out = pipeline(conn, web, client, force=True)
        call = conn.execute("SELECT * FROM calls WHERE id = ?", (saatgut["id"],)).fetchone()
        history = {row["field"]: (row["old_value"], row["new_value"])
                   for row in conn.execute("SELECT * FROM call_history WHERE call_id = ?", (saatgut["id"],))}
        old_deadlines, new_deadlines = history.get("deadlines", ("", ""))
        check("a changed deadline is re-extracted and recorded in the history, with the old and new value",
              "2027-03-01" in old_deadlines and "2027-04-15" in new_deadlines
              and json.loads(call["deadlines"])[0]["date"] == "2027-04-15" and call["next_deadline"] == "2027-04-15")
        check("a deadline that moves later makes the call 'extended' and sends a checked call back for review",
              call["status"] == "extended" and history.get("status") == ("open", "extended")
              and call["review_status"] == "new" and history.get("review_status") == ("checked", "new")
              and conn.execute("SELECT count(*) FROM call_updates WHERE handled_at IS NULL").fetchone()[0] == 0)
        check("the update kept the call's other rows: still one call, no new listings or flags",
              conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 4
              and conn.execute("SELECT count(*) FROM call_flags").fetchone()[0] == 1)

        dump = list(conn.iterdump())
        (lines, stopped), out = pipeline(conn, web, None, dry_run=True)
        check("a dry run of the pipeline changes nothing and calls no API",
              list(conn.iterdump()) == dump and "Fetch:      skipped (dry run)" in out and stopped is None)


def insert_call(conn, source_id, url, deadlines, status="open", call_type="deadline_bound", opening_date=None,
                item_keys=(), review_status="new"):
    """A call as extraction would store it, with a listing per (source, item key)."""
    call_id = conn.execute(
        "INSERT INTO calls (source_id, title, original_url, canonical_url, call_type, status, opening_date, deadlines, "
        "summary_en, summary_de, ipk_eligible, ipk_eligible_reason, relevance_score, review_status, first_seen_at, "
        "last_verified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'S', 'Z', 'yes', 'R', 3, ?, ?, ?)",
        (source_id, f"Call at {url}", url, canonical_url(url), call_type, status, opening_date, json.dumps(deadlines),
         review_status, "2026-11-01T06:00:00Z", "2026-11-01T06:00:00Z")).lastrowid
    for listing_source, item_key in item_keys or [(source_id, url)]:
        conn.execute("INSERT INTO call_listings (call_id, source_id, item_key, url, first_seen_at, last_seen_at) "
                     "VALUES (?, ?, ?, ?, '2026-11-01T06:00:00Z', '2026-11-01T06:00:00Z')",
                     (call_id, listing_source, item_key, url))
    return call_id


def remove_item(conn, source_id, item_key):
    conn.execute("INSERT INTO item_changes (run_id, source_id, item_key, url, title, change, text, detected_at) "
                 "VALUES (1, ?, ?, ?, 'Gone', 'removed', 'No longer listed: Gone', '2026-11-08T06:00:00Z')",
                 (source_id, item_key, f"https://{source_id}.test/calls"))


def test_closures_refresh_and_updates():
    sources = [("agency", "https://agency.test/calls", "html", "bund"),
               ("agency2", "https://agency2.test/calls", "html", "bund"),
               ("eu", EU_URL, "api", "")]
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp), sources)) as conn:
        future = [{"stage": "sketch", "date": "2027-02-01"}]
        past = [{"stage": "sketch", "date": "2026-10-01"}]
        with conn:
            conn.execute("INSERT INTO runs (started_at) VALUES ('2026-11-08T06:00:00Z')")
            topic = insert_call(conn, "eu", "https://ec.test/topic-details/T-1", future, item_keys=[("eu", "T-1")])
            shared = insert_call(conn, "agency", "https://agency.test/a", future,
                                 item_keys=[("agency", "A"), ("agency2", "B")])
            ended = insert_call(conn, "agency", "https://agency.test/b", past, status="open",
                                item_keys=[("agency", "C")])
            stale = insert_call(conn, "agency", "https://agency.test/c", past, status="open")
            yearly = insert_call(conn, "agency", "https://agency.test/d", [{"stage": "a", "annual_date": "03-01"}],
                                 call_type="recurring")
            opening = insert_call(conn, "agency", "https://agency.test/e", future, status="forthcoming",
                                  opening_date="2026-11-01")
            for source_id, item_key in [("eu", "T-1"), ("agency", "A"), ("agency", "C")]:
                remove_item(conn, source_id, item_key)

        dump = list(conn.iterdump())
        lines = []
        run_update(conn, clock=lambda: NOW, out=lines.append, dry_run=True)
        check("a dry run of the update stage reports what it would do and saves nothing",
              list(conn.iterdump()) == dump and "dry run, nothing saved" in lines[0] and "calls closed" in lines[0])

        stats = run_update(conn, clock=lambda: NOW, out=lambda line: None)
        status = {row["id"]: row for row in conn.execute("SELECT * FROM calls")}
        check("an EU topic that drops out of the open and forthcoming list is closed",
              status[topic]["status"] == "closed" and status[topic]["next_deadline"] is None
              and conn.execute("SELECT new_value FROM call_history WHERE call_id = ? AND field = 'status'",
                               (topic,)).fetchone()[0] == "closed")
        check("a removed section is only a removed listing while the call is listed elsewhere",
              status[shared]["status"] == "open"
              and conn.execute("SELECT count(*) FROM call_flags WHERE call_id = ?", (shared,)).fetchone()[0] == 0
              and conn.execute("SELECT removed_at FROM call_listings WHERE call_id = ? AND item_key = 'A'",
                               (shared,)).fetchone()[0] is not None)
        check("a call no longer listed anywhere whose deadlines have passed is closed",
              status[ended]["status"] == "closed")
        check("removed items are marked as dealt with and lose their text",
              conn.execute("SELECT count(*) FROM item_changes WHERE change = 'removed' AND screened_at IS NOT NULL "
                           "AND text IS NULL").fetchone()[0] == 3 and stats["removed items handled"] == 3)
        with conn:
            remove_item(conn, "agency2", "B")
        run_update(conn, clock=lambda: NOW, out=lambda line: None)
        flag = conn.execute("SELECT * FROM call_flags WHERE call_id = ?", (shared,)).fetchone()
        check("a call that isn't listed anywhere any more but hasn't ended is flagged, not closed",
              flag["kind"] == "section_removed" and "agency2" in flag["detail"]
              and conn.execute("SELECT status FROM calls WHERE id = ?", (shared,)).fetchone()[0] == "open")
        check("the daily refresh closes calls whose deadlines have all passed",
              status[stale]["status"] == "closed")
        check("recurring calls move on to their next annual date",
              status[yearly]["status"] == "open" and status[yearly]["next_deadline"] == "2027-03-01")
        check("forthcoming calls open on their opening date",
              status[opening]["status"] == "open" and status[opening]["next_deadline"] == "2027-02-01")
        history_rows = conn.execute("SELECT count(*) FROM call_history").fetchone()[0]
        run_update(conn, clock=lambda: NOW, out=lambda line: None)
        check("running the update stage again changes nothing more",
              conn.execute("SELECT count(*) FROM call_history").fetchone()[0] == history_rows
              and conn.execute("SELECT count(*) FROM call_flags").fetchone()[0] == 1)

        with conn:
            candidate_change = conn.execute(
                "INSERT INTO item_changes (run_id, source_id, item_key, url, change, detected_at, screened_at) "
                "VALUES (1, 'agency', 'Z', 'https://agency.test/z', 'changed', '2026-11-08T06:00:00Z', "
                "'2026-11-08T06:00:00Z')").lastrowid
            candidate = conn.execute(
                "INSERT INTO candidates (run_id, item_change_id, source_id, url, title, reason, created_at, extracted_at) "
                "VALUES (1, ?, 'agency', 'https://agency.test/e', 'E', 'r', '2026-11-08T06:00:00Z', "
                "'2026-11-08T06:00:00Z')", (candidate_change,)).lastrowid
            row = conn.execute("SELECT * FROM calls WHERE id = ?", (opening,)).fetchone()
            same = {"title": row["title"], "title_en": None, "call_type": row["call_type"], "status": row["status"],
                    "opening_date": row["opening_date"],
                    "deadlines": [{"stage": "sketch", "date": "2027-02-01", "annual_date": None, "note": None}],
                    "summary_en": "Reworded summary.", "summary_de": "Neu formuliert.",
                    "eligible_countries": [], "eligible_institution_types": [], "eligibility_conditions": None,
                    "ipk_eligible": "yes", "ipk_eligible_reason": "Reworded.", "funding_amount": None,
                    "funding_duration": None, "apply_via": None, "apply_url": None, "more_info_links": [],
                    "contact_point": None, "relevance_score": 3, "tags": {}, "evidence": []}
            for extracted in ({"not_a_call": False, "reason": None, "call": same},
                              {"not_a_call": True, "reason": "The page now shows a news item.", "call": None}):
                conn.execute("INSERT INTO call_updates (call_id, candidate_id, run_id, extracted, detected_at) "
                             "VALUES (?, ?, 1, ?, '2026-11-08T07:00:00Z')", (opening, candidate, json.dumps(extracted)))
        history_rows = conn.execute("SELECT count(*) FROM call_history").fetchone()[0]
        stats = run_update(conn, clock=lambda: NOW, out=lambda line: None)
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (opening,)).fetchone()
        check("an update that only rewords the summary changes nothing, but re-verifies the call",
              conn.execute("SELECT count(*) FROM call_history").fetchone()[0] == history_rows
              and row["summary_en"] == "S" and row["last_verified_at"] == "2026-11-08T08:00:00Z"
              and stats["calls re-verified, unchanged"] == 1)
        check("an update saying the page no longer describes a call flags it for the reviewer",
              conn.execute("SELECT kind, detail FROM call_flags WHERE call_id = ?", (opening,)).fetchone()[:]
              == ("not_a_call", "The page now shows a news item.")
              and conn.execute("SELECT count(*) FROM call_updates WHERE handled_at IS NULL").fetchone()[0] == 0)


def extracted_call(**changes):
    """An extraction result's call, as stored in call_updates."""
    call = {"title": "Innovative Pflanzenschutzmaßnahmen und Pflanzenschutzverfahren", "title_en": None,
            "call_type": "deadline_bound", "status": "open", "opening_date": None,
            "deadlines": [{"stage": "sketch", "date": "2027-01-14", "annual_date": None, "note": "12:00, easy-Online"}],
            "summary_en": "PDF summary.", "summary_de": "PDF-Zusammenfassung.", "eligible_countries": ["DE"],
            "eligible_institution_types": ["company", "research_institute"],
            "eligibility_conditions": "Research institutes only in consortia with substantial industry participation",
            "ipk_eligible": "yes", "ipk_eligible_reason": "German research institutes can take part in consortia.",
            "funding_amount": None, "funding_duration": "up to 3 years", "apply_via": "easy-Online",
            "apply_url": "https://foerderportal.bund.de/easyonline", "more_info_links": [], "contact_point": None,
            "relevance_score": 4, "tags": {},
            "evidence": [{"field": "ipk_eligible", "quote": "soweit eine substanzielle Wirtschaftsbeteiligung"},
                         {"field": "deadlines", "quote": "14. Januar 2027, 12 Uhr (Ausschlussfrist)"}]}
    call.update(changes)
    return call


def test_merged_duplicates():
    sources = [("agency", "https://agency.test/calls", "html", "bund"),
               ("agency2", "https://agency2.test/calls", "html", "bund")]
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp), sources)) as conn:
        with conn:
            conn.execute("INSERT INTO runs (started_at) VALUES ('2026-11-08T06:00:00Z')")
            change = conn.execute("INSERT INTO item_changes (run_id, source_id, item_key, url, change, detected_at, "
                                  "screened_at) VALUES (1, 'agency2', 'K', 'https://agency2.test/k', 'new', "
                                  "'2026-11-08T06:00:00Z', '2026-11-08T06:00:00Z')").lastrowid
            candidate = conn.execute("INSERT INTO candidates (run_id, item_change_id, source_id, url, title, reason, "
                                     "created_at, extracted_at) VALUES (1, ?, 'agency2', 'https://agency2.test/k', "
                                     "'K', 'r', '2026-11-08T06:00:00Z', '2026-11-08T06:00:00Z')",
                                     (change,)).lastrowid
            # The BLE case: the call came first from a short listing that says little about eligibility.
            ble = insert_call(conn, "agency", "https://agency.test/pflanzenschutz",
                              [{"stage": "sketch", "date": "2027-01-14"}], review_status="checked")
            conn.execute("UPDATE calls SET ipk_eligible = 'unclear', ipk_eligible_reason = 'The listing does not say "
                         "who may apply.', eligible_countries = '[\"DE\"]', "
                         "evidence = '{\"deadlines\": [\"Skizzen bis 14. Januar 2027\"]}' WHERE id = ?", (ble,))
            # A call whose merged duplicate disagrees on deadline, status and eligibility.
            other = insert_call(conn, "agency", "https://agency.test/gerste", [{"stage": "sketch", "date": "2027-01-14"}])
            disagreeing = extracted_call(deadlines=[{"stage": "sketch", "date": "2027-02-01", "annual_date": None,
                                                     "note": None}],
                                         status="forthcoming", ipk_eligible="no", funding_amount="up to EUR 1 million")
            for call_id, extracted in [(ble, extracted_call()), (other, disagreeing)]:
                conn.execute("INSERT INTO call_updates (call_id, candidate_id, run_id, kind, extracted, detected_at) "
                             "VALUES (?, ?, 1, 'merge', ?, '2026-11-08T07:00:00Z')",
                             (call_id, candidate, json.dumps({"not_a_call": False, "reason": None,
                                                              "call": extracted})))
        stats = run_update(conn, clock=lambda: NOW, out=lambda line: None)

        row = conn.execute("SELECT * FROM calls WHERE id = ?", (ble,)).fetchone()
        history = {r["field"]: (r["old_value"], r["new_value"])
                   for r in conn.execute("SELECT * FROM call_history WHERE call_id = ?", (ble,))}
        check("a merged duplicate turns 'unclear' IPK eligibility into 'yes', bringing its reason and evidence",
              row["ipk_eligible"] == "yes" and history.get("ipk_eligible") == ("unclear", "yes")
              and row["ipk_eligible_reason"] == "German research institutes can take part in consortia."
              and json.loads(row["evidence"]) == {"deadlines": ["Skizzen bis 14. Januar 2027"],
                                                  "ipk_eligible": ["soweit eine substanzielle Wirtschaftsbeteiligung"]})
        check("it fills the call's empty fields and records each in the history",
              row["funding_duration"] == "up to 3 years"
              and row["apply_url"] == "https://foerderportal.bund.de/easyonline"
              and json.loads(row["eligible_institution_types"]) == ["company", "research_institute"]
              and {"funding_duration", "apply_url", "eligible_institution_types", "eligibility_conditions",
                   "apply_via"} <= set(history))
        check("it overwrites nothing the call already has, and agreeing values raise no flag",
              row["title"] == "Call at https://agency.test/pflanzenschutz" and row["summary_en"] == "S"
              and json.loads(row["deadlines"]) == [{"stage": "sketch", "date": "2027-01-14"}]
              and "title" not in history and "deadlines" not in history
              and conn.execute("SELECT count(*) FROM call_flags WHERE call_id = ?", (ble,)).fetchone()[0] == 0)
        check("a checked call whose eligibility was filled goes back for review",
              row["review_status"] == "new" and history.get("review_status") == ("checked", "new"))

        row = conn.execute("SELECT * FROM calls WHERE id = ?", (other,)).fetchone()
        flag = conn.execute("SELECT * FROM call_flags WHERE call_id = ?", (other,)).fetchone()
        fields = {r["field"] for r in conn.execute("SELECT field FROM call_history WHERE call_id = ?", (other,))}
        check("different deadlines, status or eligibility aren't overwritten; the call is flagged instead",
              json.loads(row["deadlines"]) == [{"stage": "sketch", "date": "2027-01-14"}]
              and row["status"] == "open" and row["ipk_eligible"] == "yes"
              and flag is not None and flag["kind"] == "merge_conflict"
              and all(word in flag["detail"] for word in ["deadlines", "status", "ipk_eligible", "2027-02-01"]))
        check("empty fields are still filled when a merged duplicate disagrees elsewhere",
              row["funding_amount"] == "up to EUR 1 million" and "funding_amount" in fields
              and not fields & {"deadlines", "status", "ipk_eligible"})
        check("both merges are handled and counted",
              conn.execute("SELECT count(*) FROM call_updates WHERE handled_at IS NULL").fetchone()[0] == 0
              and stats["merged duplicates applied"] == 2 and stats["calls flagged: merged duplicate disagrees"] == 1)


def main():
    test_canonical_urls()
    test_pipeline()
    test_closures_refresh_and_updates()
    test_merged_duplicates()
    print("All checks passed.")


if __name__ == "__main__":
    main()
