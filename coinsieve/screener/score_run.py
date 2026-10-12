"""`sieve.py score`: load the latest observations, compute the Quality score, store it, write CSV + HTML reports.

Reads only the screener DB (no API calls). Re-scoring a date replaces that date's scores; other days stay.
"""
import csv
import hashlib
import html
import json
import time
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

from coinsieve.sanitize import clean_text
from coinsieve.screener import scoring
from coinsieve.screener.db import ScreenerDB

LABELS = {
    "major_volume_mcap": "Major-exchange volume / mcap", "depth_mcap": "±2% order-book depth / mcap",
    "spread_pct": "Bid-ask spread (%)", "exchanges_count": "Exchanges", "major_exchanges_count": "Major exchanges",
    "tvl_mcap": "TVL / mcap", "chain_tvl": "Value locked on the chain (USD)",
    "chain_fees": "Fees paid on the chain, 30d (USD)", "chain_fees_trend": "Chain fees 30d vs previous 30d", "fees_mcap": "Fees (yearly pace) / mcap", "revenue_mcap": "Revenue (yearly pace) / mcap",
    "fees_trend": "Fees 30d vs previous 30d", "tvl_change_30d": "TVL change 30d (%)",
    "commits_90d": "Commits 90d", "contributors_90d": "Contributors 90d", "active_weeks_1y": "Active weeks (of 52)",
    "days_since_push": "Days since last push", "circulating_ratio": "Circulating / max supply",
    "fdv_mcap": "FDV / mcap", "supply_change_30d": "Supply change 30d (%)", "supply_change_1y": "Supply change 1y (%)",
    "rel_change_30d": "Price 30d vs BTC (pp)", "rel_change_200d": "Price 200d vs BTC (pp)",
    "ath_distance": "Distance from all-time high (%)", "rsi_14": "RSI 14d", "volatility_90d": "Volatility 90d (%)",
    "watchlist_mcap": "CoinGecko watchlists per $1M mcap",
}
CATEGORY_LABELS = {"liquidity": "Liquidity", "fundamentals": "Fundamentals", "development": "Development",
                   "tokenomics": "Tokenomics", "momentum": "Momentum"}


def config_hash(scfg):
    return hashlib.sha1(json.dumps(scfg, sort_keys=True).encode()).hexdigest()[:12]


def _shift(d, days):
    return (_date.fromisoformat(d) + timedelta(days=days)).isoformat()


def load(db, scfg, as_of, now_ts):
    """{cg_id: {"metrics", "sources", "facts", "passes", "fails", "symbol", "name"}} for the universe of as_of."""
    udate = db.conn.execute("SELECT MAX(date) FROM universe_daily WHERE date <= ?", (as_of,)).fetchone()[0]
    if not udate:
        return {}, None
    ids = sorted(db.included_on(udate))
    min_date = _shift(as_of, -scfg["max_metric_age_days"])

    def fresh_raw(cid):
        return {k: dict(r) for k, r in db.latest_metrics(cid, as_of).items() if r["date"] >= min_date}

    btc = fresh_raw("bitcoin")
    names = {r[0]: (r[1], r[2]) for r in db.conn.execute("SELECT cg_id, symbol, name FROM coins")}
    coins = {}
    for cid in ids:
        raw = fresh_raw(cid)
        prices = [p for p in db.prices(cid, _shift(as_of, -400)) if p[0] <= as_of]
        metrics, sources, facts = scoring.derive(raw, prices, btc, as_of)
        fails = scoring.filter_reasons(facts, metrics, scfg, now_ts, as_of)
        sym, name = names.get(cid, ("", cid))
        coins[cid] = {"metrics": metrics, "sources": sources, "facts": facts, "passes": not fails, "fails": fails,
                      "no_source": scoring.no_source(raw),
                      "symbol": clean_text(sym or "", 12), "name": clean_text(name or cid, 40)}
    return coins, udate


def run_score(cfg, as_of=None, now=None, explain=None, csv_path=None, html_path=None, show_sensitivity=False,
              out=print):
    scfg = cfg["screener"]["scoring"]
    errs = scoring.validate(scfg)
    if errs:
        for e in errs:
            out(f"config error: {e}")
        return 1
    now = now or time.time()
    as_of = as_of or datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    db = ScreenerDB(cfg["screener"]["db_path"])
    try:
        coins, udate = load(db, scfg, as_of, now)
        if not coins:
            out(f"No universe saved on or before {as_of}: run `sieve.py fetch` first.")
            return 1
        results = scoring.score(coins, scfg)
        order = scoring.rank(results, coins)
        h = config_hash(scfg)
        db.save_scores(as_of, [
            (as_of, cid, results[cid]["total"], results[cid]["rank"], results[cid]["rank_all"],
             int(coins[cid]["passes"]), json.dumps(coins[cid]["fails"]), json.dumps(results[cid]["categories"]), h)
            for cid in order],
            [(as_of, cid, mid, v, results[cid]["pct"].get(mid), coins[cid]["sources"].get(mid))
             for cid in order for mid, v in coins[cid]["metrics"].items() if v is not None])
        db.commit()
        passing = [c for c in order if coins[c]["passes"]]
        out(f"Quality score {as_of} (universe of {udate}, config {h}): {len(coins)} coins, {len(passing)} pass "
            f"the screening criteria")
        print_table(order, coins, results, scfg, out, limit=scfg["list"]["leave_rank"])
        fail_counts = {}
        for c in order:
            for r in coins[c]["fails"]:
                key = r.split(" <")[0].split(" ")[0] if r.startswith(("age", "core")) else r
                fail_counts[key] = fail_counts.get(key, 0) + 1
        out("Not passing: " + ", ".join(f"{k} {v}" for k, v in sorted(fail_counts.items(), key=lambda x: -x[1])))
        sens = scoring.sensitivity(coins, scfg, scfg["list"]["enter_rank"]) if show_sensitivity or html_path else None
        if show_sensitivity:
            out(f"Weight sensitivity (coins of the top {scfg['list']['enter_rank']} that change at -5 / +5):")
            for cat, (lo, hi) in sens.items():
                out(f"  {cat:<13} {lo} / {hi}")
        if explain:
            explain_coin(explain, coins, results, scfg, out)
        if csv_path:
            write_csv(csv_path, order, coins, results, scfg)
            out(f"CSV written: {csv_path}")
        if html_path:
            write_html(html_path, as_of, order, coins, results, scfg, sens, h)
            out(f"Report written: {html_path}")
        return 0
    finally:
        db.close()


def _cat(cs):
    """Category score as text: "n/a" when left out of the total (missing_policy: exclude)."""
    return "n/a" if cs["score"] is None else f"{cs['score']:.0f}"


def _fmt(v):
    if v is None:
        return "–"
    if abs(v) >= 1e9:
        return f"{v / 1e9:,.1f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:,.1f}M"
    if abs(v) >= 100:
        return f"{v:,.0f}"
    if abs(v) >= 1:
        return f"{v:,.2f}"
    return f"{v:.4f}"


def print_table(order, coins, results, scfg, out, limit=40):
    cats = list(scfg["categories"])
    out(f"{'#':>3} {'Coin':<24} {'Score':>5}  " + " ".join(f"{c[:5]:>5}" for c in cats))
    shown = 0
    for cid in order:
        if not coins[cid]["passes"]:
            continue
        r = results[cid]
        flags = "".join("*" if r["categories"][c]["flag"] else " " for c in cats)
        out(f"{r['rank']:>3} {coins[cid]['symbol'] + ' ' + coins[cid]['name'][:18]:<24} {r['total']:5.1f}  "
            + " ".join(f"{_cat(r['categories'][c]):>5}" for c in cats) + f"  {flags.strip() and '* = see flags'}")
        shown += 1
        if shown >= limit:
            break


def explain_coin(cid, coins, results, scfg, out):
    if cid not in coins:
        out(f"{cid}: not in the universe")
        return
    c, r = coins[cid], results[cid]
    out(f"\n{c['symbol']} {c['name']} ({cid}): score {r['total']:.1f}, rank {r['rank'] or '–'} "
        f"(all coins: {r['rank_all']}); {'passes' if c['passes'] else 'fails: ' + '; '.join(c['fails'])}")
    for cat, w in scfg["categories"].items():
        cs = r["categories"][cat]
        out(f"  {CATEGORY_LABELS[cat]} {_cat(cs)} (weight {w}, {cs['used']}/{cs['of']} metrics"
            f"{', ' + cs['flag'] if cs['flag'] else ''})")
        for mid, d in scfg["metrics"].items():
            if d["category"] != cat:
                continue
            v, p = c["metrics"].get(mid), r["pct"].get(mid)
            out(f"    {LABELS.get(mid, mid):<36} {_fmt(v):>10}  pct {'–' if p is None else f'{p:3.0f}'}  "
                f"{c['sources'].get(mid) or ''}")
    for mid in scfg.get("info_metrics", []):
        out(f"    (info) {LABELS.get(mid, mid):<29} {_fmt(c['metrics'].get(mid)):>10}")


def write_csv(path, order, coins, results, scfg):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    mids = list(scfg["metrics"]) + list(scfg.get("info_metrics", []))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:   # BOM: Excel opens UTF-8 correctly
        w = csv.writer(f)
        w.writerow(["rank", "rank_all", "cg_id", "symbol", "name", "score", *scfg["categories"], "passes",
                    "fail_reasons", *[f"{m}" for m in mids], *[f"{m}_pct" for m in scfg["metrics"]]])
        for cid in order:
            c, r = coins[cid], results[cid]
            w.writerow([r["rank"] or "", r["rank_all"], cid, c["symbol"], c["name"], round(r["total"], 2),
                        *[None if r["categories"][k]["score"] is None else round(r["categories"][k]["score"], 1)
                          for k in scfg["categories"]], int(c["passes"]),
                        "; ".join(c["fails"]), *[c["metrics"].get(m) for m in mids],
                        *[None if r["pct"].get(m) is None else round(r["pct"][m], 1) for m in scfg["metrics"]]])


def write_html(path, as_of, order, coins, results, scfg, sens, h):
    """Self-contained review page: ranked table, per-coin breakdown with sources, sensitivity, method notes."""
    esc = html.escape
    cats = list(scfg["categories"])
    rows = []
    for cid in order:
        c, r = coins[cid], results[cid]
        bars = "".join(
            f'<td class="cat{" flag" if r["categories"][k]["flag"] else ""}" title="{esc(CATEGORY_LABELS[k])}: '
            f'{r["categories"][k]["used"]}/{r["categories"][k]["of"]} metrics'
            f'{" – " + r["categories"][k]["flag"] if r["categories"][k]["flag"] else ""}">'
            f'<span class="bar" style="width:{r["categories"][k]["score"] or 0:.0f}%"></span>'
            f'<span class="num">{_cat(r["categories"][k])}</span></td>' for k in cats)
        detail = []
        for k in cats:
            detail.append(f'<tr class="sub"><th colspan="4">{esc(CATEGORY_LABELS[k])} – '
                          f'{_cat(r["categories"][k])} (weight {scfg["categories"][k]}'
                          f'{", " + r["categories"][k]["flag"] if r["categories"][k]["flag"] else ""})</th></tr>')
            for mid, d in scfg["metrics"].items():
                if d["category"] != k:
                    continue
                v, p, s = c["metrics"].get(mid), r["pct"].get(mid), c["sources"].get(mid)
                link = f'<a href="{esc(s)}" target="_blank" rel="noopener">source</a>' if s else ""
                detail.append(f"<tr><td>{esc(LABELS.get(mid, mid))}</td><td class=r>{_fmt(v)}</td>"
                              f"<td class=r>{'–' if p is None else f'{p:.0f}'}</td><td>{link}</td></tr>")
        info = " · ".join(f"{esc(LABELS[m])}: {_fmt(c['metrics'].get(m))}" for m in scfg.get("info_metrics", []))
        status = ("passes" if c["passes"] else "fails: " + esc("; ".join(c["fails"])))
        rows.append(
            f'<tr class="{"pass" if c["passes"] else "fail"}"><td class=r>{r["rank"] or ""}</td>'
            f'<td><details><summary><b>{esc(c["symbol"])}</b> {esc(c["name"])}</summary>'
            f'<p class=st>{status}</p><table class=detail><tr><th>Metric</th><th>Value</th><th>Percentile</th>'
            f'<th></th></tr>{"".join(detail)}</table><p class=info>{info}</p></details></td>'
            f'<td class="r total">{r["total"]:.1f}</td>{bars}</tr>')
    sens_rows = "".join(f"<tr><td>{esc(CATEGORY_LABELS[k])}</td><td class=r>{lo}</td><td class=r>{hi}</td></tr>"
                        for k, (lo, hi) in (sens or {}).items())
    n_pass = sum(1 for c in coins.values() if c["passes"])
    weights = " · ".join(f"{CATEGORY_LABELS[k]} {w}" for k, w in scfg["categories"].items())
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Quality score review</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--mut:#6b6a66;--line:#e4e1da;--bar:#3f7d74;--flag:#c9a24a;--fail:#a14d3a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--fg:#ecebe6;--mut:#a3a19b;--line:#2f2e2b;--bar:#5fa89c}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:24px 16px}}
main{{max-width:1100px;margin:auto}} h1{{font-size:22px;margin:0 0 4px}} .mut{{color:var(--mut)}}
table{{border-collapse:collapse;width:100%}} th,td{{padding:6px 8px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:top}} .r{{text-align:right;font-variant-numeric:tabular-nums}}
.cat{{position:relative;width:11%;min-width:60px}} .bar{{position:absolute;left:0;top:30%;height:40%;
background:var(--bar);opacity:.25;border-radius:2px}} .num{{position:relative}} .cat.flag .bar{{background:var(--flag)}}
.cat.flag .num::after{{content:" *";color:var(--flag)}} tr.fail{{opacity:.55}} .total{{font-weight:600}}
summary{{cursor:pointer}} .detail{{margin:8px 0;font-size:13px}} .detail .sub th{{background:transparent;
padding-top:12px}} .st{{margin:6px 0;color:var(--mut)}} .info{{font-size:12px;color:var(--mut)}}
a{{color:var(--bar)}} .wrap{{overflow-x:auto}} footer{{margin-top:24px;font-size:12px;color:var(--mut)}}
</style></head><body><main>
<p><a href="./#/tokens">← Coin Sieve token list</a></p>
<h1>Quality score – {esc(as_of)}</h1>
<p class=mut>{len(coins)} coins in the universe · {n_pass} pass the screening criteria · weights: {esc(weights)}
· config {esc(h)}. Faded rows fail a screening criterion. * = a category with a flag (hover it): not enough data
(scored 50), no public code repository (scored 0) or not applicable (n/a, left out of the score).
Click a coin for its metrics, percentiles and sources.</p>
<div class=wrap><table><tr><th class=r>#</th><th>Coin</th><th class=r>Score</th>
{"".join(f"<th>{esc(CATEGORY_LABELS[k])}</th>" for k in cats)}</tr>{"".join(rows)}</table></div>
<h2>How stable is the top {scfg["list"]["enter_rank"]}?</h2>
<p class=mut>Coins that change in the top {scfg["list"]["enter_rank"]} when one category weight moves by 5 points
(the others scaled to keep 100).</p>
<table style="max-width:420px"><tr><th>Category</th><th class=r>−5</th><th class=r>+5</th></tr>{sens_rows}</table>
<h2>Method</h2><p>Each metric is ranked among all coins that have it (0 = lowest, 100 = highest; for metrics where
lower is better the scale is flipped). A category is the weighted mean of the metrics a coin has; missing metrics
are skipped. The score is the weighted sum of the categories. Scores are relative to the other coins.</p>
<footer>{esc(scfg["disclaimer"])} Passing the screening criteria is not a recommendation.<br>
Data provided by <a href="https://www.coingecko.com" target="_blank" rel="noopener">CoinGecko</a>,
<a href="https://defillama.com" target="_blank" rel="noopener">DefiLlama</a> and
<a href="https://github.com" target="_blank" rel="noopener">GitHub</a>.</footer>
</main></body></html>"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(page, encoding="utf-8")
