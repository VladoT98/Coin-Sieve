"""Market strip on the dashboard: Fear & Greed, market cap, open interest, derivatives volume.

Fetched by the dashboard server in a background thread (every `market.refresh_minutes`), stored in SQLite
(`market_snapshots`) so restarts and outages keep the last numbers. Page loads only read the cache.
Each section is fetched independently: a failing source keeps its previous value and timestamp.

Verified live 2026-10-09:
- alternative.me fng/?limit=2 -> data[{value (str), value_classification, timestamp}] newest first, daily.
- CoinGecko (no key) /global -> data.total_market_cap{usd,btc}, total_volume.usd,
  market_cap_change_percentage_24h_usd. /derivatives/exchanges?per_page=100&page=N -> [{open_interest_btc,
  trade_volume_24h_btc (str)}], 113 exchanges in 2 pages. Keyless limit: 429 after ~3 quick calls.
- Binance futures /fapi/v1/ticker/24hr?symbol=SOLUSDT -> quoteVolume (USD);
  /futures/data/openInterestHist?period=1h&limit=25 -> [{sumOpenInterestValue (USD), timestamp}] oldest first.
- Bybit /v5/market/tickers (linear SOLUSDT) -> turnover24h, singleOpenInterestValue. `openInterest` counts
  both sides (2x Binance's convention); `singleOpenInterest` is the one-side figure we use.
  /v5/market/open-interest?intervalTime=1h&limit=25 -> list[{singleOpenInterest, timestamp}] NEWEST first;
  /v5/market/kline?interval=60&limit=25 -> list[[start, open, high, low, close, vol, turnover]] newest first.
"""
import json
import logging
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

SECTIONS = ("fng", "global", "total", "sol")


def _get(url, params=None, timeout=20):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "coin-sieve/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---- parsers (pure, tested on recorded shapes) ------------------------------------------
def parse_fng(j):
    d = j["data"]
    return {"value": int(d[0]["value"]), "label": d[0]["value_classification"],
            "prev": int(d[1]["value"]) if len(d) > 1 else None}


def parse_global(j):
    d = j["data"]
    return {"mcap": d["total_market_cap"]["usd"], "mcap_change_24h_pct": d.get("market_cap_change_percentage_24h_usd"),
            "volume": d["total_volume"]["usd"],
            "btc_usd": d["total_market_cap"]["usd"] / d["total_market_cap"]["btc"]}


def total_derivatives(exchanges, btc_usd):
    """(open interest USD, 24h volume USD, exchanges counted) summed across derivatives exchanges."""
    oi = sum(float(x.get("open_interest_btc") or 0) for x in exchanges)
    vol = sum(float(x.get("trade_volume_24h_btc") or 0) for x in exchanges)
    n = sum(1 for x in exchanges if x.get("open_interest_btc"))
    return oi * btc_usd, vol * btc_usd, n


def sol_binance(ticker, hist):
    """(oi_now, oi_24h_ago, volume_24h) in USD."""
    return float(hist[-1]["sumOpenInterestValue"]), float(hist[0]["sumOpenInterestValue"]), float(ticker["quoteVolume"])


def sol_bybit(ticker, hist, klines):
    """(oi_now, oi_24h_ago, volume_24h) in USD. History is in SOL; valued at the price at that hour."""
    t = ticker["result"]["list"][0]
    oldest = hist["result"]["list"][-1]
    opens = {int(k[0]): float(k[1]) for k in klines["result"]["list"]}
    price_then = opens.get(int(oldest["timestamp"]))
    ago = float(oldest["singleOpenInterest"]) * price_then if price_then else None
    return float(t["singleOpenInterestValue"]), ago, float(t["turnover24h"])


def pct(now, before):
    return round((now / before - 1) * 100, 2) if now is not None and before else None


def change_from_snapshots(snapshots, key, now, window):
    """24h change of data['total'][key] vs the stored snapshot closest to 24h ago within `window` hours."""
    lo, hi = now - window[1] * 3600, now - window[0] * 3600
    old = [s for s in snapshots if lo <= s["ts"] <= hi and (s["data"].get("total") or {}).get(key)]
    if not old:
        return None
    ref = min(old, key=lambda s: abs(s["ts"] - (now - 24 * 3600)))
    return ref["data"]["total"][key]


# ---- fetch ------------------------------------------------------------------------------
def fetch(cfg, previous=None, get=_get, sleep=time.sleep, now=None):
    """New market data dict. Sections that fail keep the previous value (and its `updated` time)."""
    m, now = cfg["market"], now or time.time()
    u = m["urls"]
    data = {k: v for k, v in (previous or {}).items() if k in SECTIONS + ("updated",)}
    data.setdefault("updated", {})

    def section(name, fn):
        try:
            data[name] = fn()
            data["updated"][name] = now
        except Exception as e:
            log.warning("market %s failed (keeping last value): %s", name, e)

    section("fng", lambda: parse_fng(get(u["fng"])))
    section("global", lambda: parse_global(get(u["coingecko"] + "/global")))

    def total():
        btc_usd = (data.get("global") or {}).get("btc_usd")
        if not btc_usd:
            raise ValueError("no BTC price for conversion")
        exchanges = []
        for page in (1, 2, 3):
            sleep(m["coingecko_spacing_s"])
            batch = get(u["coingecko"] + "/derivatives/exchanges", {"per_page": 100, "page": page})
            exchanges += batch
            if len(batch) < 100:
                break
        oi, vol, n = total_derivatives(exchanges, btc_usd)
        return {"oi": oi, "volume": vol, "exchanges": n}
    section("total", total)

    def sol():
        b = sol_binance(get(u["binance_futures"] + "/fapi/v1/ticker/24hr", {"symbol": "SOLUSDT"}),
                        get(u["binance_futures"] + "/futures/data/openInterestHist",
                            {"symbol": "SOLUSDT", "period": "1h", "limit": 25}))
        y = sol_bybit(get(u["bybit"] + "/v5/market/tickers", {"category": "linear", "symbol": "SOLUSDT"}),
                      get(u["bybit"] + "/v5/market/open-interest",
                          {"category": "linear", "symbol": "SOLUSDT", "intervalTime": "1h", "limit": 25}),
                      get(u["bybit"] + "/v5/market/kline",
                          {"category": "linear", "symbol": "SOLUSDT", "interval": "60", "limit": 25}))
        oi, ago = b[0] + y[0], (b[1] + y[1]) if y[1] is not None else None
        return {"oi": oi, "oi_change_24h_pct": pct(oi, ago), "volume": b[2] + y[2]}
    section("sol", sol)
    data["ts"] = now
    return data


def view(snapshot, history, cfg, now):
    """What the page shows: latest data + Total OI change from stored snapshots + per-section staleness."""
    if not snapshot:
        return None
    data = dict(snapshot["data"])
    total = dict(data.get("total") or {})
    if total.get("oi"):
        before = change_from_snapshots(history, "oi", data.get("ts") or now, cfg["market"]["change_window_hours"])
        total["oi_change_24h_pct"] = pct(total["oi"], before)
    data["total"] = total or None
    limit = cfg["market"]["stale_after_minutes"] * 60
    data["stale"] = {k: now - t > limit for k, t in (data.get("updated") or {}).items()}
    data["refresh_s"] = cfg["market"]["refresh_minutes"] * 60
    return data
