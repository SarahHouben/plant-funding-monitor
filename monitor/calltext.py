"""The text of a call for extraction: its own page, a PDF, or the EU topic details.

Every text is scrubbed of personal contact details and capped in length, and
the same text is what the extraction checks look in for quotes and links.
"""

import io
import re
from urllib.parse import urldefrag, urlsplit

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from monitor.clean import html_to_markdown, normalize
from monitor.http import FetchError
from monitor.readers import EU_API_HOST, EU_STATUS, eu_results
from monitor.scrub import scrub
from monitor.urls import eu_topic_id

# About 23,000 tokens. A longer text keeps its start and its end, since German
# funding guidelines put deadlines and the procedure near the end.
TEXT_LIMIT = 60_000
HEAD = 45_000
TAIL = 15_000
SHORTENED = "\n\n[… text shortened here …]\n\n"
# A page with less text than this probably needs JavaScript or failed to load.
MIN_TEXT = 500
# A call text shorter than this probably leaves out details, so the call is
# flagged for review.
THIN_TEXT = 1_500
# The origin of the text of a call without a link of its own: the listing
# section it was found in, which is its only text, not a fallback.
NO_OWN_LINK = "stored text (no link of its own)"
# How much of the listing where screening found the call is added as context.
LISTING_LIMIT = 3_000
LISTING_HEADER = "\n\n---\n\nWhere the call was listed on the funder's site:\n\n"


def cap(text):
    """Shorten a text to TEXT_LIMIT characters, keeping its start and end."""
    if len(text) <= TEXT_LIMIT:
        return text
    return text[:HEAD].rstrip() + SHORTENED + text[-TAIL:].lstrip()


# A word hyphenated at a line break: a lower-case letter, a hyphen at the end of
# the line, and a lower-case letter starting the next. German keeps the hyphen
# before "und", "oder" and the like ("Pflanzen- und Tierzucht").
LINE_BREAK_HYPHEN = re.compile(r"([a-zäöüß])-\n(?!(?:und|oder|bzw|sowie|als|bis)\b)([a-zäöüß])")


def pdf_text(data):
    """The text of a PDF, with words hyphenated at line breaks joined again."""
    reader = PdfReader(io.BytesIO(data))
    text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    return normalize(LINE_BREAK_HYPHEN.sub(r"\1\2", text))


def eu_topic_text(result):
    """The facts, description and conditions of an EU topic from the search API."""
    meta = result["metadata"]

    def first(name):
        values = meta.get(name) or [None]
        return values[0]

    url = result.get("url") or ""
    deadlines = ", ".join(sorted(d[:10] for d in meta.get("deadlineDate") or [])) or "not stated"
    facts = [f"# {first('title')}",
             f"Topic: {first('identifier')}",
             f"Topic page (apply through the Funding & Tenders portal): {url}",
             f"Call: {' – '.join(filter(None, [first('callIdentifier'), first('callTitle')]))}",
             f"Status: {EU_STATUS.get(first('status'), first('status'))}",
             f"Opens: {(first('startDate') or '')[:10] or 'not stated'}",
             f"Deadlines: {deadlines}",
             f"Deadline model: {first('deadlineModel') or 'not stated'}"]
    text = "\n".join(facts)
    description = html_to_markdown(first("descriptionByte") or "", url)
    conditions = html_to_markdown(first("topicConditions") or "", url)
    if description:
        text += f"\n\n## Description\n\n{description}"
    if conditions:
        text += f"\n\n## Conditions\n\n{conditions}"
    return scrub(text)


def thin_source(text, origin):
    """Why a call text may be too thin for every detail of the call, or None: the
    call's own page or PDF couldn't be used, so the listing's stored text stood
    in, or the call's text (without the listing added for context) is short."""
    if origin.startswith("stored text") and origin != NO_OWN_LINK:
        return f"Extracted from the listing's {origin}"
    length = len(text.partition(LISTING_HEADER)[0])
    if length < THIN_TEXT:
        return f"The call text ({origin}) has only {length} characters"
    return None


class CallTexts:
    """Gets call texts during one run, reading all EU topics once."""

    def __init__(self, fetcher):
        self.fetcher = fetcher
        self._eu_topics = {}   # search API URL -> {topic identifier: result}

    def get(self, candidate, source):
        """Return (text, origin): the call text and where it came from."""
        url, _ = urldefrag(candidate["url"])
        stored = candidate["text"] or ""
        if url == urldefrag(source["url"])[0]:
            return cap(stored), NO_OWN_LINK
        topic = eu_topic_id(url)
        if topic and urlsplit(source["url"]).netloc == EU_API_HOST:
            result = self._eu_topic(source["url"], topic)
            if result is not None:
                return cap(eu_topic_text(result)), "EU topic details"
        try:
            text, origin = self._fetch(url)
        except (FetchError, ValueError, PyPdfError) as e:
            return cap(stored), f"stored text (fetching failed: {e})"
        if len(text) < MIN_TEXT:
            return cap(stored), f"stored text (the {origin} had only {len(text)} characters)"
        text = cap(text)
        if stored:
            text += LISTING_HEADER + stored[:LISTING_LIMIT]
        return text, origin

    def _fetch(self, url):
        response = self.fetcher.get(url)
        content_type = response.headers.get("content-type", "").lower()
        if "pdf" in content_type or response.body[:5] == b"%PDF-":
            return scrub(pdf_text(response.body)), "PDF"
        return scrub(html_to_markdown(response.body, response.url, charset=response.charset)), "page"

    def _eu_topic(self, api_url, identifier):
        if api_url not in self._eu_topics:
            results, _ = eu_results(self.fetcher, api_url)
            self._eu_topics[api_url] = {(r["metadata"].get("identifier") or [None])[0]: r
                                        for r in results}
        return self._eu_topics[api_url].get(identifier)
