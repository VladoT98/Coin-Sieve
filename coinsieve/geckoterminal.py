"""GeckoTerminal API (no key) — only used for price sparklines on the daily card.

Verified live 2026-10-08:
- networks/solana/pools/{pool}/ohlcv/hour?aggregate=1&limit=N&currency=usd&token=base
  → data.attributes.ohlcv_list = [[ts, open, high, low, close, volume_usd], ...] NEWEST FIRST.
- Free limit is lower than documented: HTTP 429 after ~6 calls at 2.1 s spacing → we space calls 7 s apart
  and take pool addresses from DexScreener (one batch call) instead of GeckoTerminal's pools endpoint.
"""
import logging

from coinsieve.http import ThrottledClient

log = logging.getLogger(__name__)


class GeckoTerminal(ThrottledClient):
    def hourly_closes(self, pool, hours):
        """[(ts, close)] oldest first for a pool whose base token is ours, or None if unavailable."""
        try:
            data = self._get(f"networks/solana/pools/{pool}/ohlcv/hour"
                             f"?aggregate=1&limit={hours}&currency=usd&token=base")
        except Exception as e:
            log.warning("geckoterminal chart for pool %s failed: %s", pool, e)
            return None
        candles = ((data or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
        points = sorted((c[0], c[4]) for c in candles if c and c[4])
        return points if len(points) >= 2 else None
