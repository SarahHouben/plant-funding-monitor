"""Canonical URLs: one form of a URL for comparing calls.

Funders link the same page in different ways (http or https, with or without
a trailing slash, with tracking or session parameters). The canonical form
removes those differences. It is for comparison only; the URL shown to people
stays as it was found.
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that don't change which page is shown: tracking, sessions,
# and bund.de's navigation node.
IGNORED_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|msclkid|mc_cid|mc_eid|jsessionid|phpsessid|sid|"
                            r"sessionid|session_id|nn|_)$", re.I)
# Parameters ignored only with this value.
IGNORED_PAIRS = {("view", "rendernewsletterhtml")}
# The identifier in an EU Funding & Tenders topic page URL.
EU_TOPIC_ID = re.compile(r"/topic-details/([^/?#]+)")


def eu_topic_id(url):
    """The EU topic identifier in a topic page URL, or None."""
    match = EU_TOPIC_ID.search(url or "")
    return match.group(1) if match else None


def canonical_url(url):
    """The URL with a lower-case host, https for http, no default port, no
    fragment, no trailing slash, no tracking or session parameters, and the
    remaining parameters sorted."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    parts = urlsplit(url)
    scheme = "https" if parts.scheme.lower() in ("http", "https") else parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r";jsessionid=[^/?#]*", "", parts.path, flags=re.I)
    path = re.sub(r"/{2,}", "/", path).rstrip("/")
    params = sorted((key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                    if not IGNORED_PARAMS.match(key) and (key.lower(), value.lower()) not in IGNORED_PAIRS)
    return urlunsplit((scheme, netloc, path, urlencode(params), ""))
