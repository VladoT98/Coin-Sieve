"""CoinGecko public API (no key): the data base of the coverage list (all chains).

Verified live 2026-10-11 (keyless):
- `coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=N&sparkline=true
  &price_change_percentage=1h,24h,7d` -> 250 coins per call: id, symbol, name, image, current_price, market_cap,
  market_cap_rank, fully_diluted_valuation, total_volume, circulating_supply, total_supply, max_supply, ath_date,
  atl_date, last_updated, price_change_percentage_{1h,24h,7d}_in_currency, sparkline_in_7d.price (168 hourly
  prices, no timestamps; the last one is at `last_updated`). `category=<id>` limits the list to one category;
  `ids=a,b` returns only those coins. Rank 250 ~ $128M, rank 500 ~ $50M market cap.
- `coins/{id}?tickers=true&market_data=true` -> categories (names), asset_platform_id (null = the coin of its own
  blockchain, e.g. zcash), platforms {chain: contract} ({"": ""} for own-chain coins), genesis_date (often null for
  tokens), links (homepage[], whitepaper, repos_url.github[], twitter_screen_name), market_data supplies,
  tickers[] (first 100: market.identifier, is_stale, is_anomaly; trust_score is null keyless).
- `coins/categories/list` -> category ids; the exclusion ids in config.yaml were checked against it.
  Note: `real-world-assets-rwa` also holds protocol tokens (chainlink, stellar, ondo-finance) -> not used;
  `tokenized-products` holds only tokenised assets (funds, gold, xStocks).
- Coin ids match ^[a-z0-9][a-z0-9-]{0,99}$ (all 500 of the top 500 checked).
- Unknown id/contract -> HTTP 404. Keyless limit is tight (429 after ~5 quick calls) -> 20 s spacing, 60 s backoff.
"""
import re
from datetime import datetime

import requests

from coinsieve.http import ThrottledClient

CHAIN = "coingecko"   # store key space for covered tokens: (CHAIN, CoinGecko coin id)
COIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_PLATFORM = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_CONTRACT = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class CoinGecko(ThrottledClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session.headers.update({"User-Agent": "coin-sieve/1.0", "Accept": "application/json"})

    def markets(self, page=1, ids=None, category=None, sparkline=True):
        """One page (<= 250 coins) of coins/markets, largest market cap first."""
        path = (f"coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page={int(page)}"
                f"&sparkline={'true' if sparkline else 'false'}&price_change_percentage=1h,24h,7d")
        if ids:
            path += "&ids=" + ",".join(i for i in ids if COIN_ID.match(i))
        if category:
            path += "&category=" + category
        return self._get(path) or []

    def coin(self, coin_id):
        """Full coin object incl. the first 100 tickers, or None when CoinGecko doesn't list it (404)."""
        path = (f"coins/{coin_id}?localization=false&tickers=true&market_data=true&community_data=false"
                "&developer_data=false&sparkline=false")
        try:
            return self._get(path)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

    def market_chart(self, coin_id, days):
        """{prices, market_caps, total_volumes: [[ms, value], ...]} (verified 2026-10-09 on JUP: days=30 -> 721
        hourly points, days=365 -> 366 daily points; keyless days > 365 -> HTTP 401)."""
        return self._get(f"coins/{coin_id}/market_chart?vs_currency=usd&days={int(days)}")


def _ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def metrics(c, now):
    """Coverage metrics for one coins/markets item. `history_days` = days since the earlier of the all-time high
    and all-time low: CoinGecko has prices for the token since at least then (a lower bound of its age)."""
    dates = [t for t in (_ts(c.get("ath_date")), _ts(c.get("atl_date"))) if t]
    return {
        "address": c["id"],
        "symbol": (c.get("symbol") or "").upper(),
        "name": c.get("name"),
        "rank": c.get("market_cap_rank"),
        "price_usd": c.get("current_price"),
        "mcap_usd": c.get("market_cap"),
        "fdv_usd": c.get("fully_diluted_valuation"),
        "volume_h24_usd": c.get("total_volume"),
        "price_change_1h_pct": c.get("price_change_percentage_1h_in_currency"),
        "price_change_24h_pct": c.get("price_change_percentage_24h_in_currency", c.get("price_change_percentage_24h")),
        "price_change_7d_pct": c.get("price_change_percentage_7d_in_currency"),
        "circ_supply": c.get("circulating_supply"),
        "total_supply": c.get("total_supply"),
        "max_supply": c.get("max_supply"),
        "history_days": round((now - min(dates)) / 86400, 1) if dates else None,
        "icon": c.get("image"),
    }


def sparkline(c):
    """[[ts, price], ...] from sparkline_in_7d: hourly prices ending at last_updated (no timestamps in the API)."""
    prices = ((c.get("sparkline_in_7d") or {}).get("price") or [])
    end = _ts(c.get("last_updated"))
    if not end or not prices:
        return []
    n = len(prices)
    return [[round(end - (n - 1 - i) * 3600), p] for i, p in enumerate(prices) if isinstance(p, (int, float)) and p > 0]


def summarize(coin):
    """The parts of a CoinGecko coin object Coin Sieve uses (stored in token_profiles)."""
    if coin is None:
        return {"listed": False}
    links = coin.get("links") or {}
    md = coin.get("market_data") or {}
    platforms = {k: v.strip() for k, v in (coin.get("platforms") or {}).items()
                 if isinstance(k, str) and _PLATFORM.match(k) and isinstance(v, str) and _CONTRACT.match(v.strip())}
    tickers = [t for t in coin.get("tickers") or [] if not t.get("is_stale") and not t.get("is_anomaly")]
    exchanges = sorted({(t.get("market") or {}).get("identifier") for t in tickers} - {None})
    platform = coin.get("asset_platform_id")
    return {
        "listed": True,
        "id": coin.get("id"),
        "platform": platform if isinstance(platform, str) and _PLATFORM.match(platform) else None,
        "platforms": platforms,
        "categories": [c for c in coin.get("categories") or [] if isinstance(c, str)],
        "genesis_date": coin.get("genesis_date") if isinstance(coin.get("genesis_date"), str) else None,
        "homepage": [u for u in links.get("homepage") or [] if u],
        "whitepaper": links.get("whitepaper") or None,
        "github": [u for u in (links.get("repos_url") or {}).get("github") or [] if u],
        "twitter": links.get("twitter_screen_name") or None,
        "exchanges": len(exchanges),
        "tickers_listed": len(coin.get("tickers") or []),
        "supply": {"circulating": md.get("circulating_supply"), "total": md.get("total_supply"),
                   "max": md.get("max_supply")},
    }
