"""Universe: coins >= enter_mcap_usd (stay until < stay_mcap_usd), minus excluded CoinGecko categories and ids.

`decide()` is pure (tested offline); `markets()` / `category_sets()` do the CoinGecko calls.
Reasons stored per coin and day: included | below_enter_mcap | below_stay_mcap | category:<id> | excluded_id |
categories_unknown (a category list failed to load -> nobody is included on doubt).
"""
import logging

log = logging.getLogger(__name__)

MARKETS = ("coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}&sparkline=false"
           "&price_change_percentage=7d,30d,200d,1y")


def markets(api, ucfg, ttl_hours):
    """All coins/markets rows down to stay_mcap_usd (essential calls: may use the budget reserve)."""
    rows = []
    for page in range(1, ucfg["max_market_pages"] + 1):
        res = api.get(MARKETS.format(page=page), ttl_hours=ttl_hours, essential=True)
        if not res.ok or not isinstance(res.data, list):
            raise RuntimeError(f"coins/markets page {page}: HTTP {res.status}")
        rows += [r for r in res.data if isinstance(r, dict) and r.get("id")]
        last = res.data[-1].get("market_cap") if res.data else None
        if len(res.data) < 250 or (last or 0) < ucfg["stay_mcap_usd"]:
            break
    return rows


def category_sets(api, ucfg):
    """{category id: set of coin ids above stay_mcap_usd}. A list that fails to load is missing from the result."""
    sets = {}
    for cat in ucfg["exclude_categories"]:
        ids = set()
        try:
            for page in range(1, ucfg["max_market_pages"] + 1):
                res = api.get(f"coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}"
                              f"&sparkline=false&category={cat}", ttl_hours=ucfg["categories_ttl_hours"])
                if not res.ok or not isinstance(res.data, list):
                    raise RuntimeError(f"HTTP {res.status}")
                ids |= {r["id"] for r in res.data if isinstance(r, dict) and r.get("id")}
                last = res.data[-1].get("market_cap") if res.data else None
                if len(res.data) < 250 or (last or 0) < ucfg["stay_mcap_usd"]:
                    break
        except Exception as e:  # noqa: BLE001 - one category must not stop the run; its coins become "unknown"
            log.warning("category %s failed: %s", cat, e)
            continue
        sets[cat] = ids
    return sets


def decide(rows, ucfg, prev_included, cat_sets):
    """[{cg_id, symbol, name, included, reason, rank, mcap}] for every markets row above stay_mcap_usd."""
    missing_cats = [c for c in ucfg["exclude_categories"] if c not in cat_sets]
    excluded_ids = set(ucfg.get("exclude_ids") or [])
    out = []
    for r in rows:
        mcap = r.get("market_cap") or 0
        if mcap < ucfg["stay_mcap_usd"]:
            continue
        cid = r["id"]
        cats = [c for c in ucfg["exclude_categories"] if cid in cat_sets.get(c, ())]
        if cid in excluded_ids:
            reason = "excluded_id"
        elif cats:
            reason = f"category:{cats[0]}"
        elif missing_cats:
            reason = "categories_unknown"
        elif mcap >= ucfg["enter_mcap_usd"] or cid in prev_included:
            reason = "included"
        else:
            reason = "below_enter_mcap"
        out.append({"cg_id": cid, "symbol": (r.get("symbol") or "").upper(), "name": r.get("name"),
                    "included": reason == "included", "reason": reason, "rank": r.get("market_cap_rank"),
                    "mcap": mcap})
    return out
