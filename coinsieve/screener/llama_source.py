"""DefiLlama (no key) -> TVL, fees, revenue, hacks per CoinGecko id. Verified 2026-10-12 (docs/api_samples/dl_*.json).

Matching (no guessing by name):
- /lite/protocols2: `protocols` (`defillamaId`, `geckoId`, `parentProtocol`, `tvl`, `tvlPrevWeek`, `tvlPrevMonth`)
  + `parentProtocols` (`id` = "parent#aave", `gecko_id`). Big protocols carry their gecko id ONLY on the parent
  (Uniswap, Chainlink, Hyperliquid, Ethena, Pendle, PancakeSwap), some only on one child (Aave V2 = "aave").
  A coin's group = all children of that parent (children tagged with a DIFFERENT gecko id are left out); a
  protocol without a parent is its own group.
- /protocols: only for page slugs (`id` -> `slug`); lite has none.
- /overview/fees (fees and revenue): no gecko id; `defillamaId` = lite `defillamaId`. Chains appear with
  `protocolType: chain` and are matched to /v2/chains by name.
- Chain coins get `chain_tvl_usd` + `chain_fees_*` / `chain_revenue_*` (network usage), never mixed into the
  protocol `fees_*` (protocol cash flow); scoring treats them differently.
- /v2/chains: `gecko_id` per chain; one gecko id can have several rows (BNB: "BSC" $5.6B and "Binance" $0) -> the
  row with the highest TVL wins.
- /hacks: `defillamaId` -> group members.
"""
from collections import defaultdict

FEES = "overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true&dataType={kind}"


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _sum(values):
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def protocol_groups(lite, slugs):
    """{gecko_id: {"ids": set(str), "slug": str, "members": [lite protocol]}}"""
    protocols = [p for p in lite.get("protocols") or [] if isinstance(p, dict)]
    by_parent = defaultdict(list)
    for p in protocols:
        if p.get("parentProtocol"):
            by_parent[p["parentProtocol"]].append(p)
    parents = [pp for pp in lite.get("parentProtocols") or [] if isinstance(pp, dict) and pp.get("id")]
    parent_gecko = {pp["id"]: pp.get("gecko_id") for pp in parents}
    parent_url = {pp["id"]: pp.get("url") for pp in parents}
    groups = {}

    def add(gid, members, slug, url=None):
        g = groups.setdefault(gid, {"ids": set(), "slug": slug, "members": [], "url": None})
        g["url"] = g["url"] or url or next((m.get("url") for m in members if m.get("url")), None)
        for m in members:
            if str(m.get("defillamaId")) not in g["ids"]:
                g["ids"].add(str(m.get("defillamaId")))
                g["members"].append(m)

    def children(parent, gid):
        return [m for m in by_parent[parent] if not m.get("geckoId") or m.get("geckoId") == gid]

    for parent, gid in parent_gecko.items():
        if gid:
            add(gid, children(parent, gid), parent.split("#", 1)[-1], parent_url.get(parent))
    for p in protocols:
        gid, parent = p.get("geckoId"), p.get("parentProtocol")
        if not gid:
            continue
        if parent and parent_gecko.get(parent) in (None, gid):
            add(gid, children(parent, gid), parent.split("#", 1)[-1], parent_url.get(parent))
        else:
            add(gid, [p], slugs.get(str(p.get("defillamaId"))) or str(p.get("defillamaId")))
    return groups


def _change(members, now_key, prev_key):
    pairs = [(_num(p.get(now_key)), _num(p.get(prev_key))) for p in members]
    pairs = [(a, b) for a, b in pairs if a is not None and b]
    if not pairs:
        return None
    return (sum(a for a, _ in pairs) / sum(b for _, b in pairs) - 1) * 100


def coin_metrics(cg_ids, lite, slugs, chains, fees, revenue, hacks):
    """{cg_id: [(metrics, source_url), ...]}: a protocol part and/or a chain part, each with its own page."""
    groups = protocol_groups(lite, slugs)
    chain_by_gecko = {}
    for c in chains:
        gid = c.get("gecko_id")
        if gid and (_num(c.get("tvl")) or 0) >= (_num((chain_by_gecko.get(gid) or {}).get("tvl")) or -1):
            chain_by_gecko[gid] = c
    out = {}
    for cid in cg_ids:
        g, chain = groups.get(cid), chain_by_gecko.get(cid)
        parts = []
        if g:
            p = {"protocol_tvl_usd": _sum(_num(x.get("tvl")) for x in g["members"]),
                 "protocol_tvl_change_7d_pct": _change(g["members"], "tvl", "tvlPrevWeek"),
                 "protocol_tvl_change_30d_pct": _change(g["members"], "tvl", "tvlPrevMonth"),
                 "defillama_category": next((x.get("category") for x in g["members"] if x.get("category")), None),
                 "defillama_group": g["slug"],
                 "defillama_website": g["url"] if isinstance(g["url"], str) and g["url"].startswith("http")
                 else None}
            proto_fees = _fee_metrics([r for r in fees if str(r.get("defillamaId")) in g["ids"]],
                                      [r for r in revenue if str(r.get("defillamaId")) in g["ids"]])
            hits = [h for h in hacks if str(h.get("defillamaId")) in g["ids"]]
            p.update(hacks_count=float(len(hits)), hacks_amount_usd=_sum(_num(h.get("amount")) for h in hits) or 0.0,
                     last_hack_ts=max((_num(h.get("date")) or 0) for h in hits) if hits else None)
            # A group without TVL or fees (e.g. a foundation treasury entry) only contributes when no chain exists
            if p["protocol_tvl_usd"] or proto_fees or not chain:
                p.update(proto_fees or {})
                parts.append((p, f"https://defillama.com/protocol/{g['slug']}"))
        if chain:   # network usage of the coin's own chain, kept apart from protocol fees (chain_fees_*)
            c = {"chain_tvl_usd": _num(chain.get("tvl"))}
            is_chain = lambda r: r.get("protocolType") == "chain" and r.get("name") == chain.get("name")  # noqa: E731
            c.update(_fee_metrics([r for r in fees if is_chain(r)], [r for r in revenue if is_chain(r)], "chain_")
                     or {})
            parts.append((c, f"https://defillama.com/chain/{chain.get('name')}"))
        if parts:
            out[cid] = parts
    return out


def _fee_metrics(fee_rows, rev_rows, prefix=""):
    """<prefix>fees_/revenue_ {24h,7d,30d,1y,prev30d}_usd summed over matched rows; None when nothing matched."""
    if not fee_rows and not rev_rows:
        return None
    m = {}
    for kind, rows in (("fees", fee_rows), ("revenue", rev_rows)):
        for field, key in (("24h", "total24h"), ("7d", "total7d"), ("30d", "total30d"), ("1y", "total1y"),
                           ("prev30d", "total60dto30d")):
            m[f"{prefix}{kind}_{field}_usd"] = _sum(_num(r.get(key)) for r in rows) if rows else None
    return m


def fetch(api, ttl_hours):
    """The daily lists -> (lite, slugs, chains, fees, revenue, hacks). Raises when the lite list or chains fail;
    slugs/fees/revenue/hacks fall back to empty."""
    lite = api.get("lite/protocols2", ttl_hours=ttl_hours)
    chains = api.get("v2/chains", ttl_hours=ttl_hours)
    if not lite.ok or not isinstance(lite.data, dict) or not chains.ok:
        raise RuntimeError(f"DefiLlama lite/protocols2 HTTP {lite.status} / chains HTTP {chains.status}")
    full = api.get("protocols", ttl_hours=ttl_hours)
    slugs = ({str(p.get("id")): p.get("slug") for p in full.data or [] if isinstance(p, dict) and p.get("slug")}
             if full.ok and isinstance(full.data, list) else {})
    res = {}
    for kind in ("dailyFees", "dailyRevenue"):
        r = api.get(FEES.format(kind=kind), ttl_hours=ttl_hours)
        res[kind] = ((r.data or {}).get("protocols") or []) if r.ok and isinstance(r.data, dict) else []
    hacks = api.get("hacks", ttl_hours=ttl_hours)
    return (lite.data, slugs, chains.data or [], res["dailyFees"], res["dailyRevenue"],
            hacks.data if hacks.ok and isinstance(hacks.data, list) else [])
