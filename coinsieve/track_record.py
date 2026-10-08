"""Track record: snapshot every tier entry at fixed checkpoints, then summarise neutrally.

Every entry counts — including tokens that collapsed or vanished (found = 0) — so the record
is not survivorship-biased. Excluded from stats: a tier's initial fill (bootstrap) and entries
whose baseline snapshot was taken too long after entry.
"""
import logging
from statistics import median

log = logging.getLogger(__name__)


def update(store, jup, now, tr):
    """Record every checkpoint that is due and not yet recorded. Returns number recorded."""
    checkpoints = tr["checkpoints_hours"]
    max_h = max(checkpoints.values())
    done = store.recorded_checkpoints()
    due = []
    for e in store.tier_entries(since=now - (max_h + tr["catch_up_hours"]) * 3600):
        for name, hours in checkpoints.items():
            key = (e["address"], e["tier"], e["entered_at"], name)
            target = e["entered_at"] + hours * 3600
            # due, not recorded, and not so late that the snapshot would misrepresent the checkpoint
            if key not in done and target <= now <= target + tr["catch_up_hours"] * 3600:
                due.append((e, name))
    if not due:
        return 0
    try:
        data = {t["id"]: t for t in jup.search(sorted({e["address"] for e, _ in due}))}
    except Exception as ex:  # retry next run; no data is not "token vanished"
        log.error("track record: jupiter search failed: %s", ex)
        return 0
    for e, name in due:
        t = data.get(e["address"])
        store.add_outcome({
            "address": e["address"], "tier": e["tier"], "entered_at": e["entered_at"], "checkpoint": name,
            "ts": now, "found": int(t is not None),
            "price_usd": t.get("usdPrice") if t else None, "mcap_usd": t.get("mcap") if t else None,
            "liquidity_usd": t.get("liquidity") if t else None, "holders": t.get("holderCount") if t else None,
            "tiers_now": ",".join(store.tiers_of(e["address"])),
        })
    store.commit()
    return len(due)


def _pct(new, old):
    return (new - old) / old * 100 if new is not None and old else None


def summarize(rows, checkpoint, bootstrap_end, tr, entered_between=None):
    """Neutral stats for one tier + checkpoint. rows = store.outcomes(tier)."""
    base = {(r["address"], r["entered_at"]): r for r in rows if r["checkpoint"] == "0h"}
    pairs = []
    for r in rows:
        if r["checkpoint"] != checkpoint:
            continue
        b = base.get((r["address"], r["entered_at"]))
        if b is None or not b["found"] or r["entered_at"] <= bootstrap_end:
            continue
        if b["ts"] - r["entered_at"] > tr["baseline_max_delay_hours"] * 3600:
            continue  # baseline too late to be a fair starting point
        if entered_between and not entered_between[0] <= r["entered_at"] < entered_between[1]:
            continue
        pairs.append((b, r))
    n = len(pairs)
    if not n:
        return {"n": 0}
    found = [(b, r) for b, r in pairs if r["found"]]
    price = [p for b, r in found if (p := _pct(r["price_usd"], b["price_usd"])) is not None]
    liq = [p for b, r in found if (p := _pct(r["liquidity_usd"], b["liquidity_usd"])) is not None]
    holders = [p for b, r in found if (p := _pct(r["holders"], b["holders"])) is not None]
    return {
        "n": n,
        "not_found": n - len(found),                       # Jupiter no longer lists it
        "in_a_tier": sum(1 for _, r in pairs if r["tiers_now"]),
        "median_price_change_pct": round(median(price), 1) if price else None,
        "median_liquidity_change_pct": round(median(liq), 1) if liq else None,
        "median_holder_change_pct": round(median(holders), 1) if holders else None,
        "liquidity_down_50pct": sum(1 for p in liq if p <= -50),
        "price_up": sum(1 for p in price if p > 0),
    }
