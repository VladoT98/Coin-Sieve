"""Stage 3 gates + ranking for tokens that passed hard filters and RugCheck."""


def holder_growth(snaps, now):
    """(holders gained per hour, history span in hours) from snapshots (oldest first)."""
    if not snaps:
        return None, 0.0
    span_h = (now - snaps[0]["ts"]) / 3600
    if span_h <= 0 or len(snaps) < 2:
        return None, span_h
    return (snaps[-1]["holders"] - snaps[0]["holders"]) / span_h, span_h


def rank_gate(m, snaps, now, rk):
    """Add ranking fields to metrics m; return rejection reasons ([] = eligible)."""
    reasons = []
    liq, vol = m.get("liquidity_usd") or 0, m.get("volume_h24_usd") or 0
    ratio = vol / liq if liq else None
    growth, span_h = holder_growth(snaps, now)
    m["vol_liq_ratio"] = round(ratio, 2) if ratio is not None else None
    m["holder_growth_per_h"] = round(growth, 1) if growth is not None else None
    m["history_h"] = round(span_h, 2)

    if ratio is None or not rk["min_vol_liq_ratio"] <= ratio <= rk["max_vol_liq_ratio"]:
        reasons.append(f"vol_liq_ratio: {m['vol_liq_ratio']} outside "
                       f"[{rk['min_vol_liq_ratio']}, {rk['max_vol_liq_ratio']}]")
    if span_h < rk["min_history_hours"] or growth is None:
        reasons.append(f"insufficient_history: {span_h:.2f}h < {rk['min_history_hours']}h")
    elif growth < rk["min_holder_growth_per_hour"]:
        reasons.append(f"holder_growth: {growth:.1f}/h < {rk['min_holder_growth_per_hour']}/h")
    return reasons


def sort_key(m):
    # Highest holder growth first; tie-break on the lower volume/liquidity ratio.
    return (-(m["holder_growth_per_h"] or 0), m["vol_liq_ratio"] or 0)
