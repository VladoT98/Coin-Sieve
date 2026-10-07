"""Coin Sieve — Stage 1: discover new Solana tokens on DexScreener, apply age + hard filters.

Usage:
    python sieve.py            # dry run (console only) — default
    python sieve.py --post     # post to Telegram (not implemented until Stage 3)
"""
import argparse
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from coinsieve.dexscreener import DexScreener
from coinsieve.evallog import EvalLog
from coinsieve.filters import evaluate, pair_metrics
from coinsieve.store import Store

log = logging.getLogger("sieve")


def setup_logging(log_dir):
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_h = logging.FileHandler(Path(log_dir) / "sieve.log", encoding="utf-8")
    file_h.setFormatter(fmt)
    console_h = logging.StreamHandler()
    console_h.setFormatter(fmt)
    console_h.setLevel(logging.WARNING)  # console shows the report; details go to file
    logging.basicConfig(level=logging.INFO, handlers=[file_h, console_h])


def discover(dex, store, cfg, now):
    new = 0
    for source in cfg["discovery"]["sources"]:
        try:
            entries = dex.feed(source)
        except Exception as e:
            log.error("feed %s failed: %s", source, e)
            continue
        for e in entries:
            if e.get("chainId") == cfg["chain"] and e.get("tokenAddress"):
                new += store.add_seen(e["tokenAddress"], source, now)
    store.commit()
    return new


def resolve_launch_times(dex, store, chain):
    """Launch = earliest pairCreatedAt across ALL pairs. tokens/v1 only returns the main
    (post-migration) pair, so this needs token-pairs/v1 — one call per token, once."""
    for row in store.active():
        if row["launch_ts"] is not None:
            continue
        try:
            pairs = dex.token_pairs(chain, row["address"])
        except Exception as e:
            log.warning("token-pairs %s failed: %s", row["address"], e)
            continue
        created = [p["pairCreatedAt"] for p in pairs if p.get("pairCreatedAt")]
        if created:
            store.set_launch(row["address"], min(created) / 1000)
    store.commit()


def run(cfg, post):
    now = time.time()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    api, age, chain = cfg["api"], cfg["age"], cfg["chain"]
    min_s, max_s = age["min_hours"] * 3600, age["max_hours"] * 3600
    bonding_ids = set(cfg["launchpad"]["bonding_curve_dex_ids"])

    dex = DexScreener(api["dexscreener_base"], api["timeout_s"], api["min_interval_s"], api["retries"])
    store = Store(cfg["storage"]["db_path"])
    evlog = EvalLog(cfg["storage"]["log_dir"], run_id)

    new = discover(dex, store, cfg, now)
    resolve_launch_times(dex, store, chain)

    # Age gate. too_old is permanent; too_young is logged once, then waits silently.
    in_window, counts = [], Counter()
    for row in store.active():
        addr, launch = row["address"], row["launch_ts"]
        if launch is None:
            # launch <= first_seen, so once first_seen is past the window the token is too old
            if now - row["first_seen"] > max_s:
                store.set_status(addr, "too_old")
                evlog.write(addr, ["too_old: no pair data before window closed"])
                counts["too_old"] += 1
            else:
                counts["no_pair_data_yet"] += 1
            continue
        age_s = now - launch
        if age_s > max_s:
            store.set_status(addr, "too_old")
            evlog.write(addr, [f"too_old: {age_s / 3600:.1f}h > {age['max_hours']}h"])
            counts["too_old"] += 1
        elif age_s < min_s:
            if row["last_checked"] is None:
                evlog.write(addr, [f"too_young: {age_s / 3600:.1f}h < {age['min_hours']}h (queued)"])
                store.mark_checked(addr, now)
            counts["waiting_too_young"] += 1
        else:
            in_window.append(row)
    store.commit()

    # Hard filters on main-pair data for tokens inside the age window.
    pairs = dex.main_pairs(chain, [r["address"] for r in in_window])
    results = []
    for row in in_window:
        addr, pair = row["address"], pairs.get(row["address"])
        if pair is None:
            metrics, reasons = {"address": addr}, ["no_pair_data: not returned by tokens/v1"]
        else:
            metrics = pair_metrics(pair, row["launch_ts"], now, bonding_ids)
            reasons = evaluate(metrics, cfg["filters"])
        evlog.write(addr, reasons, metrics)
        store.mark_checked(addr, now)
        results.append((metrics, reasons))
    store.close()

    report(run_id, new, counts, results, cfg)
    if post:
        print("\n--post ignored: Telegram posting arrives in Stage 3.")


def fmt_usd(v):
    return "-" if v is None else f"{v:,.0f}"


def report(run_id, new, counts, results, cfg):
    # passes first, then by 24h volume
    results = sorted(results, key=lambda x: (bool(x[1]), -(x[0].get("volume_h24_usd") or 0)))
    passed = [m for m, r in results if not r]
    print(f"\nCoin Sieve run {run_id} (dry run)")
    print(f"  new tokens discovered: {new}")
    print(f"  waiting (<{cfg['age']['min_hours']}h): {counts['waiting_too_young']}"
          f" | no pair data yet: {counts['no_pair_data_yet']}"
          f" | newly too old (>{cfg['age']['max_hours']}h): {counts['too_old']}")
    print(f"  in age window: {len(results)} | passed hard filters: {len(passed)}\n")

    if results:
        hdr = f"{'SYMBOL':<12}{'AGE_H':>6}{'LIQ_USD':>11}{'VOL24_USD':>12}{'TXNS24':>8}  {'DEX':<11}{'SOC':>4}  RESULT"
        print(hdr)
        print("-" * len(hdr))
        for m, reasons in results:
            if "age_h" not in m:
                print(f"{m['address'][:12]:<12}  {reasons[0]}")
                continue
            verdict = "PASS" if not reasons else "; ".join(r.split(":")[0] for r in reasons)
            print(f"{(m['symbol'] or '?')[:11]:<12}{m['age_h']:>6.1f}{fmt_usd(m['liquidity_usd']):>11}"
                  f"{fmt_usd(m['volume_h24_usd']):>12}{m['txns_h24']:>8}  {(m['dex_id'] or '?')[:10]:<11}"
                  f"{len(m['socials']):>4}  {verdict}")

        tally = Counter(r.split(":")[0] for _, reasons in results for r in reasons)
        if tally:
            print("\nRejection reasons (a token can have several):")
            for code, n in tally.most_common():
                print(f"  {code:<20}{n}")

    if passed:
        print("\nPassed:")
        for m in passed:
            print(f"  {m['symbol']}  {m['address']}  {m['url']}")
    else:
        print("\n0 passed.")


def main():
    parser = argparse.ArgumentParser(description="Coin Sieve")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--post", action="store_true", help="post to Telegram (default: dry run)")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # token names contain emoji
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    setup_logging(cfg["storage"]["log_dir"])
    run(cfg, args.post)


if __name__ == "__main__":
    main()
