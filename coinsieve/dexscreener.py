"""DexScreener public API client (no key).

Field names verified against live responses on 2026-10-07:
- feeds (token-profiles/latest, token-boosts/*, community-takeovers/latest): list of
  {chainId, tokenAddress, ...}, ~30 newest entries only.
- tokens/v1/{chain}/{a,b,...}: up to 30 addresses, returns ONE main pair per token.
- token-pairs/v1/{chain}/{addr}: ALL pairs for a token (incl. the old bonding-curve pair
  after migration). pairCreatedAt is in milliseconds.
"""
import logging
import time

import requests

log = logging.getLogger(__name__)

BATCH_SIZE = 30  # max addresses per tokens/v1 call


class DexScreener:
    def __init__(self, base_url, timeout_s, min_interval_s, retries):
        self.base = base_url.rstrip("/")
        self.timeout = timeout_s
        self.min_interval = min_interval_s
        self.retries = retries
        self._last_call = 0.0
        self.session = requests.Session()

    def _get(self, path):
        for attempt in range(1, self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            try:
                r = self.session.get(f"{self.base}/{path}", timeout=self.timeout)
            except requests.RequestException as e:
                err = e
            else:
                # Retry only rate limits and server errors; other 4xx are permanent.
                if r.status_code == 429 or r.status_code >= 500:
                    err = f"HTTP {r.status_code}"
                else:
                    r.raise_for_status()
                    try:
                        return r.json()
                    except ValueError as e:
                        err = e
            log.warning("GET %s failed (attempt %d/%d): %s", path, attempt, self.retries, err)
            if attempt < self.retries:
                time.sleep(2 ** attempt)
        raise RuntimeError(f"GET {path} failed after {self.retries} attempts")

    def feed(self, path):
        """Entries from a discovery feed, e.g. 'token-profiles/latest/v1'."""
        data = self._get(path)
        return data if isinstance(data, list) else []

    def main_pairs(self, chain, addresses):
        """{token address: main pair} for the given addresses (batched by 30)."""
        result = {}
        for i in range(0, len(addresses), BATCH_SIZE):
            batch = addresses[i:i + BATCH_SIZE]
            try:
                pairs = self._get(f"tokens/v1/{chain}/{','.join(batch)}") or []
            except Exception as e:  # one bad batch shouldn't sink the run
                log.error("tokens/v1 batch failed: %s", e)
                continue
            for pair in pairs:
                addr = pair.get("baseToken", {}).get("address")
                if addr in batch and addr not in result:
                    result[addr] = pair
        return result

    def token_pairs(self, chain, address):
        """All pairs where the token is the base token."""
        pairs = self._get(f"token-pairs/v1/{chain}/{address}") or []
        return [p for p in pairs if p.get("baseToken", {}).get("address") == address]
