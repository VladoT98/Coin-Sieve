"""Token profiles for the coverage list: project links, chains and contracts, exchanges, supply, logo cache.

Per token (daily, budgeted): CoinGecko `coins/{id}` (links, platforms, categories, genesis date, first 100 tickers;
see coingecko.py) + DexScreener `tokens/v1/{chain}/{contract}` `info` for tokens issued on a chain it lists (CoinGecko
has no homepage for some, e.g. JUP; verified 2026-10-11 on solana, ethereum, bsc, base, arbitrum, polygon, avalanche,
optimism, sui, tron; EVM addresses come back checksummed, so they are matched case-insensitively) + the project's
homepage, scanned for whitepaper / docs / GitHub links. When the token's age is not
proven by the market data, the 1-year price history is fetched once (stored for the token page chart too).

Links are only taken from these sources — never constructed, except one check of `https://docs.<domain>` on
the project's own domain, kept only if it answers with an HTML page. Link status:
  ok        the URL answered 200 (whitepaper/docs: with a PDF or HTML page)
  blocked   listed, but the site refuses automated checks (401/403/429/503) — still counts as listed
  error     listed, the last check failed (timeout, TLS, ...) — still counts as listed
  dead      404/410 or the host does not exist — does not count
  listed    X / GitHub: listed by a source, not fetched (X blocks automated requests)
A failed CoinGecko lookup keeps the previous CoinGecko data; nothing is assumed from missing data.
"""
import functools
import logging
import re
import urllib.parse
from datetime import datetime, timezone

from coinsieve import coingecko, history, webfetch

log = logging.getLogger(__name__)

LINK_KINDS = ("website", "whitepaper", "docs", "x", "github")
# CoinGecko platform id -> DexScreener chainId (each checked live 2026-10-11)
DEX_CHAINS = {"solana": "solana", "ethereum": "ethereum", "binance-smart-chain": "bsc", "base": "base",
              "arbitrum-one": "arbitrum", "polygon-pos": "polygon", "avalanche": "avalanche",
              "optimistic-ethereum": "optimism", "sui": "sui", "tron": "tron"}
COUNTS = ("ok", "blocked", "error")          # statuses that mean "the project lists this link"
_DOCS_HOSTS = ("gitbook.io", "notion.site", "readthedocs.io", "mintlify.app")
_WP_HINT = re.compile(r"white\s*-?\s*paper|lite\s*-?\s*paper|light\s*-?\s*paper", re.I)
_DOCS_HINT = re.compile(r"(^|[\s/._-])(docs?|documentation|gitbook)([\s/._-]|$)", re.I)
_LEGAL = re.compile(r"terms|privacy|policy|legal|dmca|cookie|disclaimer|careers|press|brand", re.I)
_GITHUB = re.compile(r"^https://(www\.)?github\.com/[A-Za-z0-9_.-]+/?([A-Za-z0-9_.-]+/?)?$")


def _status(page_or_error):
    if isinstance(page_or_error, webfetch.BlockedURL):
        return "dead" if "DNS lookup failed" in str(page_or_error) else "error"
    if isinstance(page_or_error, Exception):
        return "error"
    s = page_or_error.status
    if s == 200:
        return "ok"
    if s in (404, 410):
        return "dead"
    return "blocked" if s in (401, 403, 429, 503) else "error"


def _get(url, pcfg, fetcher):
    try:
        return fetcher(url, pcfg["max_html_bytes"] if not url.lower().endswith(".pdf") else pcfg["max_pdf_probe_bytes"])
    except Exception as e:  # recorded as the link's status
        return e


def _same_site(url, home_domain):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host == home_domain or host.endswith("." + home_domain) or host.endswith(_DOCS_HOSTS)


def _norm(url):
    p = urllib.parse.urlsplit(url)
    return f"{(p.hostname or '').lower().removeprefix('www.')}{p.path.rstrip('/')}"


def _link(url, source, status, now, fmt=None):
    d = {"url": url, "source": source, "status": status, "checked_at": now}
    if fmt:
        d["format"] = fmt
    return d


def classify(url, page, hint):
    """('whitepaper' | 'docs' | None, format) for a candidate URL and its fetched page.
    hint = 'whitepaper' | 'docs' (where the link came from)."""
    if isinstance(page, Exception) or page.status != 200:
        return hint, None
    if page.content_type == "application/pdf" or page.body[:5] == b"%PDF-":
        return "whitepaper", "pdf"
    if page.content_type not in ("text/html", "application/xhtml+xml"):
        return None, None
    host = (urllib.parse.urlsplit(page.url).hostname or "").lower()
    path = urllib.parse.urlsplit(page.url).path.lower()
    if hint == "docs" or host.startswith("docs.") or host.endswith(_DOCS_HOSTS) or "/docs" in path:
        return ("whitepaper" if hint == "whitepaper" and _WP_HINT.search(page.url) else "docs"), "html"
    return hint, "html"


def build(address, jm, dex_info, cg_summary, old, pcfg, now, fetcher=webfetch.fetch):
    """Profile dict for one token. jm = stored coverage metrics (CoinGecko markets; `website` / `twitter` only from
    older Jupiter metrics); dex_info = DexScreener `info` (None for the coverage list);
    cg_summary = coingecko.summarize(...) or None when the lookup failed (old CoinGecko data is kept)."""
    old = old or {}
    cg = cg_summary if cg_summary is not None else old.get("coingecko")
    cg_ok = cg_summary is not None
    links = {}

    # website: first listed homepage; Jupiter (older metrics), then CoinGecko, then DexScreener's "Website" entries
    sites = [(jm.get("website"), "jupiter")] + [(u, "coingecko") for u in ((cg or {}).get("homepage") or [])]
    sites += [(w.get("url"), "dexscreener") for w in (dex_info or {}).get("websites") or []
              if str(w.get("label") or "").lower() == "website"]
    home = home_page = None
    for url, src in sites:
        if not url:
            continue
        try:
            url = webfetch.check_url(url, resolve=False)
        except webfetch.BlockedURL:
            continue
        page = _get(url, pcfg, fetcher)
        links["website"] = _link(url, src, _status(page), now)
        home, home_page = url, page
        if links["website"]["status"] != "dead":
            break

    # candidates: (url, source, hint)
    cands, fallback = [], []
    if cg and cg.get("whitepaper"):
        cands.append((cg["whitepaper"], "coingecko", "whitepaper"))
    github = [(u, "coingecko") for u in (cg or {}).get("github") or []]
    if home and not isinstance(home_page, Exception) and home_page.status == 200 and "html" in home_page.content_type:
        dom = webfetch.site_domain(home_page.url)
        for url, text in webfetch.links(home_page.body, home_page.url):
            label = f"{text} {urllib.parse.urlsplit(url).path}"
            if _LEGAL.search(label):
                continue
            if _GITHUB.match(url.replace("http://", "https://")):
                github.append((url, "homepage"))
            elif not _same_site(url, dom) and not url.lower().endswith(".pdf"):
                continue
            elif _WP_HINT.search(label) or (url.lower().endswith(".pdf") and _same_site(url, dom)):
                cands.append((url, "homepage", "whitepaper"))
            elif _DOCS_HINT.search(label) or (urllib.parse.urlsplit(url).hostname or "").startswith("docs."):
                cands.append((url, "homepage", "docs"))
        fallback = [(f"https://docs.{dom}", "homepage-domain", "docs")]

    seen = {_norm(home)} if home else set()
    for url, src, hint in cands[:pcfg["max_candidates"]] + fallback:
        if src == "homepage-domain" and (links.get("docs") or {}).get("status") in COUNTS:
            continue  # docs already found; the docs.<domain> check is only a fallback
        try:
            url = webfetch.check_url(url, resolve=False)
        except webfetch.BlockedURL:
            continue
        if _norm(url) in seen:
            continue
        seen.add(_norm(url))
        page = _get(url, pcfg, fetcher)
        status = _status(page)
        if src == "homepage-domain" and status != "ok":
            continue  # the docs.<domain> check only counts when it answers
        kind, fmt = classify(url, page, hint)
        if kind is None or kind in links and links[kind]["status"] in COUNTS:
            continue
        if status == "dead" and kind in links:
            continue
        links[kind] = _link(getattr(page, "url", url) if status == "ok" else url, src, status, now, fmt)

    xs = [(jm.get("twitter"), "jupiter")] + [(s.get("url"), "dexscreener") for s in (dex_info or {}).get("socials") or []
                                             if s.get("type") == "twitter"]
    if cg and cg.get("twitter"):
        xs.append((f"https://x.com/{cg['twitter']}", "coingecko"))
    x = next(((u, s) for u, s in xs if u and u.startswith("https://")), None)
    if x:
        links["x"] = _link(x[0], x[1], "listed", now)
    if github:
        links["github"] = _link(github[0][0], github[0][1], "listed", now)

    return {
        "links": {k: links.get(k) for k in LINK_KINDS},
        "coingecko": cg,
        "coingecko_checked_at": now if cg_ok else old.get("coingecko_checked_at"),
        "icon": jm.get("icon") or (dex_info or {}).get("imageUrl"),
        "supply": {"circulating": jm.get("circ_supply"), "total": jm.get("total_supply"), "source": "coingecko",
                   "max": jm.get("max_supply") if jm.get("max_supply") is not None else ((cg or {}).get("supply") or {}).get("max"),
                   "max_source": "coingecko"},
        **{k: old[k] for k in ("first_price_ts", "age_checked_at") if k in old},
    }


def _date_ts(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def facts(profile, ccfg):
    """Coverage facts derived from a stored profile (merged into the coverage metrics, so the dashboard's
    what-if needs no DB access). None = not known yet."""
    if not profile:
        return {"profile_checked": False, "website_or_docs": None, "links_found": [], "exchanges": None,
                "age_checked": False, "platform": None, "contracts": {}, "genesis_ts": None, "first_price_ts": None}
    links = profile.get("links") or {}
    found = [k for k in LINK_KINDS if (links.get(k) or {}).get("status") in COUNTS + ("listed",)]
    cg = profile.get("coingecko") or {}
    genesis = _date_ts(cg.get("genesis_date"))
    return {"profile_checked": True, "website_or_docs": any(k in found for k in ("website", "whitepaper", "docs")),
            "links_found": found, "exchanges": cg.get("exchanges") if cg.get("listed") else None,
            # age is settled once the profiles job looked for a genesis date and the 1-year history
            "age_checked": bool(cg.get("listed")) and "age_checked_at" in profile,
            "platform": cg.get("platform"), "contracts": cg.get("platforms") or {},
            "genesis_ts": genesis, "first_price_ts": profile.get("first_price_ts")}


def dex_info(dex, cg):
    """DexScreener `info` (websites, socials) for the token's contract on its issuing chain, or None."""
    chain, contract = DEX_CHAINS.get((cg or {}).get("platform")), ((cg or {}).get("platforms") or {}).get((cg or {}).get("platform"))
    if not dex or not chain or not contract:
        return None
    try:
        pairs = dex._get(f"tokens/v1/{chain}/{contract}") or []
    except Exception as e:  # links from DexScreener are optional
        log.warning("dexscreener info %s/%s failed: %s", chain, contract, e)
        return None
    for p in pairs:
        if str((p.get("baseToken") or {}).get("address") or "").lower() == contract.lower():
            return p.get("info")
    return None


def update(store, cfg, ids, metrics, cg_client, now, fetcher=None, cache_logo=None, dex=None):
    """Refresh due profiles (oldest first, at most profiles.max_per_run). Stops early on a CoinGecko rate limit
    (the rest are due again next run). Returns counts."""
    chain, pcfg, min_age = coingecko.CHAIN, cfg["profiles"], cfg["coverage"]["min_age_days"]
    fetcher = fetcher or functools.partial(webfetch.fetch, timeout=pcfg["fetch_timeout_s"])
    old = store.profiles(chain, ids)
    due = sorted((a for a in ids if a not in old or now - old[a]["updated_at"] >= pcfg["refresh_hours"] * 3600),
                 key=lambda a: ((old.get(a) or {}).get("updated_at") or 0, -((metrics.get(a) or {}).get("mcap_usd") or 0))
                 )[:pcfg["max_per_run"]]
    stats = {"due": len(due), "done": 0, "coingecko_failed": 0, "logos": 0, "age_checks": 0, "rate_limited": False}
    for cid in due:
        try:
            cg = coingecko.summarize(cg_client.coin(cid))
        except RuntimeError as e:  # rate limit / outage after retries: stop, the rest stay due
            log.warning("coingecko %s failed, stopping: %s", cid, e)
            stats.update(coingecko_failed=stats["coingecko_failed"] + 1, rate_limited=True)
            break
        except Exception as e:  # keep the old CoinGecko data for this token
            log.warning("coingecko %s failed: %s", cid, e)
            cg = None
            stats["coingecko_failed"] += 1
        m = metrics.get(cid) or {}
        prof = build(cid, m, dex_info(dex, cg), cg, (old.get(cid) or {}).get("data"), pcfg, now, fetcher)
        if cg and cg.get("listed") and "age_checked_at" not in prof:
            ok, ts = _first_price(store, cg_client, cid, m, cg, min_age, now, stats)
            if ok:
                prof.update(first_price_ts=ts, age_checked_at=now)
        store.save_profile(chain, cid, prof, now)
        store.commit()
        stats["done"] += 1
        if cache_logo and prof.get("icon") and cache_logo(prof["icon"]):
            stats["logos"] += 1
    return stats


def _first_price(store, cg_client, cid, m, cg, min_age, now, stats):
    """(settled, first price ts or None): the first point of the token's 1-year price history. Fetched only when
    the market data and the genesis date don't already prove `min_age` days; a stored series is reused.
    settled=False when the fetch failed (the age stays "not checked yet" and is retried at the next refresh)."""
    known = [m.get("history_days")] + [(now - ts) / 86400 for ts in (_date_ts(cg.get("genesis_date")),) if ts]
    if any(d is not None and d >= min_age for d in known):
        return True, None
    pts = (store.price_history(coingecko.CHAIN, cid).get("365d") or {}).get("points")
    if not pts:
        try:
            pts = history.parse(cg_client.market_chart(cid, 365))
            stats["age_checks"] += 1
        except Exception as e:
            log.warning("coingecko 1y history %s failed: %s", cid, e)
            return False, None
        if pts:
            store.save_history(coingecko.CHAIN, cid, "365d", pts, now)
    return True, (pts[0][0] if pts else None)
