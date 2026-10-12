"""`sieve.py fetch` / `backfill` / `budget`: run the v4 data pipeline once.

Order: CoinGecko markets -> universe -> DefiLlama -> CoinGecko profiles (weekly rotation) -> GitHub (weekly
rotation) -> price backfill for coins without history. Each step is independent: one failing source is reported
and the others still run. Calls are budgeted in `api.Api`; cached responses cost nothing.
"""
import calendar
import gzip
import logging
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from coinsieve.screener import cg_source, gh_source, llama_source, universe
from coinsieve.screener.api import Api, BudgetExceeded, month_start
from coinsieve.screener.db import ScreenerDB

log = logging.getLogger(__name__)


def today(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


def open_db(cfg):
    return ScreenerDB(cfg["screener"]["db_path"])


def make_apis(cfg, db, env, sleep=time.sleep):
    s = cfg["screener"]
    cg, gh, dl = s["coingecko"], s["github"], s["defillama"]
    cg_key, gh_tok = env.get(cg["key_env"]), env.get(gh["token_env"])
    if not cg_key:
        raise SystemExit(f"{cg['key_env']} missing in .env")
    return {
        "coingecko": Api("coingecko", db, cg["base_url"], headers={"x-cg-demo-api-key": cg_key},
                         min_interval_s=cg["min_interval_s"], backoff_s=cg["backoff_s"], cache_dir=s["cache_dir"],
                         monthly_budget=cg["monthly_budget"], reserve_pct=cg["reserve_pct"], sleep=sleep),
        "defillama": Api("defillama", db, dl["base_url"], min_interval_s=dl["min_interval_s"], timeout_s=90,
                         cache_dir=s["cache_dir"], sleep=sleep),
        "github": Api("github", db, gh["base_url"],
                      headers={"Authorization": f"Bearer {gh_tok}", "X-GitHub-Api-Version": "2022-11-28",
                               "Accept": "application/vnd.github+json"} if gh_tok else {},
                      min_interval_s=gh["min_interval_s"], cache_dir=s["cache_dir"],
                      min_remaining=gh["min_remaining"], sleep=sleep),
    }


# ---------------------------------------------------------------- steps
def step_universe(cfg, db, api, now, date):
    ucfg = cfg["screener"]["universe"]
    rows = universe.markets(api, ucfg, cfg["screener"]["coingecko"]["markets_ttl_hours"])
    sets = universe.category_sets(api, ucfg)
    for cat in ucfg["exclude_categories"]:          # last good list when today's failed (logged in category_sets)
        if cat in sets:
            db.set_kv(f"category:{cat}", sorted(sets[cat]), now)
        elif db.get_kv(f"category:{cat}") is not None:
            sets[cat] = set(db.get_kv(f"category:{cat}"))
    prev_date = db.last_universe_date(date)
    decided = universe.decide(rows, ucfg, db.included_on(prev_date) if prev_date else set(), sets)
    db.save_universe(date, decided)
    included = [d["cg_id"] for d in decided if d["included"]]
    by_id = {r["id"]: r for r in rows}
    for d in decided:
        db.upsert_coin(d["cg_id"], d["symbol"], d["name"], date)
    db.save_prices([cg_source.price_row(by_id[d["cg_id"]], date) for d in decided])
    for cid in included:
        db.save_metrics(date, cid, cg_source.markets_metrics(by_id[cid]), cg_source.coin_url(cid), now)
    db.commit()
    reasons = {}
    for d in decided:
        reasons[d["reason"].split(":")[0]] = reasons.get(d["reason"].split(":")[0], 0) + 1
    return included, f"{len(rows)} market rows, {len(included)} in universe; " + \
        ", ".join(f"{k} {v}" for k, v in sorted(reasons.items()))


def step_defillama(cfg, db, api, now, date, included):
    data = llama_source.fetch(api, cfg["screener"]["defillama"]["ttl_hours"])
    found = llama_source.coin_metrics(included, *data)
    for cid, parts in found.items():
        db.replace_source_day(date, cid, "https://defillama.com/")
        for m, url in parts:
            db.save_metrics(date, cid, m, url, now)
    db.commit()
    merged = [{k: v for m, _ in parts for k, v in m.items()} for parts in found.values()]
    tvl = sum(1 for m in merged if m.get("protocol_tvl_usd") or m.get("chain_tvl_usd"))
    fees = sum(1 for m in merged if m.get("fees_30d_usd"))
    return f"{len(found)} coins matched (TVL {tvl}, fees {fees})"


def due(db, source, keys, refresh_days, now):
    """Keys never fetched first (in the given order, i.e. by market cap), then the oldest; only those older than
    refresh_days."""
    rows = sorted(((db.last_fetch(source, k) or 0, i, k) for i, k in enumerate(keys)))
    return [k for t, _, k in rows if now - t >= refresh_days * 86400 - 3600]


def step_profiles(cfg, db, api, now, date, included, max_n):
    c = cfg["screener"]["coingecko"]
    todo = due(db, "cg_profile", included, c["profile_refresh_days"], now)[:max_n]
    done = 0
    ttl = c["profile_refresh_days"] * 24 - 1
    for cid in todo:
        if not cg_source.COIN_ID.match(cid):
            continue
        try:
            coin = api.get(f"coins/{cid}?localization=false&tickers=false&market_data=true&community_data=false"
                           "&developer_data=false&sparkline=false", ttl_hours=ttl)
            tick = api.get(f"coins/{cid}/tickers?depth=true&order=volume_desc", ttl_hours=ttl)
        except BudgetExceeded:
            raise
        except RuntimeError as e:      # failed after retries: due again tomorrow (BudgetExceeded still stops)
            log.warning("profile %s failed: %s", cid, e)
            db.mark_fetched("cg_profile", cid, now - (c["profile_refresh_days"] - 1) * 86400, "error")
            db.commit()
            continue
        if coin.ok and isinstance(coin.data, dict):
            metrics, links = cg_source.profile_metrics(coin.data)
            db.save_metrics(date, cid, metrics, cg_source.coin_url(cid), now)
            for kind, urls in links.items():
                db.set_links(cid, kind, urls, now)
        if tick.ok and isinstance(tick.data, dict):
            db.save_metrics(date, cid, cg_source.ticker_metrics(tick.data.get("tickers"), c["major_exchanges"]),
                            cg_source.coin_url(cid) + "#markets", now)
        db.mark_fetched("cg_profile", cid, now, "ok" if coin.ok and tick.ok else f"{coin.status}/{tick.status}")
        db.commit()
        done += 1
    left = len(due(db, "cg_profile", included, c["profile_refresh_days"], now))
    return f"{done} profiles fetched, {left} still due"


def step_github(cfg, db, api, now, date, included, max_n, sleep=time.sleep):
    g = cfg["screener"]["github"]
    coin_owners = {}
    listed = {}
    overrides = g.get("owner_overrides") or {}
    for cid in included:
        owners, rep = gh_source.owners_and_repos(
            [f"https://github.com/{o}" for o in overrides[cid]] if cid in overrides else db.links(cid, "github"))
        if owners:
            coin_owners[cid] = owners[:2]
            for o in owners[:2]:
                listed.setdefault(o, set()).update(rep.get(o, ()))
    all_owners = sorted({o for os_ in coin_owners.values() for o in os_})
    todo = due(db, "github", all_owners, g["refresh_days"], now)[:max_n]
    fresh = set()
    for owner in todo:
        try:
            raw = gh_source.fetch_owner(api, owner, listed.get(owner, set()), g, now, sleep)
        except BudgetExceeded:
            raise
        except RuntimeError as e:      # failed after retries: due again tomorrow (BudgetExceeded still stops)
            log.warning("github owner %s failed: %s", owner, e)
            db.mark_fetched("github", owner, now - (g["refresh_days"] - 1) * 86400, "error")
            db.commit()
            continue
        db.set_kv(f"github:{owner}", raw, now)
        if raw and raw["incomplete"]:   # stats still computing at GitHub -> due again in a day, not a week
            db.mark_fetched("github", owner, now - (g["refresh_days"] - 1) * 86400, "incomplete")
        else:
            db.mark_fetched("github", owner, now, "ok" if raw else "missing")
        db.commit()
        fresh.add(owner)
    saved = 0
    for cid, owners in coin_owners.items():
        # Save when an owner was just fetched, or when raw data exists but its metrics were never saved (a run
        # that stopped between the fetch and this loop). Old data is not re-saved as a new observation.
        if not fresh.intersection(owners) and "commits_90d" in db.latest_metrics(cid, date):
            continue
        m = gh_source.coin_metrics([db.get_kv(f"github:{o}") for o in owners], now)
        if m:
            db.save_metrics(date, cid, m, f"https://github.com/{owners[0]}", now)
            saved += 1
    db.commit()
    left = len(due(db, "github", all_owners, g["refresh_days"], now))
    no_repo = sum(1 for cid in included if cid not in coin_owners)
    return (f"{len(todo)} owners fetched, {saved} coins updated, {left} owners still due, "
            f"{no_repo} coins without a GitHub link yet")


def step_backfill(cfg, db, api, now, included, max_n):
    c = cfg["screener"]["coingecko"]
    todo = [cid for cid in included if db.get_kv(f"backfill:{cid}") is None][:max_n]
    for cid in todo:
        res = api.get(f"coins/{cid}/market_chart?vs_currency=usd&days={int(c['backfill_days'])}&interval=daily")
        if res.ok and isinstance(res.data, dict):
            rows = cg_source.backfill_rows(cid, res.data)
            db.save_prices(rows)
            db.set_kv(f"backfill:{cid}", {"days": len(rows), "status": 200}, now)
        elif res.status in (401, 404):
            db.set_kv(f"backfill:{cid}", {"days": 0, "status": res.status}, now)
        db.commit()
    left = sum(1 for cid in included if db.get_kv(f"backfill:{cid}") is None)
    return f"{len(todo)} coins backfilled, {left} still without history"


# ---------------------------------------------------------------- commands
def run_fetch(cfg, env, now=None, steps=None, max_profiles=None, max_owners=None, max_backfill=None,
              out=print, sleep=time.sleep):
    now = now or time.time()
    date = today(now)
    s = cfg["screener"]
    db = open_db(cfg)
    apis = make_apis(cfg, db, env, sleep)
    steps = steps or ["universe", "defillama", "profiles", "github", "backfill"]
    out(f"v4 fetch {date} ({', '.join(steps)})")
    try:
        try:
            included, msg = step_universe(cfg, db, apis["coingecko"], now, date)
            out(f"  universe   {msg}")
        except (BudgetExceeded, RuntimeError) as e:
            out(f"  universe   FAILED: {e}")
            prev = db.last_universe_date(date + "~")    # today's or the last saved universe
            included = sorted(db.included_on(prev)) if prev else []
            if not included:
                return 1
            out(f"             using the universe of {prev} ({len(included)} coins)")
        jobs = {
            "defillama": lambda: step_defillama(cfg, db, apis["defillama"], now, date, included),
            "profiles": lambda: step_profiles(cfg, db, apis["coingecko"], now, date, included,
                                              max_profiles or s["coingecko"]["profile_max_per_run"]),
            "github": lambda: step_github(cfg, db, apis["github"], now, date, included,
                                          max_owners or s["github"]["max_owners_per_run"], sleep),
            "backfill": lambda: step_backfill(cfg, db, apis["coingecko"], now, included,
                                              max_backfill or s["coingecko"]["backfill_max_per_run"]),
        }
        for name in steps:
            if name not in jobs:
                continue
            try:
                out(f"  {name:<10} {jobs[name]()}")
            except (BudgetExceeded, RuntimeError) as e:
                db.commit()
                out(f"  {name:<10} STOPPED: {e}")
        out("  calls      " + ", ".join(f"{k} {a.calls} (+{a.cache_hits} cached)" for k, a in apis.items()))
        return 0
    finally:
        db.close()


def budget_report(cfg, now=None, out=print):
    now = now or time.time()
    db = open_db(cfg)
    try:
        start = month_start(now)
        d = datetime.fromtimestamp(now, timezone.utc)
        days_in_month = calendar.monthrange(d.year, d.month)[1]
        days_left = (start + days_in_month * 86400 - now) / 86400
        out(f"API calls in {d:%Y-%m} (day {d.day} of {days_in_month})")
        cg = cfg["screener"]["coingecko"]
        for prov, n in sorted(db.calls_by_provider(start).items()):
            line = f"  {prov:<10} {n:>6}"
            if prov == "coingecko":
                # rate of the last 7 days (or since the first logged call, at least 1 day) for the rest of the month
                first = db.conn.execute("SELECT MIN(ts) FROM api_calls WHERE provider = ?", (prov,)).fetchone()[0]
                window = min(7.0, max((now - first) / 86400, 1.0))
                rate = db.calls_since(prov, now - window * 86400) / window
                proj = n + rate * days_left
                line += (f"   ~{rate:,.0f}/day -> month end ~{proj:,.0f} / {cg['monthly_budget']:,} "
                         f"(non-essential stop at {int(cg['monthly_budget'] * (1 - cg['reserve_pct'] / 100)):,})")
            out(line)
        last = db.conn.execute("SELECT provider, COUNT(*) FROM api_calls WHERE ts >= ? GROUP BY provider",
                               (now - 86400,)).fetchall()
        out("  last 24h:  " + (", ".join(f"{p} {n}" for p, n in last) or "none"))
    finally:
        db.close()
    return 0


def backup(cfg, out_dir, now=None, out=print):
    """Consistent copy of the screener DB (SQLite backup API, safe while a fetch is writing), gzipped to
    <out_dir>/screener-YYYY-MM-DD.db.gz. Phase 3/4 uploads these to a GitHub Release from CI."""
    now = now or time.time()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "screener-backup.tmp.db"
    target = out_dir / f"screener-{today(now)}.db.gz"
    src = sqlite3.connect(cfg["screener"]["db_path"])
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    with open(tmp, "rb") as fi, gzip.open(target, "wb", compresslevel=9) as fo:
        shutil.copyfileobj(fi, fo)
    size_db, size_gz = tmp.stat().st_size, target.stat().st_size
    tmp.unlink()
    out(f"Backup written: {target} ({size_gz / 1e6:.1f} MB gzipped, {size_db / 1e6:.1f} MB database)")
    return 0
