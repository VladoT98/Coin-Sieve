"""Dashboard price sparklines: budgeted background fetch into SQLite + pure display helpers.

Fetch (end of every `run`, or `python sieve.py charts`): pick due tokens by priority (rows viewed on the
dashboard, then tier members / passing tokens, then tier order, oldest first), resolve each token's
highest-liquidity pool on DexScreener (re-resolved after `pool_refresh_hours`), and store GeckoTerminal
hourly closes. GeckoTerminal's free limit is ~8 calls/min (measured 2026-10-09: 429 after 6 calls 2.5 s
apart, recovered within 60 s), so calls are spaced and capped per run. When 429s outlast the client's
backoff, the run stops and every cached series is kept.

Display: the dashboard never calls an API. view() turns a stored row into the line, the % change and the
staleness flag, all from the SAME stored points, so the number and the line always agree.
"""
import json
import logging
import time

import requests

log = logging.getLogger(__name__)

SPARK_W, SPARK_H, SPARK_PAD = 120, 32, 2
SPARK_MAX_POINTS = 80   # downsampled for the table (first and last point always kept)


# ---- fetch -------------------------------------------------------------------------------
def pick_pool(pairs):
    """pairAddress of the highest-liquidity pair (bonding-curve pairs have no liquidity -> 0), or None."""
    best = max(pairs or [], key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0), default=None)
    return (best or {}).get("pairAddress")


def plan(store, cfg, now, limit=None):
    """[(address, tier, hours)] due for a fetch, highest priority first, capped at `limit`
    (default: max_calls_per_run)."""
    c = cfg["charts"]
    demand = store.chart_demand_since(now - c["demand_window_minutes"] * 60)
    rows = store.chart_rows()
    best = {}  # address -> (priority key, tier, hours)
    for rank, tier in enumerate(c["tier_order"]):
        ts = store.latest_run_ts(tier)
        if not ts:
            continue
        members = set(store.current_members(tier))
        for t in store.latest_since(tier, ts):
            addr = t["metrics"]["address"]
            viewed, important = addr in demand, addr in members or not t["reasons"]
            if not (viewed or important or c["include_failing"]):
                continue
            row = rows.get(addr) or {}
            last = row.get("attempted_at")
            if last is not None and now - last < c["refresh_minutes"][tier] * 60:
                continue
            hours = c["history_hours"][tier]
            key = (0 if viewed else 1 if important else 2, rank, row.get("fetched_at") or 0)
            if addr in best:  # in two tiers (e.g. graduating): best priority, longest window
                key, hours = min(key, best[addr][0]), max(hours, best[addr][2])
            best[addr] = (key, tier, hours)
    ordered = sorted(best.items(), key=lambda kv: kv[1][0])
    return [(a, tier, hours) for a, (_, tier, hours) in ordered][:c["max_calls_per_run"] if limit is None else limit]


def fetch(store, cfg, gt, dex, now=None, max_calls=None):
    """Fetch due series. Returns counts {planned, fetched, empty, failed, rate_limited}."""
    now = now or time.time()
    c = cfg["charts"]
    todo = plan(store, cfg, now, max_calls)
    rows = store.chart_rows([a for a, _, _ in todo])
    stats = {"planned": len(todo), "fetched": 0, "empty": 0, "failed": 0, "rate_limited": False}
    for addr, tier, hours in todo:
        t = now  # one timestamp per run keeps refresh intervals exact
        row = rows.get(addr) or {}
        pool = row.get("pool")
        if not pool or t - (row.get("pool_resolved_at") or 0) >= c["pool_refresh_hours"] * 3600:
            try:
                pool = pick_pool(dex.token_pairs(cfg["chain"], addr)) or pool
                if pool:
                    store.set_chart_pool(addr, pool, t)
            except Exception as e:  # keep the old pool if there is one
                log.warning("chart pool lookup %s failed: %s", addr, e)
        if not pool:
            store.chart_failed(addr, "no pool found on DexScreener", t)
            stats["failed"] += 1
            store.commit()
            continue
        try:
            points = gt.closes(pool, hours)
        except requests.HTTPError as e:  # permanent for this pool (e.g. 404): skip, keep any old series
            store.chart_failed(addr, f"HTTP {e.response.status_code if e.response is not None else '?'}", t)
            stats["failed"] += 1
            store.commit()
            continue
        except RuntimeError as e:  # 429 / outage beyond the backoff: stop, cached series stay
            store.chart_failed(addr, f"rate limited or unavailable: {e}", t)
            stats["failed"] += 1
            stats["rate_limited"] = True
            store.commit()
            log.warning("geckoterminal stopped after %d fetches: %s", stats["fetched"], e)
            break
        store.save_chart_points(addr, hours, points, t)
        stats["fetched" if points else "empty"] += 1
        store.commit()
    return stats


# ---- display -----------------------------------------------------------------------------
def view(row, hours, cfg, now):
    """Display data for one token, or None ("–"). The window ends at the last stored close, so a stale
    series is still drawn whole (and flagged) instead of being cut to nothing."""
    if not row or not row.get("points"):
        return None
    pts = json.loads(row["points"])
    if not pts:
        return None
    pts = [p for p in pts if p[0] >= pts[-1][0] - hours * 3600 and p[1] and p[1] > 0]
    if len(pts) < max(2, cfg["charts"]["min_points"]):
        return None
    first, last = pts[0][1], pts[-1][1]
    change = (last / first - 1) * 100
    fetched = row.get("fetched_at")
    stale = fetched is None or now - fetched > cfg["charts"]["stale_after_hours"] * 3600
    return {"change_pct": round(change, 2), "first": first, "last": last, "points": len(pts),
            "span_h": round((pts[-1][0] - pts[0][0]) / 3600, 1), "window_h": hours,
            "fetched_at": fetched, "stale": stale, "svg": sparkline_svg(pts, change, stale)}


def direction(change):
    return "flat" if round(change, 1) == 0 else "up" if change > 0 else "down"


def _downsample(pts, n):
    if len(pts) <= n:
        return pts
    step = (len(pts) - 1) / (n - 1)
    return [pts[round(i * step)] for i in range(n)]


def sparkline_svg(pts, change, stale=False):
    """Compact inline SVG built only from numbers (no text from the API). Colour comes from CSS via
    currentColor + the up/down/flat class, so it follows the dashboard theme."""
    pts = _downsample(pts, SPARK_MAX_POINTS)
    t0, t1 = pts[0][0], pts[-1][0]
    lo, hi = min(p[1] for p in pts), max(p[1] for p in pts)
    w, h = SPARK_W - 2 * SPARK_PAD, SPARK_H - 2 * SPARK_PAD

    def xy(p):
        x = SPARK_PAD + (w * (p[0] - t0) / (t1 - t0) if t1 > t0 else w / 2)
        y = SPARK_PAD + (h / 2 if hi == lo else h * (hi - p[1]) / (hi - lo))
        return f"{x:.1f},{y:.1f}"

    cls = "spark " + direction(change) + (" stale" if stale else "")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" class="{cls}" viewBox="0 0 {SPARK_W} {SPARK_H}" '
            f'width="{SPARK_W}" height="{SPARK_H}" preserveAspectRatio="none" aria-hidden="true">'
            f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" '
            f'vector-effect="non-scaling-stroke" points="{" ".join(xy(p) for p in pts)}"/></svg>')
