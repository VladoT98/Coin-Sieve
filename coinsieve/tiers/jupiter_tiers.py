"""Emerging + Established tiers, both evaluated from one Jupiter token universe.

Scope (evaluated + logged): token inside the tier's age range with mcap >= scope_min_mcap_usd.
Everything else is only counted — evaluating all ~3,900 tokens every run would flood the logs.
"""
import logging
from datetime import datetime

from coinsieve.tiers import TierRun

log = logging.getLogger(__name__)

TIERS = ("emerging", "established")


def build_universe(jup, store, cfg):
    """Union of the configured Jupiter lists + every token ever tracked in any tier."""
    universe, failures = {}, 0
    for path in cfg["jupiter"]["universe"]:
        try:
            for t in jup.tokens(path):
                universe[t["id"]] = t
        except Exception as e:
            failures += 1
            log.error("jupiter %s failed: %s", path, e)
    tracked = [a for a in store.all_tracked() if a not in universe]
    if tracked:
        try:
            for t in jup.search(tracked):
                universe[t["id"]] = t
        except Exception as e:
            log.error("jupiter search for tracked tokens failed: %s", e)
    return universe, failures == len(cfg["jupiter"]["universe"])


def jup_metrics(t, now):
    s24 = t.get("stats24h") or {}
    audit = t.get("audit") or {}
    created = (t.get("firstPool") or {}).get("createdAt")
    age_d = (now - datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) / 86400 if created else None
    vol = (s24.get("buyVolume") or 0) + (s24.get("sellVolume") or 0)
    org_vol = (s24.get("buyOrganicVolume") or 0) + (s24.get("sellOrganicVolume") or 0)
    return {
        "address": t["id"],
        "symbol": t.get("symbol"),
        "name": t.get("name"),
        "age_d": round(age_d, 1) if age_d is not None else None,
        "mcap_usd": t.get("mcap"),
        "fdv_usd": t.get("fdv"),
        "liquidity_usd": t.get("liquidity"),
        "volume_h24_usd": round(vol, 2),
        "organic_volume_pct": round(org_vol / vol * 100, 1) if vol else None,
        "holders": t.get("holderCount"),
        "holder_change_24h_pct": s24.get("holderChange"),
        "organic_score": t.get("organicScore"),
        # audit keys are omitted when false/unknown -> treat missing as not disabled (strict)
        "mint_authority_disabled": audit.get("mintAuthorityDisabled") is True,
        "freeze_authority_disabled": audit.get("freezeAuthorityDisabled") is True,
        "top_holders_pct": audit.get("topHoldersPercentage"),
        "dev_mints": audit.get("devMints"),
        "tags": t.get("tags") or [],
        "url": f"https://dexscreener.com/solana/{t['id']}",
    }


def in_age_range(age_d, c):
    return age_d is not None and age_d >= c["min_age_days"] and (c["max_age_days"] is None or age_d <= c["max_age_days"])


def evaluate(m, c, excl):
    """All rejection reasons for one token against one tier's criteria; [] = member."""
    reasons = []
    if m["address"] in excl["mints"]:
        reasons.append("excluded_mint: in config exclude_mints")
    bad_tags = sorted(set(m["tags"]) & excl["tags"])
    if bad_tags:
        reasons.append(f"excluded_tag: {', '.join(bad_tags)}")

    def need_min(field, key, label):
        v = m[field]
        if v is None:
            reasons.append(f"missing_{field}")
        elif v < c[key]:
            reasons.append(f"{label}: {v:,.0f} < {c[key]:,}")

    need_min("mcap_usd", "min_mcap_usd", "low_mcap")
    if c["max_mcap_usd"] is not None and (m["mcap_usd"] or 0) > c["max_mcap_usd"]:
        reasons.append(f"high_mcap: {m['mcap_usd']:,.0f} > {c['max_mcap_usd']:,}")
    need_min("liquidity_usd", "min_liquidity_usd", "low_liquidity")
    need_min("volume_h24_usd", "min_volume_h24_usd", "low_volume")
    need_min("holders", "min_holders", "few_holders")
    need_min("organic_score", "min_organic_score", "low_organic_score")
    if c["require_mint_authority_disabled"] and not m["mint_authority_disabled"]:
        reasons.append("mint_authority_not_disabled")
    if c["require_freeze_authority_disabled"] and not m["freeze_authority_disabled"]:
        reasons.append("freeze_authority_not_disabled")
    if c["max_top_holders_pct"] is not None:
        if m["top_holders_pct"] is None:
            reasons.append("missing_top_holders_pct")
        elif m["top_holders_pct"] > c["max_top_holders_pct"]:
            reasons.append(f"top_holders: {m['top_holders_pct']:.1f}% > {c['max_top_holders_pct']}%")
    return reasons


def evaluate_tiers(cfg, jup, store, evlog, now, tiers=TIERS):
    universe, outage = build_universe(jup, store, cfg)
    excl = {"tags": set(cfg["jupiter"]["exclude_tags"]), "mints": set(cfg["jupiter"]["exclude_mints"])}
    metrics = [jup_metrics(t, now) for t in universe.values()]
    runs = []
    for tier in tiers:
        c = cfg["tiers"][tier]
        run = TierRun(tier, outage=outage)
        run.counts["universe"] = len(universe)
        if outage:
            runs.append(run)
            continue
        members = store.current_members(tier)
        for m in metrics:
            if not in_age_range(m["age_d"], c):
                if m["address"] in members and m["age_d"] is not None and c["max_age_days"] is not None \
                        and m["age_d"] > c["max_age_days"]:
                    run.aged_out.add(m["address"])
                continue
            if (m["mcap_usd"] or 0) < c["scope_min_mcap_usd"] and m["address"] not in members:
                run.counts["below_scope_mcap"] += 1
                continue
            reasons = evaluate(m, c, excl)
            m = {**m, "tier_member": not reasons}
            evlog.write(m["address"], reasons, m, tier=tier)
            run.results.append((m, reasons))
        runs.append(run)
    return runs
