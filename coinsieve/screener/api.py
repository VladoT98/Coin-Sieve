"""HTTP JSON client for one provider: spacing, retries, a disk cache with TTL, and call budgets.

- Every network attempt is logged in `api_calls` (cache hits are not calls and are not logged).
- Monthly budget (CoinGecko Demo: 10,000 calls/month, no usage endpoint -> we count ourselves): non-essential calls
  stop at budget * (1 - reserve_pct/100); essential calls (the daily markets list) may use the reserve.
- Quota floor (GitHub): stop when the last `X-RateLimit-Remaining` header is below `min_remaining`.
- Secrets travel only in session headers; paths (logged and used as cache keys) never contain them.
"""
import gzip
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Result:
    status: int
    data: object
    headers: dict = field(default_factory=dict)   # only the headers we use (Link, rate limits)
    cached: bool = False
    fetched_at: float = 0.0

    @property
    def ok(self):
        return self.status == 200


KEEP_HEADERS = ("link", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after")


def month_start(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp()


class Api:
    def __init__(self, provider, db, base_url, *, headers=None, min_interval_s=1.0, retries=3, backoff_s=10.0,
                 timeout_s=30, cache_dir=None, monthly_budget=None, reserve_pct=0, min_remaining=None,
                 clock=time.time, sleep=time.sleep, session=None):
        self.provider = provider
        self.db = db
        self.base = base_url.rstrip("/")
        self.min_interval = min_interval_s
        self.retries = retries
        self.backoff_s = backoff_s
        self.timeout = timeout_s
        self.cache_dir = Path(cache_dir) / provider if cache_dir else None
        self.monthly_budget = monthly_budget
        self.reserve_pct = reserve_pct
        self.min_remaining = min_remaining
        self.remaining = None          # last X-RateLimit-Remaining seen
        self.reset_at = None
        self.clock = clock
        self.sleep = sleep
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "coin-sieve/2.0", "Accept": "application/json"})
        self.session.headers.update(headers or {})
        self._last = 0.0
        self.calls = 0                 # network calls made by this client (this process)
        self.cache_hits = 0

    # ---- budget ----
    def used_this_month(self):
        return self.db.calls_since(self.provider, month_start(self.clock()))

    def limit(self, essential):
        if self.monthly_budget is None:
            return None
        return self.monthly_budget if essential else int(self.monthly_budget * (1 - self.reserve_pct / 100))

    def _check_budget(self, essential):
        lim = self.limit(essential)
        if lim is not None and self.used_this_month() >= lim:
            raise BudgetExceeded(f"{self.provider}: {lim} calls this month reached"
                                 f"{'' if essential else ' (reserve kept for essential calls)'}")
        if self.min_remaining is not None and self.remaining is not None and self.remaining < self.min_remaining:
            raise BudgetExceeded(f"{self.provider}: only {self.remaining} calls left until "
                                 f"{datetime.fromtimestamp(self.reset_at or 0, timezone.utc):%H:%M} UTC")

    # ---- cache ----
    def _cache_file(self, path):
        return self.cache_dir / (hashlib.sha1(path.encode()).hexdigest() + ".json.gz") if self.cache_dir else None

    def _cache_read(self, path, ttl_hours):
        f = self._cache_file(path)
        if not f or not ttl_hours or not f.exists():
            return None
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                c = json.load(fh)
        except (OSError, ValueError):
            return None
        if self.clock() - c["fetched_at"] >= ttl_hours * 3600:
            return None
        return Result(c["status"], c["data"], c.get("headers") or {}, True, c["fetched_at"])

    def _cache_write(self, path, res):
        f = self._cache_file(path)
        if not f:
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump({"path": path, "status": res.status, "headers": res.headers, "fetched_at": res.fetched_at,
                       "data": res.data}, fh)
        tmp.replace(f)

    # ---- GET ----
    def get(self, path, ttl_hours=0, essential=False):
        """Result for base/path. 200, 204 (empty) and 404 are cached for ttl_hours; 202 (GitHub "still computing"),
        other 4xx and failures are not. Raises BudgetExceeded before a call that would cross the budget, RuntimeError after
        `retries` failed attempts (429 / 5xx / network)."""
        hit = self._cache_read(path, ttl_hours)
        if hit:
            self.cache_hits += 1
            return hit
        err = None
        for attempt in range(1, self.retries + 1):
            self._check_budget(essential)
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            t0 = time.monotonic()
            status = 0
            try:
                r = self.session.get(f"{self.base}/{path}", timeout=self.timeout)
                status = r.status_code
            except requests.RequestException as e:
                err = e
                r = None
            ms = round((time.monotonic() - t0) * 1000)
            self.db.log_call(self.clock(), self.provider, path[:300], status, ms)
            self.calls += 1
            if r is not None:
                hdr = {k.lower(): v for k, v in r.headers.items() if k.lower() in KEEP_HEADERS}
                if "x-ratelimit-remaining" in hdr:
                    self.remaining = int(hdr["x-ratelimit-remaining"])
                    self.reset_at = int(hdr.get("x-ratelimit-reset") or 0)
                if status == 429 or status >= 500:
                    err = f"HTTP {status}"
                else:
                    try:
                        data = r.json() if r.content else None
                    except ValueError:
                        data = None
                    res = Result(status, data, hdr, False, self.clock())
                    if status in (200, 204, 404):
                        self._cache_write(path, res)
                    return res
            log.warning("%s GET %s failed (attempt %d/%d): %s", self.provider, path, attempt, self.retries, err)
            if attempt < self.retries:
                self.sleep(self.backoff_s * attempt)
        raise RuntimeError(f"{self.provider} GET {path} failed after {self.retries} attempts: {err}")
