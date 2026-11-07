"""Turn pages into clean Markdown, split them into sections, and fingerprint them.

Everything here is deterministic, so an unchanged page always gives the same
text and the same fingerprints.
"""

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from markdownify import markdownify

DROP_TAGS = ["script", "style", "noscript", "template", "svg", "iframe", "form", "button",
             "nav", "footer", "aside"]
# Cookie banners, consent dialogs, breadcrumbs and skip links, by id or class.
DROP_IDENT = re.compile(r"cookie|consent|cmplz|breadcrumb|skip-link", re.I)
# Query parameters that differ between visits without the page changing.
VOLATILE_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|mc_cid|mc_eid|jsessionid|phpsessid|"
                             r"sid|sessionid|session_id|_)$", re.I)
# "Last updated" lines: a keyword, then a date within the next few characters.
VOLATILE_LINE = re.compile(
    r"^\W*(stand|zuletzt aktualisiert|zuletzt geändert|letzte änderung|letzte aktualisierung|"
    r"aktualisiert|last updated|last modified|updated|page last updated)\b[^\n\d]{0,20}\d[^\n]{0,40}$",
    re.I | re.M)
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
EXCERPT_LENGTH = 1500


class ContentNotFound(ValueError):
    """The page has no content where the source says to look."""


def html_to_markdown(html, base_url, selector=None, charset=None, fallback=True):
    """Return the main content of an HTML page or fragment as Markdown, links kept.

    With a selector, the content is the first element it matches. If it matches
    nothing, the generic choice is used when fallback is true, and
    ContentNotFound is raised otherwise, so a redesigned page is noticed rather
    than silently read from the wrong place.
    """
    if isinstance(html, bytes):
        soup = BeautifulSoup(html, "html.parser", from_encoding=charset)
    else:
        soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(selector) if selector else None
    if root is None and selector and not fallback:
        raise ContentNotFound(f"content selector {selector!r} matches nothing on {base_url}")
    if root is None:
        root = (soup.find("main") or soup.find(attrs={"role": "main"}) or soup.find("article")
                or soup.body or soup)

    # A <header> is page chrome when it holds navigation or no heading; one
    # with a heading and no navigation is a card or section title, so it stays.
    # Decided before navigation is removed below.
    for header in root.find_all("header"):
        if not header.decomposed and (header.find("nav") or not header.find(HEADING_TAGS)):
            header.decompose()
    for tag in root.find_all(DROP_TAGS):
        tag.decompose()
    for tag in root.find_all(True):
        if tag.decomposed:
            continue
        ident = " ".join([tag.get("id") or ""] + (tag.get("class") or []))
        if DROP_IDENT.search(ident) or tag.has_attr("hidden") or tag.get("aria-hidden") == "true":
            tag.decompose()
    for a in root.find_all("a"):
        _clean_link(a, base_url)
    _promote_list_headings(root)

    markdown = markdownify(str(root), heading_style="ATX", bullets="-", strip=["img"])
    return normalize(markdown)


def _promote_list_headings(root):
    """Turn list entries that have their own heading into sections: the heading
    first, then the rest of the entry. Listing pages often show each call this
    way, and it keeps every call a section of its own."""
    for listing in root.find_all(["ul", "ol"]):
        entries = listing.find_all("li", recursive=False)
        if not any(entry.find(HEADING_TAGS) for entry in entries):
            continue
        listing.name = "div"
        for entry in entries:
            entry.name = "div"
            heading = entry.find(HEADING_TAGS)
            if heading is not None:
                entry.insert(0, heading.extract())


def next_page_url(html, base_url, charset=None):
    """The next page of a paginated listing, from its rel="next" link. Only
    another page of the same listing counts: same address apart from the query."""
    if isinstance(html, bytes):
        soup = BeautifulSoup(html, "html.parser", from_encoding=charset)
    else:
        soup = BeautifulSoup(html, "html.parser")
    link = soup.find(lambda tag: tag.name in ("a", "link") and "next" in (tag.get("rel") or [])
                     and tag.get("href"))
    if link is None:
        return None
    url = urljoin(base_url, link["href"])
    return url if urlsplit(url)[:3] == urlsplit(base_url)[:3] else None


def _clean_link(a, base_url):
    """Make a link absolute and stable, with its text on one line. Links that
    are only an icon get their label, or "link", so their URL is kept."""
    text = " ".join(a.get_text(" ").split())
    href = (a.get("href") or "").strip()
    if not href or href.startswith("#") or href.lower().startswith("javascript:"):
        if text:
            a.replace_with(text)
        else:
            a.decompose()
        return
    text = text or " ".join((a.get("aria-label") or a.get("title") or "link").split())
    a.attrs = {"href": clean_url(urljoin(base_url, href))}
    a.string = text


def clean_url(url):
    """Drop tracking and session parameters from a URL."""
    parts = urlsplit(url)
    path = re.sub(r";jsessionid=[^/?#]*", "", parts.path, flags=re.I)
    query = parts.query
    params = parse_qsl(query, keep_blank_values=True)
    kept = [(k, v) for k, v in params if not VOLATILE_PARAMS.match(k)]
    if len(kept) != len(params):
        query = urlencode(kept)
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def normalize(text):
    """Remove differences that don't matter: odd spaces, "last updated" lines, blank lines."""
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[   ]", " ", text)
    text = re.sub(r"[​‌‍﻿]", "", text)
    text = VOLATILE_LINE.sub("", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_sections(markdown):
    """Split Markdown at its headings.

    Returns (path, title, text) for every section that has content below its
    heading. The path joins the heading with its parent headings by " > ", and
    repeated paths get " (2)", " (3)" and so on. Text before the first heading
    has the path "".
    """
    sections, counts = [], {}
    stack = []           # (level, heading text) of the current heading and its parents
    heading_line, lines = None, []

    def flush():
        body = "\n".join(lines).strip()
        if not body:
            return
        path = " > ".join(title for _, title in stack)
        counts[path] = counts.get(path, 0) + 1
        if counts[path] > 1:
            path = f"{path} ({counts[path]})"
        title = stack[-1][1] if stack else None
        text = f"{heading_line}\n\n{body}" if heading_line else body
        sections.append((path, title, text))

    for line in markdown.splitlines():
        match = HEADING.match(line)
        if match:
            flush()
            level, title = len(match.group(1)), " ".join(match.group(2).split())
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            heading_line, lines = line, []
        else:
            lines.append(line)
    flush()
    return sections


def excerpt(text, length=EXCERPT_LENGTH):
    """Shorten text to about `length` characters, ending at a line or sentence if possible."""
    if len(text) <= length:
        return text
    cut = text[:length]
    end = max(cut.rfind("\n"), cut.rfind(". "))
    if end > length * 0.6:
        cut = cut[:end + 1]
    return cut.rstrip() + " …"


def fingerprint(*parts):
    """A stable hash of some text parts."""
    return hashlib.sha256("\x1f".join(p or "" for p in parts).encode("utf-8")).hexdigest()
