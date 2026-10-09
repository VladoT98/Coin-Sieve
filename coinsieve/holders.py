"""Top-holder analysis for covered tokens: who holds the supply, and what share the 10 largest hold.

Source: RugCheck report `topHolders` (top 20 token accounts: address, owner, pct of supply) + `knownAccounts`
(types seen 2026-10-09: AMM, LOCKER — no exchange labels) + `markets` (pool vaults). The owner wallet of each
holder is looked up on-chain (Solana RPC getMultipleAccounts): an owner account controlled by a program other
than the System Program is a contract (locks, staking/voting escrows, vaults). Exchange wallets only come from
the curated `holders.exchange_wallets` config list (each entry needs a source) — never guessed.

Kinds: pool, locker, contract, exchange, wallet (unlabelled — can be a person, a team multisig or an exchange
we can't identify). The kinds in `holders.exclude_kinds` are left out of "top 10 (excl.)".
"""
import logging

from coinsieve.rugcheck import _pool_accounts
from coinsieve.solana_rpc import SYSTEM_PROGRAM

log = logging.getLogger(__name__)

# Owner accounts held by these programs are not contracts holding tokens (seen 2026-10-09: RugCheck reports some
# owners that are themselves token accounts) -> treated as unidentified.
NOT_CONTRACTS = {SYSTEM_PROGRAM, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}


def owners_of(report):
    """Distinct holder owner wallets in a RugCheck report (input for the RPC lookup)."""
    return sorted({h.get("owner") for h in report.get("topHolders") or [] if h.get("owner")})


def analyse(report, accounts, hcfg):
    """Holder breakdown from one RugCheck report and {owner: {"program", "executable"} | None}.

    Returns {"holders": [{owner, pct, kind, label}], "top10_all_pct", "top10_excl_pct",
    "excluded_pct", "complete", "listed"}. complete=False means fewer than 10 non-excluded owners were in
    the top-20 data, so top10_excl_pct is a lower bound.
    """
    pool = _pool_accounts(report)
    known = report.get("knownAccounts") or {}
    exchanges = hcfg.get("exchange_wallets") or {}
    programs = hcfg.get("program_labels") or {}
    excluded_kinds = set(hcfg["exclude_kinds"])

    by_owner = {}
    for h in report.get("topHolders") or []:
        owner = h.get("owner") or h.get("address")
        if not owner or not isinstance(h.get("pct"), (int, float)):
            continue
        e = by_owner.setdefault(owner, {"owner": owner, "pct": 0.0, "accounts": set()})
        e["pct"] += h["pct"]
        e["accounts"].add(h.get("address"))

    rows = []
    for e in by_owner.values():
        owner, acc = e["owner"], accounts.get(e["owner"])
        k = known.get(owner) or {}
        if owner in pool or e["accounts"] & pool:
            kind, label = "pool", k.get("name") or "Trading pool"
        elif k.get("type") == "LOCKER":
            kind, label = "locker", k.get("name") or "Token lock"
        elif owner in exchanges:
            kind, label = "exchange", (exchanges[owner] or {}).get("name") or "Exchange"
        elif acc and acc.get("program") and acc["program"] not in NOT_CONTRACTS:
            kind, label = "contract", programs.get(acc["program"]) or "Program-controlled account"
        else:
            kind, label = "wallet", None
        row = {"owner": owner, "pct": round(e["pct"], 4), "kind": kind, "label": label}
        if kind == "contract":
            row["program"] = acc["program"]
        rows.append(row)
    rows.sort(key=lambda r: -r["pct"])

    kept = [r for r in rows if r["kind"] not in excluded_kinds]
    return {
        "holders": rows,
        "top10_all_pct": round(sum(r["pct"] for r in rows[:10]), 2) if rows else None,
        "top10_excl_pct": round(sum(r["pct"] for r in kept[:10]), 2) if rows else None,
        "excluded_pct": round(sum(r["pct"] for r in rows if r["kind"] in excluded_kinds), 2),
        "complete": len(kept) >= 10,
        "listed": len(report.get("topHolders") or []),
    }


def update(store, cfg, addresses, rug, rpc, now, force=False):
    """Fetch + analyse holders for the due addresses. Returns {"done", "failed"}. A failure keeps the old report."""
    chain, hcfg = cfg["chain"], cfg["holders"]
    old = store.holder_reports(chain, addresses)
    due = [a for a in addresses if force or a not in old or now - old[a]["ts"] >= hcfg["refresh_hours"] * 3600]
    stats = {"due": len(due), "done": 0, "failed": 0}
    for addr in due:
        try:
            report = rug.report(addr)
            if not report.get("topHolders"):
                raise ValueError("RugCheck report has no topHolders")
            accounts = rpc.account_owners(owners_of(report))
            data = analyse(report, accounts, hcfg)
        except Exception as e:  # next run retries; the last good report stays on the site
            log.warning("holders %s failed: %s", addr, e)
            stats["failed"] += 1
            continue
        data["source"] = "rugcheck+solana-rpc"
        # RugCheck states authorities explicitly (null = revoked); Jupiter omits unknown ones
        data["authorities"] = {k: report[f"{k}Authority"] for k in ("mint", "freeze") if f"{k}Authority" in report}
        store.save_holder_report(chain, addr, data, now)
        store.commit()
        stats["done"] += 1
    return stats
