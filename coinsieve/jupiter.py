"""Jupiter Tokens API v2 client (no key).

Verified live 2026-10-08:
- tag?query=verified: ~3,900 tokens, full objects (~5 MB).
- toporganicscore/24h, toptraded/24h: `limit` up to 100.
- search?query=a,b,c: comma-separated mints, max 100 returned per call.
- Token fields used: id, symbol, name, mcap, fdv, liquidity, holderCount, organicScore, isVerified,
  tags, firstPool.createdAt (token age; missing on ~14%), audit.{mintAuthorityDisabled,
  freezeAuthorityDisabled, topHoldersPercentage, devMints} (keys omitted when unknown/false),
  stats24h.{holderChange (%), buyVolume, sellVolume, buyOrganicVolume, sellOrganicVolume}.
"""
import logging

from coinsieve.http import ThrottledClient

log = logging.getLogger(__name__)

SEARCH_BATCH = 100


class Jupiter(ThrottledClient):
    def tokens(self, path):
        data = self._get(path)
        return data if isinstance(data, list) else []

    def search(self, mints):
        found = []
        for i in range(0, len(mints), SEARCH_BATCH):
            found += self.tokens(f"search?query={','.join(mints[i:i + SEARCH_BATCH])}")
        return found
