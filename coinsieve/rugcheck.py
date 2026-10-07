"""RugCheck report checks (GET /v1/tokens/{mint}/report, no key).

Field names verified against 51 live reports on 2026-10-07:
- mintAuthority / freezeAuthority: null when revoked.
- risks: [{name, value, description, score, level}], level in {warn, danger}.
- markets[].lp: lpLockedPct, lpLockedUSD, baseUSD, quoteUSD. pump_fun / pump_fun_amm pools
  report 100% locked; third-party Meteora/Orca pools typically 0%.
- topHolders[]: {address, owner, pct, insider}. Pool vaults appear here (4-96% of supply)
  and are excluded: matched via market pubkey, vault address (liquidityA/B), vault owner,
  or knownAccounts type AMM. Unidentified holders always count (strict).
"""
from coinsieve.http import ThrottledClient


class RugCheck(ThrottledClient):
    def report(self, mint):
        return self._get(f"tokens/{mint}/report")


def _pool_accounts(report):
    pool = set()
    for m in report.get("markets") or []:
        pool |= {m.get("pubkey"), m.get("liquidityA"), m.get("liquidityB"),
                 (m.get("liquidityAAccount") or {}).get("owner"),
                 (m.get("liquidityBAccount") or {}).get("owner")}
    pool |= {k for k, v in (report.get("knownAccounts") or {}).items() if v.get("type") == "AMM"}
    pool.discard(None)
    return pool


def evaluate_report(report, rc):
    """Return (metrics, reasons). Any missing field -> 'rugcheck_incomplete' (reject)."""
    missing = [k for k in ("mintAuthority", "freezeAuthority", "risks", "topHolders", "markets")
               if k not in report]
    missing += [k for k in ("topHolders", "markets") if k in report and not report[k]]
    lps = [m.get("lp") for m in report.get("markets") or []]
    if report.get("markets") and not all(lps):
        missing.append("markets[].lp")
    if missing:
        return {}, [f"rugcheck_incomplete: missing {', '.join(sorted(set(missing)))}"]

    pool = _pool_accounts(report)
    holders = sorted((h for h in report["topHolders"]
                      if h.get("address") not in pool and h.get("owner") not in pool),
                     key=lambda h: h.get("pct") or 0, reverse=True)
    total_liq = sum((lp.get("baseUSD") or 0) + (lp.get("quoteUSD") or 0) for lp in lps)
    locked_liq = sum(lp.get("lpLockedUSD") or 0 for lp in lps)
    risks = report["risks"] or []

    m = {
        "rc_score_normalised": report.get("score_normalised"),
        "rc_total_holders": report.get("totalHolders"),
        "rc_top1_holder_pct": round(holders[0]["pct"], 2) if holders else 0.0,
        "rc_top10_holders_pct": round(sum(h["pct"] for h in holders[:10]), 2),
        "rc_lp_locked_pct": round(locked_liq / total_liq * 100, 1) if total_liq else None,
        "rc_total_liquidity_usd": round(total_liq, 2),
        "rc_markets": len(lps),
        "rc_risks": [f"{r.get('level')}:{r.get('name')}" for r in risks],
        "rc_graph_insiders": report.get("graphInsidersDetected"),
        "rc_launchpad": (report.get("launchpad") or {}).get("platform"),
    }

    reasons = []
    if report.get("rugged"):
        reasons.append("rugged: RugCheck marks token as rugged")
    if report["mintAuthority"] is not None:
        reasons.append("mint_authority_active")
    if report["freezeAuthority"] is not None:
        reasons.append("freeze_authority_active")
    if m["rc_lp_locked_pct"] is None:
        reasons.append("rugcheck_incomplete: zero total market liquidity")
    elif m["rc_lp_locked_pct"] < rc["min_lp_locked_pct"]:
        reasons.append(f"lp_not_locked: {m['rc_lp_locked_pct']}% < {rc['min_lp_locked_pct']}%")
    if m["rc_top1_holder_pct"] > rc["max_top1_holder_pct"]:
        reasons.append(f"top1_holder: {m['rc_top1_holder_pct']}% > {rc['max_top1_holder_pct']}%")
    if m["rc_top10_holders_pct"] > rc["max_top10_holders_pct"]:
        reasons.append(f"top10_holders: {m['rc_top10_holders_pct']}% > {rc['max_top10_holders_pct']}%")
    for r in risks:
        if r.get("level") in rc["reject_risk_levels"]:
            reasons.append(f"risk_{r.get('level')}: {r.get('name')} ({r.get('value')})")
    return m, reasons
