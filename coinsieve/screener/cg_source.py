"""CoinGecko (Demo key) -> metric observations. Field names verified 2026-10-12 (docs/api_samples/cg_*.json).

Daily, from coins/markets (no extra calls): price, market cap, FDV, volume, supplies, ATH distance, price changes.
Weekly per coin (2 calls): coins/{id} (watchlist users, links, platforms, categories, genesis, CoinGecko TVL) and
coins/{id}/tickers?depth=true (exchanges, major-exchange volume, +-2% depth, spread).
One-time per coin: 365 daily points of price / market cap / volume (market_chart) -> price_daily.
"""
import re
from datetime import datetime, timezone

COIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")


def coin_url(cg_id):
    return f"https://www.coingecko.com/en/coins/{cg_id}"


def _ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def markets_metrics(r):
    """Metrics from one coins/markets row (asked with price_change_percentage=7d,30d,200d,1y)."""
    return {
        "price_usd": _num(r.get("current_price")),
        "mcap_usd": _num(r.get("market_cap")),
        "mcap_rank": _num(r.get("market_cap_rank")),
        "fdv_usd": _num(r.get("fully_diluted_valuation")),
        "volume_24h_usd": _num(r.get("total_volume")),
        "circ_supply": _num(r.get("circulating_supply")),
        "total_supply": _num(r.get("total_supply")),
        "max_supply": _num(r.get("max_supply")),
        "ath_change_pct": _num(r.get("ath_change_percentage")),
        "ath_ts": _ts(r.get("ath_date")),
        "atl_ts": _ts(r.get("atl_date")),
        "price_change_7d_pct": _num(r.get("price_change_percentage_7d_in_currency")),
        "price_change_30d_pct": _num(r.get("price_change_percentage_30d_in_currency")),
        "price_change_200d_pct": _num(r.get("price_change_percentage_200d_in_currency")),
        "price_change_1y_pct": _num(r.get("price_change_percentage_1y_in_currency")),
    }


def price_row(r, date):
    return (r["id"], date, _num(r.get("current_price")), _num(r.get("market_cap")), _num(r.get("total_volume")),
            "markets")


def profile_metrics(coin):
    """Metrics + links from coins/{id}?localization=false&tickers=false&market_data=true&community_data=false
    &developer_data=false&sparkline=false."""
    links = coin.get("links") or {}
    md = coin.get("market_data") or {}
    tvl = md.get("total_value_locked")
    platforms = {k: v for k, v in (coin.get("platforms") or {}).items() if k and isinstance(v, str) and v.strip()}
    homepage = [u for u in links.get("homepage") or [] if isinstance(u, str) and u.startswith("http")]
    github = [u for u in (links.get("repos_url") or {}).get("github") or []
              if isinstance(u, str) and u.startswith("https://github.com/")]
    whitepaper = links.get("whitepaper") if isinstance(links.get("whitepaper"), str) else ""
    metrics = {
        "watchlist_users": _num(coin.get("watchlist_portfolio_users")),
        "sentiment_up_pct": _num(coin.get("sentiment_votes_up_percentage")),
        "genesis_ts": _ts(coin.get("genesis_date") + "T00:00:00Z") if isinstance(coin.get("genesis_date"), str) else None,
        "chains_count": float(len(platforms)),        # 0 = the coin of its own blockchain
        "own_chain": coin.get("asset_platform_id") is None,
        "cg_tvl_usd": _num(tvl.get("usd")) if isinstance(tvl, dict) else None,
        "has_website": bool(homepage),
        "has_whitepaper": bool(whitepaper.startswith("http")),
        "has_github": bool(github),
        "categories": [c for c in coin.get("categories") or [] if isinstance(c, str)],
    }
    link_rows = {"homepage": homepage, "whitepaper": [whitepaper] if whitepaper.startswith("http") else [],
                 "github": github}
    return metrics, link_rows


def ticker_metrics(tickers, major):
    """Metrics from coins/{id}/tickers?depth=true&order=volume_desc (first page = 100 tickers by volume).
    Stale / anomalous tickers are ignored. Depth = cost to move the price 2% up + 2% down, summed over major
    exchanges' tickers; spread = volume-weighted bid-ask spread on major exchanges."""
    good = [t for t in tickers or [] if isinstance(t, dict) and not t.get("is_stale") and not t.get("is_anomaly")]
    exchanges = {(t.get("market") or {}).get("identifier") for t in good} - {None}
    majors = [t for t in good if (t.get("market") or {}).get("identifier") in set(major)]
    vol = lambda t: _num((t.get("converted_volume") or {}).get("usd")) or 0.0  # noqa: E731
    major_vol = sum(vol(t) for t in majors)
    depth = sum((_num(t.get("cost_to_move_up_usd")) or 0) + (_num(t.get("cost_to_move_down_usd")) or 0)
                for t in majors)
    spread_w = [(t["bid_ask_spread_percentage"], vol(t)) for t in majors
                if _num(t.get("bid_ask_spread_percentage")) is not None and vol(t) > 0]
    spread = sum(s * w for s, w in spread_w) / sum(w for _, w in spread_w) if spread_w else None
    return {
        "exchanges_count": float(len(exchanges)),
        "major_exchanges_count": float(len({(t.get("market") or {}).get("identifier") for t in majors})),
        "major_exchanges": sorted({(t.get("market") or {}).get("identifier") for t in majors}),
        "major_volume_24h_usd": major_vol if majors else 0.0,
        "depth_2pct_usd": depth if majors else None,
        "spread_pct": spread,
        "tickers_volume_24h_usd": sum(vol(t) for t in good),
    }


def backfill_rows(cg_id, chart):
    """price_daily rows from market_chart?days=365&interval=daily. The last point is "now" (not midnight) and is
    dropped: today's row comes from coins/markets."""
    prices = chart.get("prices") or []
    caps = {p[0]: p[1] for p in chart.get("market_caps") or [] if len(p) == 2}
    vols = {p[0]: p[1] for p in chart.get("total_volumes") or [] if len(p) == 2}
    rows = {}
    for p in prices[:-1]:
        if len(p) != 2 or not _num(p[1]):
            continue
        date = datetime.fromtimestamp(p[0] / 1000, timezone.utc).strftime("%Y-%m-%d")
        rows[date] = (cg_id, date, _num(p[1]), _num(caps.get(p[0])), _num(vols.get(p[0])), "backfill")
    return list(rows.values())
