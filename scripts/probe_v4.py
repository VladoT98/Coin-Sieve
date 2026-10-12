"""Phase 1.2 probes: call each v4 endpoint once, print field names + key facts, save trimmed samples.

Keys come from .env (COINGECKO_DEMO_KEY, GITHUB_TOKEN) and are only sent as request headers - never printed or saved.
Run: python scripts/probe_v4.py [coingecko|defillama|github ...]   (default: all)
"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "api_samples"
ENV = dotenv_values(ROOT / ".env")
SECRET_HEADERS = {"authorization", "x-cg-demo-api-key"}
S = requests.Session()
S.headers["User-Agent"] = "coin-sieve-probe/1.0"


def trim(x, n=2, depth=0):
    """Keep samples small: first n list items, long strings cut, deep nesting summarised."""
    if isinstance(x, list):
        return [trim(i, n, depth + 1) for i in x[:n]] + ([f"... {len(x) - n} more"] if len(x) > n else [])
    if isinstance(x, dict):
        if depth > 4:
            return {"...keys": sorted(x)[:20]}
        return {k: trim(v, n, depth + 1) for k, v in x.items()}
    if isinstance(x, str) and len(x) > 200:
        return x[:200] + "..."
    return x


def get(name, url, headers=None, save=True, n=2):
    t = time.monotonic()
    r = S.get(url, headers=headers or {}, timeout=60)
    ms = round((time.monotonic() - t) * 1000)
    rate = {k: v for k, v in r.headers.items()
            if ("rate" in k.lower() or "limit" in k.lower() or k.lower() in ("retry-after", "cache-control", "link"))
            and k.lower() not in SECRET_HEADERS}
    print(f"\n=== {name}: HTTP {r.status_code}, {len(r.content):,} bytes, {ms} ms")
    if rate:
        print("    headers:", rate)
    try:
        data = r.json()
    except ValueError:
        print("    (not JSON)", r.text[:200])
        return r.status_code, None, r
    if save:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{name}.json").write_text(json.dumps({"_url": url.split("?")[0], "_status": r.status_code,
                                                      "_rate_headers": rate, "sample": trim(data, n)},
                                                     indent=1, ensure_ascii=False), encoding="utf-8")
    return r.status_code, data, r


def keys(obj):
    return sorted(obj) if isinstance(obj, dict) else type(obj).__name__


# ---------------------------------------------------------------- CoinGecko (Demo key)
def coingecko():
    key = ENV.get("COINGECKO_DEMO_KEY")
    if not key:
        print("COINGECKO_DEMO_KEY missing in .env - skipped")
        return
    h = {"x-cg-demo-api-key": key, "Accept": "application/json"}
    base = "https://api.coingecko.com/api/v3"

    get("cg_ping", f"{base}/ping", h)
    time.sleep(2.5)
    get("cg_key", f"{base}/key", h)   # usage endpoint: may be paid-only
    time.sleep(2.5)

    st, d, _ = get("cg_markets", f"{base}/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=1"
                                 "&sparkline=false&price_change_percentage=24h,7d,30d,200d,1y", h)
    if isinstance(d, list) and d:
        print("    coins:", len(d), "| fields:", keys(d[0]))
        print("    rank 250 mcap:", d[-1].get("market_cap"), "| BTC ath_change_percentage:", d[0].get("ath_change_percentage"))
    time.sleep(2.5)

    cid = "aave"
    st, d, _ = get("cg_coin_full", f"{base}/coins/{cid}?localization=false&tickers=true&market_data=true"
                                   "&community_data=true&developer_data=true&sparkline=false", h)
    if isinstance(d, dict):
        print("    top-level:", keys(d))
        print("    developer_data:", json.dumps(d.get("developer_data"), default=str)[:600])
        print("    community_data:", json.dumps(d.get("community_data"), default=str)[:300])
        print("    watchlist_portfolio_users:", d.get("watchlist_portfolio_users"))
        print("    repos_url.github:", (d.get("links") or {}).get("repos_url", {}).get("github"))
        t = (d.get("tickers") or [{}])[0]
        print("    ticker fields:", keys(t))
        print("    market_data keys:", keys(d.get("market_data") or {}))
    time.sleep(2.5)

    st, d, _ = get("cg_tickers_depth", f"{base}/coins/{cid}/tickers?depth=true&order=volume_desc", h)
    if isinstance(d, dict) and d.get("tickers"):
        t = d["tickers"][0]
        print("    tickers:", len(d["tickers"]), "| first:", {k: t.get(k) for k in
              ("base", "target", "bid_ask_spread_percentage", "cost_to_move_up_usd", "cost_to_move_down_usd",
               "trust_score", "converted_volume")}, "| market:", t.get("market"))
    time.sleep(2.5)

    st, d, _ = get("cg_derivatives", f"{base}/derivatives", h, n=3)
    if isinstance(d, list) and d:
        print("    contracts:", len(d), "| fields:", keys(d[0]))
        print("    with index_id:", sum(1 for x in d if x.get("index_id")),
              "| sample index_ids:", sorted({x.get("index_id") for x in d if x.get("index_id")})[:15])
    time.sleep(2.5)

    st, d, _ = get("cg_market_chart_365", f"{base}/coins/{cid}/market_chart?vs_currency=usd&days=365&interval=daily", h)
    if isinstance(d, dict):
        print("    keys:", keys(d), "| points:", {k: len(v) for k, v in d.items() if isinstance(v, list)})
        p = d.get("prices") or []
        if p:
            print("    first:", datetime.fromtimestamp(p[0][0] / 1000, timezone.utc), "last:",
                  datetime.fromtimestamp(p[-1][0] / 1000, timezone.utc))
    time.sleep(2.5)

    st, d, _ = get("cg_market_chart_max", f"{base}/coins/{cid}/market_chart?vs_currency=usd&days=max&interval=daily",
                   h, save=False)
    if isinstance(d, dict):
        print("    days=max points:", len(d.get("prices") or []), "| error:", d.get("error") or d.get("status"))


# ---------------------------------------------------------------- DefiLlama (no key)
def defillama():
    st, d, _ = get("dl_protocols", "https://api.llama.fi/protocols")
    if isinstance(d, list):
        print("    protocols:", len(d), "| fields:", keys(d[0]))
        print("    with gecko_id:", sum(1 for x in d if x.get("gecko_id")),
              "| with parentProtocol:", sum(1 for x in d if x.get("parentProtocol")))
        for x in d:
            if "aave" in (x.get("name") or "").lower() and (x.get("tvl") or 0) > 1e8:
                print("     ", {k: x.get(k) for k in ("name", "slug", "gecko_id", "parentProtocol", "category", "tvl",
                                                      "mcap", "change_7d")})
    time.sleep(1)
    st, d, _ = get("dl_chains", "https://api.llama.fi/v2/chains", n=3)
    if isinstance(d, list):
        print("    chains:", len(d), "| fields:", keys(d[0]), "| with gecko_id:", sum(1 for x in d if x.get("gecko_id")))
    time.sleep(1)
    for kind in ("dailyFees", "dailyRevenue"):
        st, d, _ = get(f"dl_overview_{kind}", "https://api.llama.fi/overview/fees?excludeTotalDataChart=true"
                       f"&excludeTotalDataChartBreakdown=true&dataType={kind}")
        if isinstance(d, dict):
            ps = d.get("protocols") or []
            print("    top-level:", keys(d), "| protocols:", len(ps))
            if ps:
                print("    protocol fields:", keys(ps[0]))
                print("    with gecko_id:", sum(1 for x in ps if x.get("gecko_id")),
                      "| with parentProtocol:", sum(1 for x in ps if x.get("parentProtocol")))
                for x in ps:
                    if "aave" in (x.get("name") or "").lower() and (x.get("total30d") or 0) > 1e6:
                        print("     ", {k: x.get(k) for k in ("name", "slug", "gecko_id", "parentProtocol",
                                                              "defillamaId", "category", "total24h", "total7d",
                                                              "total30d", "protocolType")})
        time.sleep(1)
    get("dl_hacks", "https://api.llama.fi/hacks", save=False)


# ---------------------------------------------------------------- GitHub (token)
def github():
    tok = ENV.get("GITHUB_TOKEN")
    if not tok:
        print("GITHUB_TOKEN missing in .env - skipped")
        return
    h = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    api = "https://api.github.com"
    st, d, _ = get("gh_rate_limit", f"{api}/rate_limit", h)
    if isinstance(d, dict):
        print("    core:", (d.get("resources") or {}).get("core"))
    st, d, _ = get("gh_org_repos", f"{api}/orgs/aave/repos?sort=pushed&per_page=5&type=public", h, n=1)
    if isinstance(d, list) and d:
        print("    fields:", keys(d[0]))
        print("    repos:", [(x["full_name"], x.get("pushed_at"), x.get("fork"), x.get("archived"),
                              x.get("stargazers_count")) for x in d])
    repo = "aave/aave-v3-core"
    st, d, _ = get("gh_repo", f"{api}/repos/{repo}", h)
    if isinstance(d, dict):
        print("    ", {k: d.get(k) for k in ("full_name", "created_at", "pushed_at", "stargazers_count",
                                             "forks_count", "archived", "fork")})
    since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    st, d, r = get("gh_commits_90d", f"{api}/repos/{repo}/commits?since={since}&per_page=1", h)
    print("    Link header:", r.headers.get("Link"))
    for stat in ("commit_activity", "contributors"):
        for attempt in range(3):
            st, d, _ = get(f"gh_stats_{stat}", f"{api}/repos/{repo}/stats/{stat}", h, n=1)
            if st != 202:
                break
            print("    202 = GitHub still computing; retry in 5 s")
            time.sleep(5)
        if isinstance(d, list) and d:
            print("    items:", len(d), "| fields:", keys(d[0]))


if __name__ == "__main__":
    which = sys.argv[1:] or ["coingecko", "defillama", "github"]
    for name in which:
        print(f"\n######## {name}")
        globals()[name]()
