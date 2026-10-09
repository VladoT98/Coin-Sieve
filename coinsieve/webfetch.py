"""Fetch project web pages (homepages, docs, whitepapers) whose URLs come from third-party data.

Those URLs are untrusted, so every request (and every redirect hop) is checked: https only (http links are
tried as https), the host must resolve to public IP addresses only (no localhost / private network), at most
`max_redirects` hops, and at most `max_bytes` are read. Nothing here executes or renders content.
"""
import ipaddress
import logging
import socket
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser

import requests

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; CoinSieve/1.0; token profile link check)"


class BlockedURL(ValueError):
    """URL refused before any request was sent (scheme, host or address not allowed)."""


@dataclass
class Page:
    url: str              # final URL after redirects
    status: int
    content_type: str     # lower-case media type without parameters, e.g. "text/html"
    body: bytes
    truncated: bool       # more than max_bytes were available


def _public_host(host):
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedURL(f"DNS lookup failed for {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise BlockedURL(f"{host} resolves to a non-public address")


def check_url(url, resolve=True):
    """Normalised https URL, or BlockedURL. http:// links are upgraded to https://."""
    parts = urllib.parse.urlsplit((url or "").strip())
    if parts.scheme == "http":
        parts = parts._replace(scheme="https")
    if parts.scheme != "https" or not parts.hostname:
        raise BlockedURL(f"not an https URL: {url!r}"[:200])
    if parts.username or parts.password or (parts.port not in (None, 443)):
        raise BlockedURL("credentials or non-standard port in URL")
    if resolve:
        _public_host(parts.hostname)
    return urllib.parse.urlunsplit(parts._replace(fragment=""))


def fetch(url, max_bytes, timeout=20, max_redirects=5, session=None):
    """GET with the checks above. Returns a Page for any HTTP status; raises BlockedURL or requests errors."""
    s = session or requests.Session()
    current = check_url(url)
    for _ in range(max_redirects + 1):
        with s.get(current, timeout=timeout, stream=True, allow_redirects=False,
                   headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.5"}) as r:
            if r.is_redirect and r.headers.get("Location"):
                current = check_url(urllib.parse.urljoin(current, r.headers["Location"]))
                continue
            body = r.raw.read(max_bytes + 1, decode_content=True)
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            return Page(current, r.status_code, ctype, body[:max_bytes], len(body) > max_bytes)
    raise BlockedURL(f"more than {max_redirects} redirects")


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())[:120]))
            self._href = None


def links(html_bytes, base_url):
    """[(absolute https URL, anchor text)] from an HTML page; other schemes dropped."""
    p = _Links()
    try:
        p.feed(html_bytes.decode("utf-8", errors="replace"))
    except Exception as e:  # malformed HTML: keep what was parsed
        log.debug("html parse stopped: %s", e)
    out = []
    for href, text in p.links:
        u = urllib.parse.urljoin(base_url, href.strip())
        if urllib.parse.urlsplit(u).scheme in ("http", "https"):
            out.append((urllib.parse.urlunsplit(urllib.parse.urlsplit(u)._replace(fragment="")), text))
    return list(dict.fromkeys(out))


def site_domain(url):
    """Registrable-ish domain: last two labels of the host ('www.bonkcoin.com' -> 'bonkcoin.com').
    Good enough for project sites; country second-level domains (co.uk) are rare here."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return ".".join(host.split(".")[-2:]) if host else ""
