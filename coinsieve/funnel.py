"""Screening funnel per tier: how many tokens each stage let through, and why the rest were dropped."""
from collections import Counter

# Ranking gates only decide what is postable; they don't remove tier membership -> not funnel reasons.
_RANK_CODES = {"insufficient_history", "vol_liq_ratio", "holder_growth"}

REASON_LABELS = {
    "low_liquidity": "low liquidity", "no_liquidity_data": "no liquidity data", "bonding_curve": "still on launchpad",
    "low_volume": "low volume", "few_txns": "few trades", "few_socials": "no socials listed",
    "no_pair_data": "no market data", "serial_creator": "serial creator", "jupiter_incomplete": "incomplete data",
    "jupiter_failed": "data unavailable", "rugcheck_failed": "RugCheck unavailable",
    "rugcheck_incomplete": "RugCheck incomplete", "rugged": "marked rugged",
    "outside_age_window": "outside age window", "outside_age_range": "outside age range",
    "not_checked_yet": "waiting for next run",
    "mint_authority_active": "mint authority active", "freeze_authority_active": "freeze authority active",
    "lp_not_locked": "liquidity not locked", "top1_holder": "one wallet holds too much",
    "top10_holders": "top wallets hold too much", "risk_danger": "danger flag",
    "excluded_tag": "stablecoin / staking / stock", "excluded_mint": "excluded asset", "low_mcap": "market cap too small",
    "high_mcap": "market cap too large", "few_holders": "too few holders", "low_organic_score": "mostly bot trading",
    "mint_authority_not_disabled": "mint authority active", "freeze_authority_not_disabled": "freeze authority active",
    "top_holders": "top wallets hold too much", "missing_liquidity_usd": "no liquidity data",
    "missing_mcap_usd": "no market cap data", "missing_holders": "no holder data",
    "missing_top_holders_pct": "no holder data", "missing_organic_score": "no trading-quality data",
    # v3 coverage list
    "too_young": "younger than the minimum age", "excluded_type": "stablecoin / staking / stock",
    "not_native": "not issued on Solana", "no_website_or_docs": "no website or docs found",
}


def label(code):
    return REASON_LABELS.get(code, code.replace("_", " "))


def stats(run):
    """Funnel numbers for one TierRun. Reasons = the FIRST failing check of each rejected token."""
    rejected = [r for m, r in run.results if not m.get("tier_member")]
    first = Counter(r[0].split(":")[0] for r in rejected if r and r[0].split(":")[0] not in _RANK_CODES)
    s = {"evaluated": len(run.results),
         "members": sum(1 for m, _ in run.results if m.get("tier_member")),
         "reasons": dict(first.most_common(5))}
    if run.tier == "new_launches":
        s["market_passed"] = sum(1 for m, _ in run.results if m.get("passed_hard_filters"))
    else:
        s["tracked"] = run.counts.get("universe", 0)
    return s


def stages(tier, s, shown):
    """[(label, count)] for the card's funnel bars."""
    if tier == "new_launches":
        return [("checked (6-48h old)", s["evaluated"]), ("passed market filters", s["market_passed"]),
                ("passed creator + RugCheck", s["members"]), ("shown today", shown)]
    return [("tracked", s["tracked"]), ("in age & size range", s["evaluated"]),
            ("passed all filters", s["members"]), ("shown today", shown)]
