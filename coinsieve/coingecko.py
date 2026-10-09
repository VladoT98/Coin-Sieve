"""CoinGecko public API (no key) — token profile lookups by contract address.

Verified live 2026-10-09 (`coins/solana/contract/{mint}`, PYTH / RAY / Bonk + 19 of 41 coverage candidates):
- id, asset_platform_id ("solana" for Solana-native; "polygon-pos" for GEOD, "ethereum" for SPX), categories
  (bridged assets carry "Bridged-Tokens": ETH-wormhole, SPX), platforms {chain: address}.
- links.homepage [..], links.whitepaper (str, often "" — and sometimes dead or a docs site),
  links.repos_url.github [..], links.twitter_screen_name.
- market_data.circulating_supply / total_supply / max_supply.
- Unknown contract -> HTTP 404.
- Keyless limit is tight: 22 of 41 calls answered 429 at 7 s spacing -> config spacing 20 s, backoff 60 s.
"""
import requests

from coinsieve.http import ThrottledClient


class CoinGecko(ThrottledClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session.headers.update({"User-Agent": "coin-sieve/1.0", "Accept": "application/json"})

    def contract(self, platform, address):
        """Coin data for a contract address, or None when CoinGecko doesn't list it (404)."""
        path = (f"coins/{platform}/contract/{address}?localization=false&tickers=false&market_data=true"
                "&community_data=false&developer_data=false&sparkline=false")
        try:
            return self._get(path)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise


def summarize(coin, platform):
    """The parts of a CoinGecko coin object Coin Sieve uses (stored in token_profiles)."""
    if coin is None:
        return {"listed": False}
    links = coin.get("links") or {}
    md = coin.get("market_data") or {}
    return {
        "listed": True,
        "id": coin.get("id"),
        "platform": coin.get("asset_platform_id"),
        "native": coin.get("asset_platform_id") == platform,
        "categories": [c for c in coin.get("categories") or [] if isinstance(c, str)],
        "homepage": [u for u in links.get("homepage") or [] if u],
        "whitepaper": links.get("whitepaper") or None,
        "github": [u for u in (links.get("repos_url") or {}).get("github") or [] if u],
        "twitter": links.get("twitter_screen_name") or None,
        "supply": {"circulating": md.get("circulating_supply"), "total": md.get("total_supply"),
                   "max": md.get("max_supply")},
    }
