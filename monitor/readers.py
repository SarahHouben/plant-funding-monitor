"""Read a source into items: feed entries, EU topics, or sections of an HTML page."""

import json
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

from monitor.clean import (clean_url, excerpt, fingerprint, html_to_markdown, normalize,
                           split_sections)
from monitor.http import FetchError

# A page with less Markdown than this has almost certainly lost its content.
MIN_PAGE_LENGTH = 100


@dataclass
class Item:
    key: str
    url: str
    title: str | None
    fingerprint: str
    text: str
    # A feed entry without a description; it gets an excerpt of its linked page.
    needs_excerpt: bool = False


@dataclass
class Snapshot:
    items: list
    # Whether every item was read, so items that are missing have really gone.
    complete: bool = True
    # Whether missing items count as removed. Not for feeds, which only show
    # their newest entries.
    tracks_removals: bool = True


def read_source(fetcher, source):
    """Read a source row (a dict or sqlite3.Row) into a Snapshot."""
    method = source["access_method"]
    if method == "html":
        return read_html(fetcher, source)
    if method == "rss":
        return read_rss(fetcher, source)
    if method == "api" and urlsplit(source["url"]).netloc == EU_API_HOST:
        return read_eu_api(fetcher, source)
    raise ValueError(f"no reader for access method {method!r} at {source['url']}")


def read_html(fetcher, source):
    """One item per section of the page, keyed by the page URL and the heading path."""
    response = fetcher.get(source["url"])
    markdown = html_to_markdown(response.body, response.url, source["content_selector"],
                                response.charset, fallback=False)
    if len(markdown) < MIN_PAGE_LENGTH:
        raise FetchError(f"page has almost no content ({len(markdown)} characters); "
                         f"check the URL and content_selector")
    items = [Item(key=f"{source['url']}#{path}", url=source["url"], title=title or "(top of page)",
                  fingerprint=fingerprint(text), text=text)
             for path, title, text in split_sections(markdown)]
    return Snapshot(items, complete=True, tracks_removals=True)


def read_rss(fetcher, source):
    """One item per feed entry. The fingerprint covers only the title, link and
    description, so a new publication date alone is not a change."""
    response = fetcher.get(source["url"])
    feed = feedparser.parse(response.body)
    if not feed.entries and feed.bozo:
        raise FetchError(f"not a readable feed: {feed.bozo_exception}")
    items, seen = [], set()
    for entry in feed.entries:
        link = clean_url(entry.get("link", ""))
        key = entry.get("id") or link
        if not key or key in seen:
            continue
        seen.add(key)
        title = normalize(entry.get("title", ""))
        summary = entry.get("summary", "")
        description = html_to_markdown(summary, link or response.url) if summary.strip() else ""
        text = f"{title}\n\n{description}" if description else title
        items.append(Item(key=key, url=link or source["url"], title=title,
                          fingerprint=fingerprint(link, title, description), text=text,
                          needs_excerpt=not description and bool(link)))
    return Snapshot(items, complete=True, tracks_removals=False)


def linked_page_excerpt(fetcher, url, selector):
    """An excerpt of the main content of the page a feed entry links to."""
    response = fetcher.get(url)
    return excerpt(html_to_markdown(response.body, response.url, selector, response.charset))


# The EU Funding & Tenders portal's search API (SEDIA). The source URL holds the
# endpoint and page size; the filter is here: grant topics (type 1) that are
# forthcoming (31094501) or open (31094502) in Horizon Europe (43108390), from
# the current data source only, since older records stay "open" for ever.
EU_API_HOST = "api.tech.ec.europa.eu"
EU_QUERY = {"bool": {"must": [
    {"terms": {"type": ["1"]}},
    {"terms": {"status": ["31094501", "31094502"]}},
    {"term": {"frameworkProgramme": "43108390"}},
    {"terms": {"DATASOURCE": ["SEDIA"]}},
]}}
EU_SORT = {"field": "identifier", "order": "ASC"}
EU_STATUS = {"31094501": "Forthcoming", "31094502": "Open"}
EU_TOPIC_URL = ("https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/"
                "opportunities/topic-details/{}")
EU_MAX_PAGES = 20


def read_eu_api(fetcher, source):
    """One item per topic, reading every page of results."""
    fields = {
        "query": (json.dumps(EU_QUERY), "application/json"),
        "languages": ('["en"]', "application/json"),
        "sort": (json.dumps(EU_SORT), "application/json"),
    }
    items, seen, total = [], set(), 0
    for page in range(1, EU_MAX_PAGES + 1):
        response = fetcher.post_form(_with_param(source["url"], "pageNumber", page), fields)
        data = json.loads(response.body)
        total = data["totalResults"]
        results = data.get("results") or []
        for result in results:
            item = _eu_topic(result)
            if item.key not in seen:
                seen.add(item.key)
                items.append(item)
        if not results or len(seen) >= total:
            break
    return Snapshot(items, complete=len(seen) == total, tracks_removals=True)


def _eu_topic(result):
    meta = result["metadata"]

    def first(name):
        values = meta.get(name) or [None]
        return values[0]

    identifier = first("identifier")
    url = result.get("url") or EU_TOPIC_URL.format(identifier)
    title = normalize(first("title") or "")
    status = EU_STATUS.get(first("status"), first("status"))
    opens = (first("startDate") or "")[:10]
    deadlines = sorted(d[:10] for d in meta.get("deadlineDate") or [])
    call = " – ".join(filter(None, [first("callIdentifier"), first("callTitle")]))
    description = html_to_markdown(first("descriptionByte") or "", url)
    facts = [f"Topic: {identifier}", f"Call: {call}", f"Status: {status}",
             f"Opens: {opens or 'not stated'}", f"Deadlines: {', '.join(deadlines) or 'not stated'}"]
    text = f"{title}\n\n" + "\n".join(facts) + (f"\n\n{excerpt(description)}" if description else "")
    return Item(key=identifier, url=url, title=title,
                fingerprint=fingerprint(identifier, title, status, opens, ",".join(deadlines),
                                        call, description),
                text=text)


def _with_param(url, name, value):
    parts = urlsplit(url)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != name]
    params.append((name, str(value)))
    return urlunsplit(parts._replace(query=urlencode(params, safe="*")))
