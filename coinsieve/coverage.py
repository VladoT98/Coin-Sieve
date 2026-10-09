"""Coverage list (v3): the established Solana tokens profiled on the site.

From the Jupiter universe (same lists as Emerging/Established): tokens at least `min_age_days` old with market cap
and liquidity above the coverage minimums, not excluded by tag/mint, Solana-native (CoinGecko platform and
categories, from the profiles job) and with a website or docs page found. Unknown facts (profile not fetched
yet, token not on CoinGecko) are "not checked yet" — the token is not covered until they are known.

Membership reuses tier_members / tier_events as tier "coverage" (entered / left with `leave_after_hours`
hysteresis). The checklist below is the single source of truth: the reasons stored per token are derived from it.
"""
import logging

from coinsieve import profiles, site_text
from coinsieve.tiers import TierRun
from coinsieve.tiers.jupiter_tiers import build_universe, jup_metrics

log = logging.getLogger(__name__)

TIER = "coverage"
# check id -> reason code (funnel.REASON_LABELS) when the check fails
REASON = {"age": "too_young", "type": "excluded_type", "mcap": "low_mcap", "liquidity": "low_liquidity",
          "native": "not_native", "links": "no_website_or_docs"}


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


def checks(m, cfg):
    """Coverage checklist for one token's stored metrics (Jupiter metrics + profile facts)."""
    c, j = cfg["coverage"], cfg["jupiter"]
    age = m.get("age_d")
    bad_tags = sorted(set(m.get("tags") or []) & set(j["exclude_tags"]))
    excluded = m["address"] in set(j["exclude_mints"])
    out = [
        _chk("age", age is not None and age >= c["min_age_days"], "–" if age is None else f"{age:,.0f} days",
             f"min {c['min_age_days']:,} days", ["coverage.min_age_days"]),
        _chk("type", not bad_tags and not excluded, ", ".join(bad_tags) or ("excluded asset" if excluded else "project token"),
             ""),
        _chk("mcap", m.get("mcap_usd") is not None and m["mcap_usd"] >= c["min_mcap_usd"], _usd(m.get("mcap_usd")),
             f"min {_usd(c['min_mcap_usd'])}", ["coverage.min_mcap_usd"]),
        _chk("liquidity", m.get("liquidity_usd") is not None and m["liquidity_usd"] >= c["min_liquidity_usd"],
             _usd(m.get("liquidity_usd")), f"min {_usd(c['min_liquidity_usd'])}", ["coverage.min_liquidity_usd"]),
    ]
    req = c["require_native_chain"]
    out.append(_chk("native", m.get("native"), m.get("native_detail") or "not checked yet",
                    "required" if req else "not required", ["coverage.require_native_chain"], info=not req))
    req = c["require_website_or_docs"]
    found = [k for k in m.get("links_found") or [] if k in ("website", "whitepaper", "docs")]
    out.append(_chk("links", m.get("website_or_docs"),
                    ", ".join(found) if found else ("none found" if m.get("profile_checked") else "not checked yet"),
                    "required" if req else "not required", ["coverage.require_website_or_docs"], info=not req))
    for key, flag in (("mint", "mint_authority_disabled"), ("freeze", "freeze_authority_disabled")):
        out.append(_chk(key, bool(m.get(flag)), "revoked" if m.get(flag) else "active", "", info=True))
    return out


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
    """Passes the criteria that need no profile (age, type, mcap, liquidity) — these tokens get profiled."""
    return all(c["ok"] for c in checks(m, cfg)[:4])


def in_scope(m, c, is_member):
    age = m.get("age_d")
    return is_member or (age is not None and age >= c["min_age_days"] and (m.get("mcap_usd") or 0) >= c["scope_min_mcap_usd"])


def evaluate(cfg, jup, store, evlog, now):
    """One coverage run -> TierRun("coverage"). Profile facts come from token_profiles (profiles job)."""
    universe, outage = build_universe(jup, store, cfg)
    run = TierRun(TIER, outage=outage)
    run.counts["universe"] = len(universe)
    if outage:
        return run
    c = cfg["coverage"]
    members = store.current_members(TIER)
    metrics = [jup_metrics(t, now) for t in universe.values()]
    scoped = []
    for m in metrics:
        if in_scope(m, c, m["address"] in members):
            scoped.append(m)
        elif m.get("age_d") is None:
            run.counts["no_age"] += 1
        elif m["age_d"] < c["min_age_days"]:
            run.counts["too_young"] += 1
        else:
            run.counts["below_scope_mcap"] += 1
    profs = store.profiles(cfg["chain"], [m["address"] for m in scoped])
    for m in scoped:
        m = {**m, **profiles.facts((profs.get(m["address"]) or {}).get("data"), c)}
        chks = checks(m, cfg)
        why = reasons(chks)
        m["tier_member"] = not why
        m["coverage_status"] = status(chks)
        evlog.write(m["address"], why, m, tier=TIER)
        run.results.append((m, why))
    return run


def record_holder_snapshots(store, run, now, every_hours):
    """Jupiter holder count of covered tokens, at most once per `every_hours` (holder history chart)."""
    n = 0
    for m, why in run.results:
        if why or m.get("holders") is None:
            continue
        last = store.last_snapshot_ts(m["address"], "jupiter")
        if last is None or now - last >= every_hours * 3600:
            store.add_snapshot(m["address"], now, m["holders"], m.get("liquidity_usd"), m.get("volume_h24_usd"), "jupiter")
            n += 1
    return n
