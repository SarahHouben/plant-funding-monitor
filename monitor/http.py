"""Polite HTTP: robots.txt, one request at a time with a pause per site,
timeouts, retries and a cookie jar per site.

Requests go through a transport function so tests can serve saved pages
instead of using the network.
"""

import http.client
import http.cookiejar
import socket
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from protego import Protego

USER_AGENT = "plant-funding-monitor/0.1 (+https://github.com/SarahHouben/plant-funding-monitor)"
# The name robots.txt rules are matched against.
ROBOTS_NAME = "plant-funding-monitor"


class FetchError(Exception):
    """A request failed for good, or robots.txt doesn't allow it."""


@dataclass
class Response:
    status: int
    url: str
    headers: dict = field(default_factory=dict)  # lower-case names
    body: bytes = b""

    @property
    def charset(self):
        """The charset from the Content-Type header, or None."""
        for part in self.headers.get("content-type", "").split(";")[1:]:
            name, _, value = part.strip().partition("=")
            if name.lower() == "charset" and value:
                return value.strip('"')
        return None


def read_body(raw):
    """Read a response body, accepting a short body if the HTML in it is complete.

    Some servers announce a Content-Length larger than what they send, although
    the page itself arrives in full.
    """
    try:
        return raw.read()
    except http.client.IncompleteRead as e:
        if b"</html>" in e.partial[-4096:].lower():
            return e.partial
        raise


class UrllibTransport:
    """Sends requests over the network, with a cookie jar per site.

    Some sites only serve a page to a client that sends their cookie back: the
    Bundesanzeiger, for example, sets a session cookie and redirects until it
    gets it. Redirects are followed inside urllib, so the cookies are handled
    there too. Each site has its own jar, so no cookie reaches another site.
    """

    def __init__(self):
        self._openers = {}   # site -> opener with that site's cookie jar

    def __call__(self, method, url, headers, data, timeout):
        """Send one request and return a Response for any HTTP status."""
        site = _site(url)
        if site not in self._openers:
            jar = http.cookiejar.CookieJar()
            self._openers[site] = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._openers[site].open(request, timeout=timeout) as raw:
                body = read_body(raw)
                return Response(raw.status, raw.geturl(),
                                {k.lower(): v for k, v in raw.headers.items()}, body)
        except urllib.error.HTTPError as e:
            return Response(e.code, url, {k.lower(): v for k, v in e.headers.items()}, e.read())


# Network errors worth another attempt. Anything else is a bug and should surface.
NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, socket.timeout,
                  ConnectionError, TimeoutError)


class Fetcher:
    """Fetches pages politely. One Fetcher per run, used for all sources in turn."""

    def __init__(self, transport=None, sleep=time.sleep, clock=time.monotonic,
                 pause=2.0, timeout=30, attempts=3, backoff=(5, 15)):
        self.transport = transport or UrllibTransport()
        self.sleep = sleep
        self.clock = clock
        self.pause = pause
        self.timeout = timeout
        self.attempts = attempts
        self.backoff = backoff
        self._robots = {}      # site -> Protego, None (no rules) or FetchError
        self._delay = {}       # site -> seconds between requests
        self._last = {}        # site -> clock time the last request finished

    def get(self, url):
        """GET a URL that robots.txt allows. Raises FetchError unless the status is 2xx."""
        self._check_robots(url)
        return self._ok(self._request("GET", url))

    def post_form(self, url, fields):
        """POST multipart form fields, given as {name: (value, content type)}."""
        self._check_robots(url)
        boundary = uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n'
                 f"Content-Type: {content_type}\r\n\r\n{value}\r\n"
                 for name, (value, content_type) in fields.items()]
        data = ("".join(parts) + f"--{boundary}--\r\n").encode("utf-8")
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        return self._ok(self._request("POST", url, data, headers))

    @staticmethod
    def _ok(response):
        if not 200 <= response.status < 300:
            raise FetchError(f"HTTP {response.status} for {response.url}")
        return response

    def _check_robots(self, url):
        site = _site(url)
        if site not in self._robots:
            self._robots[site] = self._load_robots(site)
        rules = self._robots[site]
        if isinstance(rules, FetchError):
            raise rules
        if rules is not None and not rules.can_fetch(url, ROBOTS_NAME):
            raise FetchError(f"robots.txt does not allow {url}")

    def _load_robots(self, site):
        """Read a site's robots.txt as RFC 9309 says: a 4xx means no rules, while
        a server error or no answer means the whole site is off limits for now."""
        try:
            response = self._request("GET", site + "/robots.txt")
        except FetchError as e:
            return FetchError(f"robots.txt unavailable, so not fetching from {site}: {e}")
        if 200 <= response.status < 300:
            rules = Protego.parse(response.body.decode("utf-8", errors="replace"))
            self._delay[site] = max(self.pause, rules.crawl_delay(ROBOTS_NAME) or 0)
            return rules
        if 400 <= response.status < 500:
            return None
        return FetchError(f"robots.txt returned HTTP {response.status}, so not fetching from {site}")

    def _request(self, method, url, data=None, extra_headers=None):
        """Send a request, retrying network errors, 429 and 5xx. Returns the last response."""
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", **(extra_headers or {})}
        site = _site(url)
        for attempt in range(1, self.attempts + 1):
            self._wait(site)
            try:
                response = self.transport(method, url, headers, data, self.timeout)
                error = None
            except NETWORK_ERRORS as e:
                response, error = None, e
            finally:
                self._last[site] = self.clock()
            retry = error is not None or response.status == 429 or response.status >= 500
            if not retry or attempt == self.attempts:
                break
            self.sleep(self._retry_wait(response, attempt))
        if error is not None:
            raise FetchError(f"{type(error).__name__}: {error} ({url})")
        return response

    def _wait(self, site):
        delay = self._delay.get(site, self.pause)
        if site in self._last:
            remaining = self._last[site] + delay - self.clock()
            if remaining > 0:
                self.sleep(remaining)

    def _retry_wait(self, response, attempt):
        wait = self.backoff[min(attempt, len(self.backoff)) - 1]
        retry_after = response.headers.get("retry-after", "") if response else ""
        if retry_after.isdigit():
            wait = max(wait, min(int(retry_after), 60))
        return wait


def _site(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
