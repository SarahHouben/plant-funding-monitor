"""Tests for fetch.py and the monitor package, using saved example pages and no network.

Run from the repo root, after installing requirements.txt:
    .venv/bin/python tests/test_fetch.py

The pages in tests/fixtures copy the structure of real source pages (cookie
banners, menus, a page without <main>, icon-only links) with placeholder text.
"""

import csv
import http.client
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fetch import run  # noqa: E402
from monitor.clean import ContentNotFound, html_to_markdown, split_sections  # noqa: E402
from monitor.http import USER_AGENT, Fetcher, Response, read_body  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LISTING_URL = "https://agency.test/bekanntmachungen"
FEED_URL = "https://funder.test/feed.rss"
EU_URL = ("https://api.tech.ec.europa.eu/search-api/prod/rest/search"
          "?apiKey=SEDIA&text=***&pageSize=2")
SOURCE_COLUMNS = ["id", "funder", "programme", "url", "access_method", "content_selector",
                  "scope", "check_frequency", "status", "notes"]


def check(label, ok):
    if not ok:
        print(f"FAIL {label}")
        sys.exit(1)
    print(f"PASS {label}")


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeWeb:
    """Serves saved pages by URL instead of the network, and records every request.

    A page is a Response, or a list of Responses served in turn with the last
    one repeating. URLs that aren't set return 404, so robots.txt allows
    everything unless a test sets one. Time only passes when the fetcher sleeps.
    """

    def __init__(self):
        self.pages, self.requests, self.sleeps, self.time = {}, [], [], 0.0

    def set(self, url, body, status=200, content_type="text/html; charset=utf-8", headers=None):
        self.pages[url] = response(url, body, status, content_type, headers)

    def transport(self, method, url, headers, data, timeout):
        self.requests.append({"method": method, "url": url, "headers": headers, "time": self.time})
        page = self.pages.get(url, Response(404, url))
        if isinstance(page, list):
            page = page.pop(0) if len(page) > 1 else page[0]
        return page

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.time += seconds

    def fetcher(self):
        return Fetcher(transport=self.transport, sleep=self.sleep, clock=lambda: self.time)

    def urls(self, since=0):
        return [r["url"] for r in self.requests[since:]]


def response(url, body, status=200, content_type="text/html; charset=utf-8", headers=None):
    if isinstance(body, str):
        body = body.encode("utf-8")
    return Response(status, url, {"content-type": content_type, **(headers or {})}, body)


def make_db(work, sources):
    """A database made by init_db.py from a sources.csv with the given rows."""
    for name in ["schema.sql", "init_db.py", "tags.csv"]:
        shutil.copy(REPO / name, work / name)
    with (work / "sources.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, SOURCE_COLUMNS)
        writer.writeheader()
        for source in sources:
            writer.writerow({"funder": "Test funder", "programme": "Test programme",
                             "content_selector": "", "scope": "national",
                             "check_frequency": "daily", "status": "active", "notes": "", **source})
    subprocess.run([sys.executable, str(work / "init_db.py"), "--db", "test.db"],
                   check=True, capture_output=True)
    conn = sqlite3.connect(work / "test.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def fetch(conn, web, day=7, **options):
    """Run the fetch step on 2026-11-<day>; return the run id, its changes and its output."""
    lines = []
    run_id = run(conn, web.fetcher(), clock=lambda: datetime(2026, 11, day, 6, 0, tzinfo=timezone.utc),
                 out=lines.append, **options)
    changes = conn.execute("SELECT * FROM item_changes WHERE run_id = ? ORDER BY id",
                           (run_id,)).fetchall()
    return run_id, changes, "\n".join(lines)


def test_cleaning():
    md = html_to_markdown(fixture("listing.html"), LISTING_URL)
    check("cleaning keeps the main content", "Förderaufruf Pflanzenzüchtung" in md and "Programm B" in md)
    check("cleaning drops the cookie banner, menu, breadcrumb, skip link, footer and 'Stand:' line",
          not any(text in md for text in ["Cookies", "Startseite", "›", "Zum Inhalt", "Impressum", "Stand:"]))
    check("links are absolute and lose session and tracking parameters",
          "(https://agency.test/aufrufe/pflanzenzuechtung.html)" in md
          and "(https://www.example.org/faq?lang=de)" in md)
    check("an icon-only link keeps its URL, labelled from aria-label",
          "[Alle Programme](https://agency.test/programme/)" in md)
    check("the page splits into one section per heading",
          [path for path, _, _ in split_sections(md)] == [
              "Offene Bekanntmachungen",
              "Offene Bekanntmachungen > Förderaufruf Pflanzenzüchtung (Beispiel)",
              "Offene Bekanntmachungen > Förderaufruf Ökolandbau (Beispiel)",
              "Offene Bekanntmachungen > Weitere Förderprogramme"])

    md = html_to_markdown(fixture("no_main.html"), "https://programme.test/grants", ".main-col",
                          fallback=False)
    check("a content selector picks the main area of a page without <main>",
          "Letter of Intent" in md and "newsletter" not in md and "About us" not in md)
    try:
        html_to_markdown(fixture("no_main.html"), "https://programme.test/grants", "#missing",
                         fallback=False)
        error = None
    except ContentNotFound as e:
        error = e
    check("a selector that matches nothing is an error, not a silent fallback", error is not None)


def test_short_responses():
    class Raw:
        def __init__(self, partial):
            self.partial = partial

        def read(self):
            raise http.client.IncompleteRead(self.partial, 500)

    complete = b"<html><body><p>All here</p></body></html>\n"
    check("a response shorter than announced is accepted when its HTML is complete",
          read_body(Raw(complete)) == complete)
    try:
        read_body(Raw(b"<html><body><p>Cut off"))
        cut_off_rejected = False
    except http.client.IncompleteRead:
        cut_off_rejected = True
    check("a response cut off mid-page is still an error", cut_off_rejected)


def test_runs():
    """New, unchanged, volatile, changed and removed content across the three source types."""
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp), [
            {"id": "agency", "url": LISTING_URL, "access_method": "html"},
            {"id": "funder", "url": FEED_URL, "access_method": "rss"},
            {"id": "eu", "url": EU_URL, "access_method": "api", "scope": "eu"},
    ])) as conn:
        web = FakeWeb()
        web.set(LISTING_URL, fixture("listing.html"))
        web.set(FEED_URL, fixture("feed.rss"), content_type="application/rss+xml")
        web.set("https://funder.test/calls/b", fixture("item_page.html"))
        web.set("https://funder.test/news/c", fixture("item_page.html"))
        web.set(EU_URL + "&pageNumber=1", fixture("eu_page1.json"), content_type="application/json")
        web.set(EU_URL + "&pageNumber=2", fixture("eu_page2.json"), content_type="application/json")

        run_id, changes, out = fetch(conn, web)
        counts = Counter((c["source_id"], c["change"]) for c in changes)
        check("first run: each page section is its own new item",
              counts[("agency", "new")] == 4 and {c["title"] for c in changes if c["source_id"] == "agency"}
              >= {"Förderaufruf Pflanzenzüchtung (Beispiel)", "Weitere Förderprogramme"})
        check("first run: every feed entry and EU topic is new, the topics read over two pages",
              counts[("funder", "new")] == 3 and counts[("eu", "new")] == 3
              and sum(r["method"] == "POST" for r in web.requests) == 2)
        texts = {c["item_key"]: c["text"] for c in changes}
        entry_b = texts["https://funder.test/calls/b"]
        check("a feed entry without a description gets an excerpt of its linked page",
              entry_b.startswith("Call B: Data infrastructure") and "Who can apply" in entry_b
              and entry_b.endswith("…") and 1200 < len(entry_b) < 1700)
        check("an entry with a description doesn't fetch its linked page",
              "https://funder.test/calls/a" not in web.urls())
        check("EU topics carry their status and every deadline",
              "Status: Forthcoming" in texts["HORIZON-INFRA-2027-01-EXAMPLE-02"]
              and "Deadlines: 2027-04-22, 2027-09-16" in texts["HORIZON-INFRA-2027-01-EXAMPLE-02"])
        check("every request names the project in its User-Agent",
              all(r["headers"]["User-Agent"] == USER_AGENT for r in web.requests))
        by_site = {}
        gaps_ok = True
        for r in web.requests:
            site = r["url"].split("/")[2]
            if site in by_site and r["time"] - by_site[site] < 2:
                gaps_ok = False
            by_site[site] = r["time"]
        check("requests to the same site are at least 2 s apart", gaps_ok)
        run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        check("the run is recorded with its counts",
              run_row["started_at"] == "2026-11-07T06:00:00Z" and run_row["finished_at"] is not None
              and (run_row["sources_checked"], run_row["pages_changed"], run_row["errors"]) == (3, 3, 0))

        # The test the plan asked for: the same pages again give no changes.
        since = len(web.requests)
        _, changes, out = fetch(conn, web, force=True)
        check("second run with unchanged pages reports no changes",
              changes == [] and "Items: 0 new, 0 changed, 0 removed." in out)
        check("... although every source was fetched again",
              {LISTING_URL, FEED_URL, EU_URL + "&pageNumber=2"} <= set(web.urls(since))
              and "https://funder.test/calls/b" not in web.urls(since))

        page = (fixture("listing.html").replace("Stand: 01.11.2026", "Stand: 06.11.2026")
                .replace("jsessionid=ABC123", "jsessionid=XYZ789")
                .replace("utm_source=newsletter", "utm_source=social")
                .replace("sid=99", "sid=42").replace('"a81f9c"', '"77e0d1"'))
        web.set(LISTING_URL, page)
        web.set(FEED_URL, fixture("feed.rss").replace("Thu, 05 Nov 2026 09:00:00 GMT",
                                                       "Fri, 06 Nov 2026 10:00:00 GMT"),
                content_type="application/rss+xml")
        _, changes, _ = fetch(conn, web, force=True)
        check("new timestamps, session IDs, tracking parameters and publication dates are not changes",
              changes == [])

        page = page.replace('<li><a href="/programme/b.html">Programm B</a></li>',
                            '<li><a href="/programme/b.html">Programm B</a></li>\n'
                            '    <li><a href="/programme/c.html">Programm C</a></li>')
        page = page[:page.index("<h2>Förderaufruf Ökolandbau")] + page[page.index("<h2>Weitere"):]
        web.set(LISTING_URL, page)
        _, changes, _ = fetch(conn, web, force=True)
        changed = [c for c in changes if c["change"] == "changed"]
        removed = [c for c in changes if c["change"] == "removed"]
        check("an added line is reported as a changed section, with the line under 'Added'",
              len(changed) == 1 and changed[0]["title"] == "Weitere Förderprogramme"
              and changed[0]["text"].startswith("## Weitere Förderprogramme")
              and "Added:\n- [Programm C](https://agency.test/programme/c.html)" in changed[0]["text"])
        check("a section that disappears is reported as removed, with its last text",
              len(removed) == 1 and removed[0]["title"] == "Förderaufruf Ökolandbau (Beispiel)"
              and "15. Januar 2027" in removed[0]["text"] and len(changes) == 2)
        _, changes, _ = fetch(conn, web, force=True)
        check("a removed section is reported only once", changes == [])
        web.set(LISTING_URL, fixture("listing.html"))
        _, changes, _ = fetch(conn, web, force=True)
        check("a section that comes back is new again",
              ("new", "Förderaufruf Ökolandbau (Beispiel)") in {(c["change"], c["title"]) for c in changes})

        feed = (fixture("feed.rss")
                .replace("breeding drought-tolerant cereals.", "breeding drought-tolerant cereals and legumes.")
                .replace("Call B: Data infrastructure for crop research", "Call B: Data infrastructure for crop and soil research")
                .replace("</channel>", "  <item><title>Call D: Seed banks (example)</title>"
                         "<link>https://funder.test/calls/d</link><guid>https://funder.test/calls/d</guid>"
                         "</item>\n</channel>"))
        web.set(FEED_URL, feed, content_type="application/rss+xml")
        web.set("https://funder.test/calls/d", fixture("item_page.html"))
        since = len(web.requests)
        _, changes, _ = fetch(conn, web, force=True, only=["funder"])
        entries = {c["item_key"].rsplit("/", 1)[1]: c for c in changes}
        check("feed entries with a new description or title are changed, and a new entry is new",
              {k: c["change"] for k, c in entries.items()} == {"a": "changed", "b": "changed", "d": "new"})
        check("a changed entry keeps its excerpt without fetching its page again",
              "Who can apply" in entries["b"]["text"] and "crop and soil" in entries["b"]["text"]
              and "https://funder.test/calls/b" not in web.urls(since)
              and "https://funder.test/calls/d" in web.urls(since))

        incomplete = json.loads(fixture("eu_page2.json")) | {"results": []}
        web.set(EU_URL + "&pageNumber=2", json.dumps(incomplete), content_type="application/json")
        _, changes, out = fetch(conn, web, force=True, only=["eu"])
        check("EU results that don't add up to the total mark nothing as removed",
              changes == [] and "incomplete" in out)
        complete = json.loads(fixture("eu_page1.json")) | {"totalResults": 2}
        web.set(EU_URL + "&pageNumber=1", json.dumps(complete), content_type="application/json")
        _, changes, _ = fetch(conn, web, force=True, only=["eu"])
        check("an EU topic that is no longer listed is removed",
              [(c["change"], c["item_key"]) for c in changes] == [("removed", "ERC-2027-STG-EXAMPLE")])


def test_failures_and_robots():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp), [
            {"id": "broken", "url": "https://broken.test/page", "access_method": "html"},
            {"id": "fine", "url": "https://fine.test/page", "access_method": "html"},
            {"id": "forbidden", "url": "https://forbidden.test/calls", "access_method": "html"},
            {"id": "unreachable", "url": "https://unreachable.test/calls", "access_method": "html"},
            {"id": "redesigned", "url": "https://redesigned.test/calls", "access_method": "html",
             "content_selector": "#calls"},
    ])) as conn:
        web = FakeWeb()
        web.pages["https://broken.test/page"] = response("https://broken.test/page", "Server error", 500)
        web.set("https://fine.test/page", fixture("listing.html"))
        web.set("https://forbidden.test/robots.txt", "User-agent: *\nDisallow: /calls\n",
                content_type="text/plain")
        web.set("https://forbidden.test/calls", fixture("listing.html"))
        web.set("https://unreachable.test/robots.txt", "", status=503)
        web.set("https://redesigned.test/calls", fixture("listing.html"))

        run_id, changes, out = fetch(conn, web)
        state = {row["id"]: row for row in conn.execute("SELECT * FROM sources")}
        check("a failing source records its error and the run carries on with the next one",
              "HTTP 500" in state["broken"]["last_error"] and state["broken"]["consecutive_failures"] == 1
              and any(c["source_id"] == "fine" for c in changes))
        check("a failing request is tried 3 times, waiting 5 s and then 15 s",
              web.urls().count("https://broken.test/page") == 3 and {5, 15} <= set(web.sleeps))
        check("robots.txt that disallows a page stops the request",
              "robots.txt does not allow" in state["forbidden"]["last_error"]
              and "https://forbidden.test/calls" not in web.urls())
        check("robots.txt with a server error keeps us off the site",
              "robots.txt returned HTTP 503" in state["unreachable"]["last_error"]
              and "https://unreachable.test/calls" not in web.urls())
        check("a content selector that no longer matches is a failure",
              "matches nothing" in state["redesigned"]["last_error"])
        run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        check("the run counts the failures and lists them",
              run_row["errors"] == 4 and run_row["sources_checked"] == 5
              and {p["source_id"] for p in json.loads(run_row["error_details"])}
              == {"broken", "forbidden", "unreachable", "redesigned"})

        fetch(conn, web)
        failures = conn.execute("SELECT consecutive_failures FROM sources WHERE id = 'broken'").fetchone()[0]
        check("consecutive failures add up", failures == 2)
        web.pages["https://broken.test/page"] = [
            response("https://broken.test/page", "Busy", 503, headers={"retry-after": "30"}),
            response("https://broken.test/page", fixture("listing.html"))]
        web.sleeps.clear()
        fetch(conn, web)
        row = conn.execute("SELECT last_error, consecutive_failures FROM sources WHERE id = 'broken'").fetchone()
        check("a retry that succeeds honours Retry-After, and success clears the error",
              30 in web.sleeps and tuple(row) == (None, 0))

    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp), [
            {"id": "slow", "url": "https://slow.test/feed.rss", "access_method": "rss"},
    ])) as conn:
        web = FakeWeb()
        web.set("https://slow.test/robots.txt", "User-agent: *\nCrawl-delay: 10\n", content_type="text/plain")
        web.set("https://slow.test/feed.rss", fixture("feed.rss").replace("funder.test", "slow.test"),
                content_type="application/rss+xml")
        fetch(conn, web)
        times = [r["time"] for r in web.requests]
        check("a Crawl-delay in robots.txt spaces requests further apart",
              len(times) == 4 and all(b - a >= 10 for a, b in zip(times[1:], times[2:])))


def test_due():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp), [
            {"id": "daily", "url": "https://daily.test/page", "access_method": "html"},
            {"id": "weekly", "url": "https://weekly.test/page", "access_method": "html",
             "check_frequency": "weekly"},
    ])) as conn:
        web = FakeWeb()
        web.set("https://daily.test/page", fixture("listing.html"))
        web.set("https://weekly.test/page", fixture("listing.html"))

        def checked(day, **options):
            since = len(web.requests)
            fetch(conn, web, day=day, **options)
            return {url.split("/")[2].split(".")[0] for url in web.urls(since) if url.endswith("/page")}

        check("sources never checked are due", checked(1) == {"daily", "weekly"})
        check("two days later only the daily source is due", checked(3) == {"daily"})
        check("--force checks every active source", checked(3, force=True) == {"daily", "weekly"})
        check("--source checks just that source", checked(3, only=["weekly"]) == {"weekly"})
        check("a week after its last check the weekly source is due again", checked(10) == {"daily", "weekly"})


def main():
    test_cleaning()
    test_short_responses()
    test_runs()
    test_failures_and_robots()
    test_due()
    print("All checks passed.")


if __name__ == "__main__":
    main()
