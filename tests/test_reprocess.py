"""Tests for reprocess.py and monitor/reprocess.py, with a fake web and a fake
API client: no network, no spending.

Run from the repo root, after installing requirements.txt:
    .venv/bin/python tests/test_reprocess.py
"""

import json
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from monitor.dedupe import call_key  # noqa: E402
from monitor.extraction import call_identity  # noqa: E402
from monitor.reprocess import reextract, reextraction_link, rescreen_source, select_calls  # noqa: E402
from test_update import NOW, FakeClient, _reply, check, funders_web, insert_call, make_db, pipeline  # noqa: E402

SOURCES = [("agency", "https://agency.test/calls", "html", "bund"),
           ("agency2", "https://agency2.test/calls", "html", "bund"),
           ("agency3", "https://agency3.test/calls", "html", "")]


class Rescreening(FakeClient):
    """Answers like FakeClient, except that screening finds the items with
    these titles not relevant, and extraction finds no call in these."""

    def __init__(self, not_relevant=(), not_a_call=()):
        super().__init__()
        self.not_relevant, self.not_a_call = set(not_relevant), set(not_a_call)

    def _screen(self, **request):
        response = super()._screen(**request)
        results = json.loads(response.content[0].text)["results"]
        for result in results:
            if result["title"] in self.not_relevant:
                result["category"] = "not_relevant"
        response.content[0].text = json.dumps({"results": results})
        return response

    def _extract(self, **request):
        title = request["messages"][0]["content"].split("<call_title>", 1)[1].split("</call_title>", 1)[0]
        if title in self.not_a_call:
            self.requests.append("extraction")
            return _reply({"not_a_call": True, "reason": "The page now shows a news item.", "call": None})
        return super()._extract(**request)


def run(function, conn, web, client, **options):
    lines = []
    result = function(conn, web.fetcher(), client, clock=lambda: NOW, out=lines.append, **options)
    return result, "\n".join(lines)


def call_id(conn, title):
    return conn.execute("SELECT id FROM calls WHERE title = ? ORDER BY id", (title,)).fetchone()[0]


def test_rescreen():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp), SOURCES)) as conn:
        web = funders_web()
        pipeline(conn, web, FakeClient())
        gerste = call_id(conn, "Aufruf Gerste (Beispiel)")
        saatgut = conn.execute("SELECT id FROM calls WHERE canonical_url = 'https://agency.test/aufrufe/saatgut'"
                               ).fetchone()[0]
        items = conn.execute("SELECT count(*) FROM source_items WHERE source_id = 'agency'").fetchone()[0]

        dump = list(conn.iterdump())
        _, out = run(rescreen_source, conn, web, None, source_id="agency", dry_run=True)
        check("a dry run of a re-screen reads the source and estimates the cost, without an API client, "
              "changing nothing", list(conn.iterdump()) == dump and f"has {items} items" in out
              and "Estimated cost" in out)

        client = Rescreening(not_relevant={"Aufruf Gerste (Beispiel)"})
        last_change = conn.execute("SELECT max(id) FROM item_changes").fetchone()[0]
        rejected, out = run(rescreen_source, conn, web, client, source_id="agency")
        history = conn.execute("SELECT old_value, new_value FROM call_history WHERE call_id = ? "
                               "AND field = 'review_status'", (gerste,)).fetchone()
        check("a call whose item is now not relevant is rejected, with a history entry; the others are kept",
              rejected == [gerste] and history is not None and tuple(history) == ("new", "rejected")
              and conn.execute("SELECT review_status FROM calls WHERE id = ?", (saatgut,)).fetchone()[0] == "new")
        changes = dict(conn.execute("SELECT change, count(*) FROM item_changes WHERE id > ? GROUP BY change",
                                    (last_change,)).fetchall())
        check("every item of that source, unchanged ones included, is screened again as new, in one request",
              changes == {"new": items} and client.requests == ["screening"]
              and conn.execute("SELECT count(*) FROM item_changes WHERE screened_at IS NULL").fetchone()[0] == 0)
        check("a known call found again waits for extraction, where it only becomes a listing",
              "1 calls wait for extraction: 1 known ones" in out)


def test_reextract():
    with tempfile.TemporaryDirectory() as tmp, closing(make_db(Path(tmp), SOURCES)) as conn:
        pipeline(conn, funders_web(), FakeClient())
        saatgut = conn.execute("SELECT id FROM calls WHERE canonical_url = 'https://agency.test/aufrufe/saatgut'"
                               ).fetchone()[0]
        gerste = call_id(conn, "Aufruf Gerste (Beispiel)")
        sorten = call_id(conn, "Aufruf Saatgut und Sortenprüfung (Beispiel)")
        with conn:
            conn.execute("UPDATE calls SET status = 'closed', deadlines = '[]' WHERE id = ?", (gerste,))

        def chosen(*args, **options):
            return [row["id"] for row in select_calls(conn, *args, **options)]

        check("--closed-undated, --flag and --call choose calls",
              chosen(closed_undated=True) == [gerste] and chosen(flag="possible_duplicate") == [sorten]
              and chosen([saatgut], flag="possible_duplicate") == sorted([saatgut, sorten]))
        with conn:
            conn.execute("UPDATE calls SET review_status = 'rejected' WHERE id = ?", (sorten,))
        check("rejected calls are only chosen when named",
              chosen(flag="possible_duplicate") == [] and chosen([sorten]) == [sorten])

        web = funders_web(saatgut_deadline="2027-04-15")
        dump = list(conn.iterdump())
        _, out = run(reextract, conn, web, None, call_ids=[saatgut], dry_run=True)
        check("a dry run of a re-extraction fetches the texts and estimates the cost, changing nothing",
              list(conn.iterdump()) == dump and "Dry run: would extract 1 of 1" in out and "with --batch" in out)

        listings = conn.execute("SELECT count(*) FROM call_listings").fetchone()[0]
        client = FakeClient()
        stats, out = run(reextract, conn, web, client, call_ids=[saatgut], sleep=lambda seconds: None)
        call = conn.execute("SELECT * FROM calls WHERE id = ?", (saatgut,)).fetchone()
        history = {row["field"]: row["new_value"] for row in
                   conn.execute("SELECT field, new_value FROM call_history WHERE call_id = ?", (saatgut,))}
        check("a re-extracted call is updated through call_updates and the update stage, with its history",
              json.loads(call["deadlines"])[0]["date"] == "2027-04-15" and "2027-04-15" in history["deadlines"]
              and call["status"] == "extended" and stats["calls updated"] == 1
              and client.requests == ["extraction"]
              and conn.execute("SELECT count(*) FROM call_updates WHERE handled_at IS NULL").fetchone()[0] == 0)
        check("it adds no listing and leaves nothing waiting",
              conn.execute("SELECT count(*) FROM call_listings").fetchone()[0] == listings
              and conn.execute("SELECT count(*) FROM candidates WHERE extracted_at IS NULL").fetchone()[0] == 0)

        changes = conn.execute("SELECT count(*) FROM item_changes").fetchone()[0]
        _, out = run(reextract, conn, web, FakeClient(), call_ids=[gerste], max_cost=0.000001,
                     sleep=lambda seconds: None)
        check("a call not extracted again (over --max-cost) leaves no request behind for the daily run",
              conn.execute("SELECT count(*) FROM candidates WHERE extracted_at IS NULL").fetchone()[0] == 0
              and conn.execute("SELECT count(*) FROM item_changes").fetchone()[0] == changes
              and "weren't extracted again" in out)

        run(reextract, conn, web, Rescreening(not_a_call={"Aufruf Gerste (Beispiel)"}), call_ids=[gerste],
            sleep=lambda seconds: None)
        flag = conn.execute("SELECT detail FROM call_flags WHERE call_id = ? AND kind = 'not_a_call'",
                            (gerste,)).fetchone()
        check("a call whose page no longer describes a call is flagged not_a_call, not changed",
              flag is not None and flag[0] == "The page now shows a news item."
              and conn.execute("SELECT status FROM calls WHERE id = ?", (gerste,)).fetchone()[0] == "closed")

        with conn:
            listed = insert_call(conn, "agency", "https://agency.test/calls#Programm ohne eigene Seite", [])
            conn.execute("UPDATE calls SET canonical_url = ? WHERE id = ?",
                         (call_key("https://agency.test/calls", "Programm ohne eigene Seite"), listed))
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (listed,)).fetchone()
        url, title = reextraction_link(row)
        source = conn.execute("SELECT * FROM sources WHERE id = 'agency'").fetchone()
        check("a call without a link of its own is extracted again under its listing and the title it was listed by",
              (url, title) == ("https://agency.test/calls", "Programm ohne eigene Seite")
              and call_identity({"url": url, "title": title}, source)[1] == row["canonical_url"])


def test_command_line():
    def error(*args):
        result = subprocess.run([sys.executable, str(REPO / "reprocess.py"), *args], capture_output=True, text=True)
        return result.returncode, result.stderr

    code, message = error("--reextract")
    check("--reextract without a choice of calls is refused", code == 2 and "needs --call" in message)
    code, message = error("--rescreen-source", "agency", "--flag", "thin_source")
    check("--flag only goes with --reextract", code == 2 and "go with --reextract" in message)
    code, message = error("--rescreen-source", "agency", "--reextract")
    check("--rescreen-source and --reextract can't be combined", code == 2 and "not allowed" in message)


def main():
    test_rescreen()
    test_reextract()
    test_command_line()
    print("All checks passed.")


if __name__ == "__main__":
    main()
