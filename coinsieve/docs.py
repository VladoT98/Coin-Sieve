"""Project documents as plain text, for AI summaries: whitepaper (PDF or HTML), docs site, homepage.

URLs come only from the token's stored profile (the project's own listings and homepage) and are fetched with
webfetch (https only, public hosts, size caps). Text is capped per document and in total; a capped document is
marked `truncated` so the summary label can say so. Nothing here is shown on the site directly.
"""
import hashlib
import io
import logging
import re
import urllib.parse
from html.parser import HTMLParser

from coinsieve import webfetch

log = logging.getLogger(__name__)

_SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "button", "iframe", "template"}
_BLOCK = {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th", "pre"}
_TOKENOMICS = re.compile(r"token|tokenomic|allocation|distribution|vesting|unlock|supply|emission|airdrop|economics", re.I)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP and self.skip:
            self.skip -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_text(body):
    p = _Text()
    try:
        p.feed(body.decode("utf-8", errors="replace"))
    except Exception as e:  # malformed HTML: keep what was parsed
        log.debug("html parse stopped: %s", e)
    lines = (" ".join(line.split()) for line in "".join(p.parts).splitlines())
    return "\n".join(line for line in lines if line)


def pdf_text(body, max_pages):
    """(text, pages_read, pages_total). Image-only pages give no text."""
    from pypdf import PdfReader  # lazy: only the summaries job needs it
    reader = PdfReader(io.BytesIO(body))
    pages = reader.pages
    out = []
    for page in pages[:max_pages]:
        try:
            out.append(page.extract_text() or "")
        except Exception as e:  # one broken page should not lose the document
            log.debug("pdf page skipped: %s", e)
    return "\n".join(" ".join(t.split()) for t in out if t.strip()), min(len(pages), max_pages), len(pages)


def _cap(text, n):
    return (text, False) if len(text) <= n else (text[:n], True)


def collect(profile, acfg, fetcher=webfetch.fetch):
    """[{kind, url, text, truncated, note}] for the token's whitepaper, docs and homepage (in that order).
    Docs sites add up to `docs_extra_pages` same-site pages about the token (tokenomics, allocation, ...)."""
    links = (profile or {}).get("links") or {}
    out, seen = [], set()

    def get(url, max_bytes):
        try:
            page = fetcher(url, max_bytes)
        except Exception as e:  # unreachable: summarised without this document
            log.info("doc %s unavailable: %s", url[:80], e)
            return None
        return page if page.status == 200 else None

    def add(kind, url, text, truncated=False, note=None):
        text, cut = _cap(text, acfg["max_chars_per_doc"])
        if len(text.strip()) >= acfg["min_chars"]:
            out.append({"kind": kind, "url": url, "text": text, "truncated": truncated or cut, "note": note})

    for kind in ("whitepaper", "docs", "website"):
        link = links.get(kind) or {}
        url = link.get("url")
        if not url or link.get("status") == "dead" or url in seen:
            continue
        seen.add(url)
        is_pdf = link.get("format") == "pdf" or url.lower().endswith(".pdf")
        page = get(url, acfg["max_pdf_bytes"] if is_pdf else acfg["max_html_bytes"])
        if page is None:
            continue
        if page.content_type == "application/pdf" or page.body[:5] == b"%PDF-":
            try:
                text, read, total = pdf_text(page.body, acfg["max_pdf_pages"])
            except Exception as e:
                log.info("pdf %s unreadable: %s", url[:80], e)
                continue
            add(kind, page.url, text, read < total or page.truncated, f"first {read} of {total} pages" if read < total else None)
            continue
        if "html" not in page.content_type:
            continue
        add("homepage" if kind == "website" else kind, page.url, html_text(page.body), page.truncated)
        if kind == "docs":  # a few more pages of the docs site about the token itself
            host = urllib.parse.urlsplit(page.url).hostname
            extra = [u for u, label in webfetch.links(page.body, page.url)
                     if urllib.parse.urlsplit(u).hostname == host and u not in seen and _TOKENOMICS.search(f"{label} {u}")]
            for u in extra[:acfg["docs_extra_pages"]]:
                seen.add(u)
                sub = get(u, acfg["max_html_bytes"])
                if sub is not None and "html" in sub.content_type:
                    add("docs", sub.url, html_text(sub.body), sub.truncated)
    total, kept = 0, []
    for d in out:  # overall cap, in priority order
        if total >= acfg["max_chars_total"]:
            break
        room = acfg["max_chars_total"] - total
        if len(d["text"]) > room:
            d = {**d, "text": d["text"][:room], "truncated": True}
        kept.append(d)
        total += len(d["text"])
    return kept


def digest(docs):
    """Stable hash of the documents' text (decides whether a summary must be regenerated)."""
    h = hashlib.sha256()
    for d in sorted(docs, key=lambda d: (d["kind"], d["url"])):
        h.update(f"{d['kind']}|{d['url']}|".encode())
        h.update(d["text"].encode("utf-8", errors="replace"))
    return h.hexdigest()
