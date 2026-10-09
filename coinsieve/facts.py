"""'Points to check' on the token page: facts from our own data that a reader may want to look at.

Each point is shown only when the data exists and crosses a threshold in config `facts:`, always with its number,
never with a verdict. The wording lives in site_text.yaml (`points:`); this module returns ids + values.
"""


def _pct(v):
    return f"{v:.1f}%" if v < 10 else f"{v:.0f}%"


def _usd(v):
    for div, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:,.1f}{s}"
    return f"${v:,.0f}"


def points(m, holders, supply, links_found, profile_checked, holder_history, cfg):
    """[{"id", "vars"}] for one token. m = coverage metrics; holders = holders_view(...) or None;
    supply = profile supply dict or None; holder_history = [[ts, holders], ...]."""
    f, out = cfg["facts"], []
    if holders and holders.get("top10_excl_pct") is not None and holders["top10_excl_pct"] >= f["top10_pct"]:
        out.append({"id": "top10", "vars": {"pct": ("" if holders.get("complete") else "≥") + _pct(holders["top10_excl_pct"])}})
    if holders and (holders.get("excluded_pct") or 0) >= f["contracts_pct"]:
        out.append({"id": "contracts", "vars": {"pct": _pct(holders["excluded_pct"])}})
    circ = m.get("circ_supply")
    cap = (supply or {}).get("max") or m.get("total_supply")
    if circ and cap and cap > 0 and circ / cap * 100 < f["circulating_below_pct"]:
        out.append({"id": "circulating", "vars": {"pct": _pct(circ / cap * 100),
                                                  "of": "max" if (supply or {}).get("max") else "total"}})
    auth = (holders or {}).get("authorities") or {}   # RugCheck: null = revoked; missing = unknown -> no claim
    for k in ("mint", "freeze"):
        if k in auth and auth[k] is not None:
            out.append({"id": k, "vars": {}})
    if profile_checked and not ({"whitepaper", "docs"} & set(links_found or [])):
        out.append({"id": "no_docs", "vars": {}})
    liq, mcap = m.get("liquidity_usd"), m.get("mcap_usd")
    if liq is not None and mcap and liq / mcap * 100 < f["liquidity_below_pct_of_mcap"]:
        out.append({"id": "liquidity", "vars": {"liq": _usd(liq), "pct": f"{liq / mcap * 100:.2f}%"}})
    hist = [p for p in holder_history or [] if p[1]]
    if len(hist) >= 2:
        last_ts, last = hist[-1]
        old = [p for p in hist if p[0] <= last_ts - f["holders_drop_days"] * 86400]
        if old:
            change = (last / old[-1][1] - 1) * 100
            if change <= -f["holders_drop_pct"]:
                out.append({"id": "holders_down", "vars": {"pct": _pct(-change), "days": f["holders_drop_days"]}})
    return out
