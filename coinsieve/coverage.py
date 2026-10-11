"""Coverage list: the established tokens profiled on the site, from every blockchain (CoinGecko).

Universe = CoinGecko coins/markets, largest market cap first, down to `scope_min_mcap_usd` (+ every covered token).
Criteria: not in an excluded CoinGecko category (stablecoins, wrapped / bridged / staked copies, tokenised assets),
market cap and 24h volume above the minimums, price history of at least `min_age_days`, traded on at least
`min_exchanges` markets, and a website or docs page found. Unknown facts (profile not fetched yet, category lists not
loaded) are "not checked yet" — the token is not covered until they are known.

Age is a lower bound: days since the earliest of CoinGecko's all-time-high / all-time-low dates, the genesis date and
the first point of the stored 1-year price history. A token whose lower bound is too young is checked once more by
the profiles job (genesis date + 1-year history) before it fails the age criterion.

Membership reuses tier_members / tier_events as tier "coverage" (entered / left with `leave_after_hours`
hysteresis). The checklist below is the single source of truth: the reasons stored per token are derived from it.
"""
import json
import logging

from coinsieve import coingecko, profiles, site_text
from coinsieve.tiers import TierRun

log = logging.getLogger(__name__)

TIER = "coverage"
# check id -> reason code (funnel.REASON_LABELS) when the check fails
REASON = {"age": "too_young", "type": "excluded_type", "mcap": "low_mcap", "volume": "low_volume",
          "exchanges": "few_exchanges", "links": "no_website_or_docs"}


def _usd(v):
    if v is None:
        return "–"
    for div, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:,.1f}{s}"
    return f"${v:,.0f}"


def _chk(cid, ok, value, limit, paths=(), info=False):
    """ok: True / False / None (not known yet). info=True: a fact shown for context, not a criterion."""
    return {"id": cid, "label": site_text.check_label(cid), "ok": ok, "value": value, "limit": limit,
            "paths": list(paths), "info": info}


def _min(v, limit):
    return None if v is None else v >= limit


def checks(m, cfg):
    """Coverage checklist for one token's stored metrics (CoinGecko market data + profile facts)."""
    c = cfg["coverage"]
    age, cats = m.get("age_d"), m.get("excluded_categories")
    if age is not None and age >= c["min_age_days"]:
        age_ok = True
    else:
        age_ok = False if m.get("age_checked") else None
    if m["address"] in set(c["exclude_ids"]):
        type_ok, type_val = False, "excluded asset"
    elif cats is None:
        type_ok, type_val = None, "not checked yet"
    else:
        type_ok, type_val = not cats, ", ".join(cats) or "project token"
    req = c["require_website_or_docs"]
    found = [k for k in m.get("links_found") or [] if k in ("website", "whitepaper", "docs")]
    ex = m.get("exchanges")
    return [
        _chk("age", age_ok, "–" if age is None else f"{age:,.0f} days" + ("" if age_ok or m.get("age_checked") else "+"),
             f"min {c['min_age_days']:,} days", ["coverage.min_age_days"]),
        _chk("type", type_ok, type_val, ""),
        _chk("mcap", _min(m.get("mcap_usd"), c["min_mcap_usd"]), _usd(m.get("mcap_usd")),
             f"min {_usd(c['min_mcap_usd'])}", ["coverage.min_mcap_usd"]),
        _chk("volume", _min(m.get("volume_h24_usd"), c["min_volume_h24_usd"]), _usd(m.get("volume_h24_usd")),
             f"min {_usd(c['min_volume_h24_usd'])}", ["coverage.min_volume_h24_usd"]),
        _chk("exchanges", _min(ex, c["min_exchanges"]), "not checked yet" if ex is None else f"{ex} markets",
             f"min {c['min_exchanges']}", ["coverage.min_exchanges"]),
        _chk("links", m.get("website_or_docs"),
             ", ".join(found) if found else ("none found" if m.get("profile_checked") else "not checked yet"),
             "required" if req else "not required", ["coverage.require_website_or_docs"], info=not req),
    ]


def status(chks):
    real = [c for c in chks if not c["info"]]
    if any(c["ok"] is False for c in real):
        return "fail"
    return "unknown" if any(c["ok"] is None for c in real) else "pass"


def reasons(chks):
    """Stored rejection reasons, derived from the checklist ([] = covered)."""
    out = []
    for c in chks:
        if c["info"]:
            continue
        if c["ok"] is False:
            out.append(f"{REASON[c['id']]}: {c['value']}" + (f" ({c['limit']})" if c["limit"] else ""))
        elif c["ok"] is None:
            out.append(f"not_checked_yet: {c['label']}")
    return out


def market_ok(m, cfg):
    """Worth profiling: no criterion that needs no profile (type, market cap, volume) fails, and age is not
    known to fail. The profile then settles age, exchanges and links."""
    by = {c["id"]: c["ok"] for c in checks(m, cfg)}
    return by["type"] is not False and by["mcap"] and by["volume"] and by["age"] is not False


def age_days(m, facts, now):
    """Largest known lower bound of the token's age in days (None when nothing is known)."""
    days = [m.get("history_days")] + [(now - ts) / 86400 for ts in (facts.get("genesis_ts"), facts.get("first_price_ts")) if ts]
    days = [d for d in days if d is not None]
    return round(max(days), 1) if days else None


# ---- category exclusions ---------------------------------------------------------------------------------
def category_sets(cfg, cg, store, now):
    """{category id: set of coin ids} for coverage.exclude_categories, refreshed every categories_refresh_hours.
    A category that never loaded is missing from the result (its tokens can't be checked yet)."""
    c, out = cfg["coverage"], {}
    for cat in c["exclude_categories"]:
        key = f"category:{cat}"
        row = store.get_kv(key)
        if row is None or now - row["ts"] >= c["categories_refresh_hours"] * 3600:
            try:
                ids = sorted({x["id"] for x in cg.markets(1, category=cat, sparkline=False) if x.get("id")})
                store.set_kv(key, json.dumps(ids), now)
                store.commit()
                row = {"value": json.dumps(ids), "ts": now}
            except Exception as e:  # keep the previous list; retried next run
                log.warning("coingecko category %s failed: %s", cat, e)
        if row is not None:
            out[cat] = set(json.loads(row["value"]))
    return out


def excluded_by(coin_id, sets, cfg):
    """Excluded categories of one coin, [] when none, None while any category list is missing."""
    if len(sets) < len(cfg["coverage"]["exclude_categories"]):
        hits = [cat for cat, ids in sets.items() if coin_id in ids]
        return hits or None   # a hit is certain even with a list missing; "no hit" is not
    return [cat for cat, ids in sets.items() if coin_id in ids]


# ---- one run ---------------------------------------------------------------------------------------------
def build_universe(cfg, cg, store):
    """({id: markets item}, outage). Pages largest first until market cap drops below scope, + covered tokens."""
    c, universe = cfg["coverage"], {}
    for page in range(1, c["markets_pages_max"] + 1):
        try:
            items = cg.markets(page)
        except Exception as e:
            log.error("coingecko markets page %d failed: %s", page, e)
            if page == 1:
                return {}, True
            break
        for x in items:
            if x.get("id") and coingecko.COIN_ID.match(x["id"]):
                universe[x["id"]] = x
        if not items or (items[-1].get("market_cap") or 0) < c["scope_min_mcap_usd"]:
            break
    missing = [a for a in store.current_members(TIER) if a not in universe and coingecko.COIN_ID.match(a)]
    for i in range(0, len(missing), 250):
        try:
            for x in cg.markets(1, ids=missing[i:i + 250]):
                universe[x["id"]] = x
        except Exception as e:  # covered tokens without data stay covered through the grace period
            log.error("coingecko markets for covered tokens failed: %s", e)
    return universe, False


def close_legacy_members(store, now):
    """One-time switch from the Solana-only list: members keyed by a Solana mint (not a CoinGecko id) are closed
    without an event. Their rows stay in the database."""
    n = 0
    for addr, row in store.current_members(TIER).items():
        if not coingecko.COIN_ID.match(addr):
            store.close_member(row["id"], now)
            n += 1
    if n:
        log.info("closed %d coverage members from the Solana-only list", n)
    return n


def evaluate(cfg, cg, store, evlog, now):
    """One coverage run -> TierRun("coverage"). Profile facts come from token_profiles (profiles job)."""
    c = cfg["coverage"]
    close_legacy_members(store, now)
    universe, outage = build_universe(cfg, cg, store)
    run = TierRun(TIER, outage=outage)
    run.counts["universe"] = len(universe)
    if outage:
        return run
    sets = category_sets(cfg, cg, store, now)
    members = store.current_members(TIER)
    scoped = []
    for x in universe.values():
        m = coingecko.metrics(x, now)
        if m["address"] in members or (m["mcap_usd"] or 0) >= c["scope_min_mcap_usd"]:
            scoped.append((m, x))
        else:
            run.counts["below_scope_mcap"] += 1
    profs = store.profiles(coingecko.CHAIN, [m["address"] for m, _ in scoped])
    for m, x in scoped:
        f = profiles.facts((profs.get(m["address"]) or {}).get("data"), c)
        m = {**m, **{k: v for k, v in f.items() if k not in ("genesis_ts", "first_price_ts")}}
        m["age_d"] = age_days(m, f, now)
        m["excluded_categories"] = excluded_by(m["address"], sets, cfg)
        chks = checks(m, cfg)
        why = reasons(chks)
        m["tier_member"] = not why
        m["coverage_status"] = status(chks)
        evlog.write(m["address"], why, m, tier=TIER)
        run.results.append((m, why))
        pts = coingecko.sparkline(x)
        if pts:   # the table's 7-day line and 7d %, from the same call
            store.save_chart_points(m["address"], 168, pts, now)
    return run

