"""Throttled JSON GET with retries on rate limits / server errors."""
import logging
import time

import requests

log = logging.getLogger(__name__)


class ThrottledClient:
    def __init__(self, base_url, timeout_s, min_interval_s, retries, backoff_s=2):
        self.base = base_url.rstrip("/")
        self.timeout = timeout_s
        self.min_interval = min_interval_s
        self.retries = retries
        self.backoff_s = backoff_s  # wait backoff_s * attempt after a failure
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
                time.sleep(self.backoff_s * attempt)
        raise RuntimeError(f"GET {path} failed after {self.retries} attempts")
