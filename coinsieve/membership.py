"""Tier membership: turn one run's passing set into entered / left / graduated events."""


def update(store, run, now, tier_cfg, graduation):
    """Apply one TierRun to tier_members. Returns the events created (list of dicts).

    - entered:   passing now, not a current member.
    - left:      current member that hasn't passed for >= leave_after_hours (hysteresis),
                 or aged out of the tier's age range (immediate, detail says so).
    - graduated: entered `graduation.to` after ever being a member of `graduation.from`.
    Skipped entirely on an outage run: no data is not evidence of failing.
    """
    if run.outage:
        return []
    tier, events = run.tier, []
    members = store.current_members(tier)
    by_addr = {m["address"]: (m, reasons) for m, reasons in run.results}
    passing = run.passing()

    def event(addr, kind, symbol, detail=None):
        store.add_event(now, addr, tier, kind, symbol, detail)
        events.append({"address": addr, "tier": tier, "kind": kind, "symbol": symbol, "detail": detail})

    for addr in sorted(passing):
        symbol = by_addr[addr][0].get("symbol")
        if addr in members:
            store.touch_member(members[addr]["id"], symbol, now)
            continue
        store.add_member(addr, tier, symbol, now)
        event(addr, "entered", symbol)
        if graduation and tier == graduation["to"] and store.was_ever_member(addr, graduation["from"]):
            event(addr, "graduated", symbol, f"from {graduation['from']}")

    leave_s = tier_cfg["leave_after_hours"] * 3600
    for addr, row in members.items():
        if addr in passing:
            continue
        if addr in run.aged_out:
            detail = "aged_out"
        elif now - row["last_pass_at"] >= leave_s:
            reasons = by_addr.get(addr, (None, ["not evaluated this run"]))[1]
            detail = "; ".join(reasons) or "failed tier criteria"
        else:
            continue  # still inside the grace period
        store.close_member(row["id"], now)
        event(addr, "left", row["symbol"], detail)
    store.commit()
    return events
