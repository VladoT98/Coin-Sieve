"""Minimal Solana JSON-RPC client (public endpoint, no key).

Verified live 2026-10-09 against api.mainnet-beta.solana.com: getMultipleAccounts with
{"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}} returns result.value[i] = null (no account)
or {owner (program id), executable, lamports, space}. Headers showed x-ratelimit-tier: free,
x-ratelimit-method-limit: 50. One call per token (up to 100 addresses) is enough here.
"""
import logging
import time

import requests

log = logging.getLogger(__name__)

SYSTEM_PROGRAM = "11111111111111111111111111111111"
MAX_ACCOUNTS = 100


class SolanaRPC:
    def __init__(self, url, timeout_s, min_interval_s, retries):
        self.url, self.timeout, self.min_interval, self.retries = url, timeout_s, min_interval_s, retries
        self._last = 0.0
        self.session = requests.Session()

    def _call(self, method, params):
        for attempt in range(1, self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                      timeout=self.timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    err = f"HTTP {r.status_code}"
                else:
                    r.raise_for_status()
                    j = r.json()
                    if "error" in j:
                        raise RuntimeError(f"{method}: {j['error']}")
                    return j["result"]
            except requests.RequestException as e:
                err = e
            log.warning("rpc %s failed (attempt %d/%d): %s", method, attempt, self.retries, err)
            if attempt < self.retries:
                time.sleep(2 * attempt)
        raise RuntimeError(f"rpc {method} failed after {self.retries} attempts")

    def account_owners(self, addresses):
        """{address: {"program": owner program id, "executable": bool} | None (no account)}."""
        out = {}
        for i in range(0, len(addresses), MAX_ACCOUNTS):
            batch = addresses[i:i + MAX_ACCOUNTS]
            res = self._call("getMultipleAccounts", [batch, {"encoding": "base64",
                                                            "dataSlice": {"offset": 0, "length": 0}}])
            for addr, v in zip(batch, res["value"]):
                out[addr] = None if v is None else {"program": v.get("owner"), "executable": bool(v.get("executable"))}
        return out
