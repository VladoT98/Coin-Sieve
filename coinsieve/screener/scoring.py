"""Quality score (Phase 2). Pure functions: no I/O, everything configurable in `screener.scoring`.

1. derive(): raw observations (metric_values) + daily prices -> scored metrics and info metrics, each with the
   source URL of the data it is computed from.
2. percentiles(): rank of each coin among the coins that have the value (ties share the average rank), scaled
   0-100; direction "lower" flips it. Ranks ignore how far an outlier is from the rest, so a wash-traded volume
   can't dominate - no extra capping needed.
3. score(): category = weighted mean of the percentiles a coin HAS (missing metrics are skipped, the others
   re-weighted); fewer than min_metrics_per_category -> 50 + flag. Total = sum of category x weight / 100.
4. Hard filters decide pass / fail; passing coins are ranked by total (ties: larger market cap first).
Scores are relative: a coin's score can move when other coins change even if its own data did not.
"""
import math
from datetime import date as _date

# derived metric -> raw metric whose source URL it carries
SOURCE_OF = {
    "major_volume_mcap": "major_volume_24h_usd", "depth_mcap": "depth_2pct_usd", "spread_pct": "spread_pct",
    "exchanges_count": "exchanges_count", "major_exchanges_count": "major_exchanges_count",
    "fees_mcap": "fees_30d_usd", "revenue_mcap": "revenue_30d_usd", "fees_trend": "fees_30d_usd",
    "tvl_change_30d": "protocol_tvl_change_30d_pct", "commits_90d": "commits_90d",
    "contributors_90d": "contributors_90d", "active_weeks_1y": "active_weeks_1y",
    "days_since_push": "days_since_push", "circulating_ratio": "circ_supply", "fdv_mcap": "fdv_usd",
    "rel_change_30d": "price_change_30d_pct", "rel_change_200d": "price_change_200d_pct",
    "ath_distance": "ath_change_pct", "watchlist_mcap": "watchlist_users",
}


def _v(raw, key):
    row = raw.get(key)
    if row is None:
        return None
    v = row["value"] if isinstance(row, dict) else row
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _ratio(a, b):
    return a / b if a is not None and b else None


def _days(d1, d2):
    return (_date.fromisoformat(d1) - _date.fromisoformat(d2)).days


def price_at(prices, target, tolerance=3):
    """(price, mcap) of the stored day closest to `target` within +-tolerance days, else None."""
    best = None
    for d, p, m in prices:
        gap = abs(_days(d, target))
        if gap <= tolerance and p and m and (best is None or gap < best[0]):
            best = (gap, p, m)
    return best[1:] if best else None


def supply_change(prices, as_of, days):
    """% change of implied circulating supply (market cap / price) over `days`."""
    now = price_at(prices, as_of, 1)
    then = price_at(prices, _date.fromordinal(_date.fromisoformat(as_of).toordinal() - days).isoformat())
    if not now or not then:
        return None
    return (now[1] / now[0]) / (then[1] / then[0]) * 100 - 100


def rsi(closes, period=14):
    """Wilder's RSI of daily closes (oldest first); None with fewer than period + 1 closes."""
    if len(closes) < period + 1:
        return None
    gains = [max(b - a, 0) for a, b in zip(closes, closes[1:])]
    losses = [max(a - b, 0) for a, b in zip(closes, closes[1:])]
    avg_g, avg_l = sum(gains[:period]) / period, sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g, avg_l = (avg_g * (period - 1) + g) / period, (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def volatility(closes, days=90):
    """Annualised standard deviation of daily log returns over the last `days` closes, in %."""
    c = [x for x in closes[-(days + 1):] if x and x > 0]
    if len(c) < days // 2:
        return None
    r = [math.log(b / a) for a, b in zip(c, c[1:])]
    mean = sum(r) / len(r)
    return math.sqrt(sum((x - mean) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(365) * 100


def derive(raw, prices, btc_raw, as_of):
    """raw: {metric: {"value", "text", "source_url"}} (latest observations); prices: [(date, price, mcap)] oldest
    first. Returns ({metric: value}, {metric: source_url}, facts) - facts feed the filters."""
    mcap = _v(raw, "mcap_usd")
    tvl = max([t for t in (_v(raw, "protocol_tvl_usd"), _v(raw, "chain_tvl_usd")) if t is not None], default=None)
    supply_cap = _v(raw, "max_supply") or _v(raw, "total_supply")
    fees, prev = _v(raw, "fees_30d_usd"), _v(raw, "fees_prev30d_usd")
    closes = [p for _, p, _ in prices if p]

    def rel(key):
        a, b = _v(raw, key), _v(btc_raw, key)
        return a - b if a is not None and b is not None else None

    m = {
        "major_volume_mcap": _ratio(_v(raw, "major_volume_24h_usd"), mcap),
        "depth_mcap": _ratio(_v(raw, "depth_2pct_usd"), mcap),
        "spread_pct": _v(raw, "spread_pct"),
        "exchanges_count": _v(raw, "exchanges_count"),
        "major_exchanges_count": _v(raw, "major_exchanges_count"),
        "tvl_mcap": _ratio(tvl, mcap),
        "fees_mcap": _ratio(fees * 12, mcap) if fees is not None else None,
        "revenue_mcap": _ratio(_v(raw, "revenue_30d_usd") * 12, mcap) if _v(raw, "revenue_30d_usd") is not None
        else None,
        "fees_trend": fees / prev if fees is not None and prev else None,
        "tvl_change_30d": _v(raw, "protocol_tvl_change_30d_pct"),
        "commits_90d": _v(raw, "commits_90d"),
        "contributors_90d": _v(raw, "contributors_90d"),
        "active_weeks_1y": _v(raw, "active_weeks_1y"),
        "days_since_push": _v(raw, "days_since_push"),
        "circulating_ratio": min(_v(raw, "circ_supply") / supply_cap, 1.0)
        if _v(raw, "circ_supply") is not None and supply_cap else None,
        "fdv_mcap": _ratio(_v(raw, "fdv_usd"), mcap),
        "supply_change_30d": supply_change(prices, as_of, 30),
        "supply_change_1y": supply_change(prices, as_of, 365),
        "rel_change_30d": rel("price_change_30d_pct"),
        "rel_change_200d": rel("price_change_200d_pct"),
        "ath_distance": _v(raw, "ath_change_pct"),
        # info only
        "rsi_14": rsi(closes[-120:]),
        "volatility_90d": volatility(closes),
        "watchlist_mcap": _ratio(_v(raw, "watchlist_users"), mcap / 1e6 if mcap else None),
    }
    src = {k: (raw.get(SOURCE_OF.get(k, "mcap_usd")) or raw.get("mcap_usd") or {}).get("source_url")
           for k, v in m.items() if v is not None}
    for k in ("tvl_mcap",):
        if m[k] is not None:
            src[k] = (raw.get("protocol_tvl_usd") if tvl == _v(raw, "protocol_tvl_usd") else
                      raw.get("chain_tvl_usd") or {}).get("source_url")
    for k in ("supply_change_30d", "supply_change_1y", "rsi_14", "volatility_90d"):
        if m[k] is not None:
            src[k] = (raw.get("price_usd") or {}).get("source_url")   # computed from stored CoinGecko prices
    starts = [t for t in (_v(raw, "ath_ts"), _v(raw, "atl_ts"), _v(raw, "genesis_ts")) if t]
    first_price = prices[0][0] if prices else None
    facts = {
        "mcap": mcap, "major_volume": _v(raw, "major_volume_24h_usd"),
        "exchanges": _v(raw, "exchanges_count"),
        "website_or_docs": bool(_v(raw, "has_website") or _v(raw, "has_whitepaper"))
        if "has_website" in raw or "has_whitepaper" in raw else None,
        "starts": starts, "first_price": first_price,
    }
    return m, src, facts


def age_days(facts, now_ts, as_of):
    candidates = [(now_ts - t) / 86400 for t in facts["starts"]]
    if facts["first_price"]:
        candidates.append(_days(as_of, facts["first_price"]))
    return max(candidates) if candidates else None


def percentiles(values, direction="higher"):
    """{id: value} -> {id: 0..100}. Ties share their average rank."""
    items = sorted(((v, k) for k, v in values.items() if v is not None), key=lambda x: x[0])
    n = len(items)
    out, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][0] == items[i][0]:
            j += 1
        avg_rank = (i + j) / 2                         # 0-based
        pct = 50.0 if n == 1 else avg_rank / (n - 1) * 100
        for k in range(i, j + 1):
            out[items[k][1]] = 100 - pct if direction == "lower" else pct
        i = j + 1
    return out


def validate(scfg):
    """Config errors as a list of strings (empty = OK)."""
    errs = []
    cats = scfg["categories"]
    if abs(sum(cats.values()) - 100) > 1e-6:
        errs.append(f"category weights add up to {sum(cats.values())}, not 100")
    for mid, m in scfg["metrics"].items():
        if m.get("category") not in cats:
            errs.append(f"metric {mid}: unknown category {m.get('category')}")
        if m.get("direction") not in ("higher", "lower"):
            errs.append(f"metric {mid}: direction must be higher/lower")
        if mid not in SOURCE_OF and mid not in ("tvl_mcap", "supply_change_30d", "supply_change_1y"):
            errs.append(f"metric {mid}: no formula in scoring.py")
    for cat in cats:
        if not any(m.get("category") == cat for m in scfg["metrics"].values()):
            errs.append(f"category {cat} has no metrics")
    return errs


def score(coins, scfg, weights=None):
    """coins: {cg_id: {"metrics": {...}, "passes": bool}}. Returns {cg_id: {"total", "categories", "pct"}}.
    weights: optional category weights (sensitivity checks); default scfg["categories"]."""
    weights = weights or scfg["categories"]
    mdefs = scfg["metrics"]
    pct = {}
    for mid, d in mdefs.items():
        vals = {cid: c["metrics"].get(mid) for cid, c in coins.items()}
        if sum(1 for v in vals.values() if v is not None) >= scfg["min_coins_per_metric"]:
            pct[mid] = percentiles(vals, d["direction"])
    out = {}
    for cid in coins:
        cats = {}
        for cat in weights:
            used = [(pct[mid][cid], d["weight"]) for mid, d in mdefs.items()
                    if d["category"] == cat and mid in pct and cid in pct[mid]]
            n_total = sum(1 for d in mdefs.values() if d["category"] == cat)
            if len(used) >= scfg["min_metrics_per_category"] and sum(w for _, w in used) > 0:
                cats[cat] = {"score": sum(p * w for p, w in used) / sum(w for _, w in used),
                             "used": len(used), "of": n_total, "flag": None}
            else:
                cats[cat] = {"score": 50.0, "used": len(used), "of": n_total, "flag": "not enough data"}
        total = sum(cats[c]["score"] * w for c, w in weights.items()) / sum(weights.values())
        out[cid] = {"total": total, "categories": cats,
                    "pct": {mid: pct[mid][cid] for mid in pct if cid in pct[mid]}}
    return out


def filter_reasons(facts, metrics, scfg, now_ts, as_of):
    """Failed hard filters as short reason strings ([] = passes)."""
    f = scfg["filters"]
    out = []
    age = age_days(facts, now_ts, as_of)
    if age is None or age < f["min_age_days"]:
        out.append(f"age {age:.0f} d < {f['min_age_days']} d" if age is not None else "age unknown")
    if (facts["major_volume"] or 0) < f["min_major_volume_24h_usd"]:
        out.append(f"major-exchange volume < ${f['min_major_volume_24h_usd']:,.0f}")
    if (facts["exchanges"] or 0) < f["min_exchanges"]:
        out.append(f"fewer than {f['min_exchanges']} exchanges")
    if f.get("require_website_or_docs") and facts["website_or_docs"] is not True:
        out.append("no website or docs found" if facts["website_or_docs"] is False else "profile not fetched yet")
    core = [mid for mid, d in scfg["metrics"].items() if d["category"] in f["core_categories"]]
    have = sum(1 for mid in core if metrics.get(mid) is not None)
    if core and have / len(core) * 100 < f["min_core_completeness_pct"]:
        out.append(f"core data {have}/{len(core)} metrics")
    return out


def rank(results, coins):
    """Adds "rank" (1.. among passing coins, None for failing) and "rank_all"; ties -> larger mcap first."""
    order = sorted(results, key=lambda cid: (-results[cid]["total"], -(coins[cid]["facts"]["mcap"] or 0)))
    r = 0
    for i, cid in enumerate(order, 1):
        results[cid]["rank_all"] = i
        if coins[cid]["passes"]:
            r += 1
            results[cid]["rank"] = r
        else:
            results[cid]["rank"] = None
    return order


def sensitivity(coins, scfg, top_n, step=5):
    """For each category: how many coins of the passing top_n change when its weight moves +-step (the other
    weights are scaled so the total stays 100)."""
    def top(weights):
        res = score(coins, scfg, weights)
        passing = [c for c in coins if coins[c]["passes"]]
        passing.sort(key=lambda c: (-res[c]["total"], -(coins[c]["facts"]["mcap"] or 0)))
        return set(passing[:top_n])

    base_w = scfg["categories"]
    base = top(base_w)
    out = {}
    for cat, w in base_w.items():
        changes = []
        for delta in (-step, step):
            nw = max(w + delta, 0)
            rest = sum(v for c, v in base_w.items() if c != cat)
            weights = {c: (nw if c == cat else v * (100 - nw) / rest) for c, v in base_w.items()}
            changes.append(len(top(weights) - base))
        out[cat] = changes
    return out
