"""Token page chart: price, market cap and volume history from CoinGecko (`python sieve.py history`).

Two series per covered token: "30d" (hourly; the page cuts 24h / 7d / 1M from it) and "365d" (daily; 1Y).
The keyless API refuses more than 365 days, so there is no "All" range. Calls are budgeted per run and spaced
(CoinGecko's keyless limit is tight); on a persistent 429 the run stops and every cached series is kept.
The website only reads the stored series.
"""
import logging
import re

log = logging.getLogger(__name__)

_COIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")


def parse(j):
    """[[ts (s), price, mcap, volume], ...] oldest first, joined on the timestamp. Points without a price are dropped."""
    by_ts = {}
    for key, idx in (("prices", 1), ("market_caps", 2), ("total_volumes", 3)):
        for row in (j or {}).get(key) or []:
            if not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], (int, float)):
                continue
            p = by_ts.setdefault(int(row[0] // 1000), [int(row[0] // 1000), None, None, None])
            p[idx] = row[1] if isinstance(row[1], (int, float)) else None
    return [p for _, p in sorted(by_ts.items()) if p[1] is not None and p[1] > 0]


def candidates(profiles):
    """{address: coingecko id} for tokens with a usable CoinGecko id (from the profiles job)."""
    out = {}
    for addr, prof in profiles.items():
        cid = ((prof.get("data") or {}).get("coingecko") or {}).get("id")
        if isinstance(cid, str) and _COIN_ID.match(cid):
            out[addr] = cid
    return out


def plan(ids, attempts, cfg, now, limit=None):
    """[(address, coin id, span)] due for a fetch: never fetched first, then the oldest attempt."""
    h = cfg["history"]
    due = []
    for addr, cid in ids.items():
        for span in h["days"]:
            last = attempts.get((addr, span))
            if last is None or now - last >= h["refresh_hours"][span] * 3600:
                due.append((last or 0, addr, cid, span))
    due.sort()
    return [(a, c, s) for _, a, c, s in due][:h["max_calls_per_run"] if limit is None else limit]


def fetch(store, cfg, cg, addresses, now, limit=None):
    """Fetch due series for the given covered addresses. Returns counts."""
    chain = cfg["chain"]
    ids = candidates(store.profiles(chain, addresses))
    todo = plan(ids, store.history_attempts(chain), cfg, now, limit)
    st = {"planned": len(todo), "fetched": 0, "failed": 0, "rate_limited": False}
    for addr, cid, span in todo:
        try:
            points = parse(cg.market_chart(cid, cfg["history"]["days"][span]))
        except RuntimeError as e:  # 429 / outage beyond the client's backoff: stop, cached series stay
            store.history_failed(chain, addr, span, f"rate limited or unavailable: {e}", now)
            store.commit()
            st.update(failed=st["failed"] + 1, rate_limited=True)
            log.warning("history stopped after %d fetches: %s", st["fetched"], e)
            break
        except Exception as e:  # permanent for this coin (e.g. 404): keep any old series
            store.history_failed(chain, addr, span, e, now)
            st["failed"] += 1
        else:
            if points:
                store.save_history(chain, addr, span, points, now)
                st["fetched"] += 1
            else:
                store.history_failed(chain, addr, span, "no price points", now)
                st["failed"] += 1
        store.commit()
    return st


def view(rows, cfg, now):
    """Page data: {span: {points, fetched_at, stale}} for the spans that have points."""
    out = {}
    for span, r in (rows or {}).items():
        if not r.get("points"):
            continue
        stale = r["fetched_at"] is None or now - r["fetched_at"] > cfg["history"]["stale_after_hours"][span] * 3600
        out[span] = {"points": r["points"], "fetched_at": r["fetched_at"], "stale": stale}
    return out
