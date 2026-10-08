"""Coin Sieve: discover new Solana tokens (DexScreener), age + hard filters, RugCheck,
rank, and post up to the daily cap to the Telegram TEST chat.

Usage:
    python sieve.py            # dry run (console only) — default
    python sieve.py --post     # post to the TEST chat from .env
"""
import argparse
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import dotenv_values

from coinsieve.dexscreener import DexScreener
from coinsieve.evallog import EvalLog
from coinsieve.filters import evaluate, pair_metrics
from coinsieve.ranking import rank_gate, sort_key
from coinsieve.rugcheck import RugCheck, evaluate_report
from coinsieve.store import Store
from coinsieve.telegram import Telegram, TelegramError, banned_hits, compose_post

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


def run(cfg, chat, client):
    now = time.time()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    api, age, chain = cfg["api"], cfg["age"], cfg["chain"]
    min_s, max_s = age["min_hours"] * 3600, age["max_hours"] * 3600
    bonding_ids = set(cfg["launchpad"]["bonding_curve_dex_ids"])

    dex = DexScreener(api["dexscreener_base"], api["timeout_s"], api["dexscreener_min_interval_s"], api["retries"])
    rug = RugCheck(api["rugcheck_base"], api["timeout_s"], api["rugcheck_min_interval_s"], api["retries"])
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

    # Hard filters on main-pair data, then RugCheck only for tokens that pass them.
    pairs = dex.main_pairs(chain, [r["address"] for r in in_window])
    if in_window and not pairs:
        # Seen live: DexScreener answering 200 with empty data for every token (even BONK).
        # Don't log that as real rejections; tokens are re-evaluated next run.
        log.error("tokens/v1 returned no pairs for any of %d tokens - treating as API outage", len(in_window))
        print(f"\nDexScreener returned no data for all {len(in_window)} in-window tokens "
              "(likely outage). Evaluation skipped; nothing logged as rejected.")
        store.close()
        return
    results = []
    for row in in_window:
        addr, pair = row["address"], pairs.get(row["address"])
        if pair is None:
            metrics, reasons = {"address": addr}, ["no_pair_data: not returned by tokens/v1"]
        else:
            metrics = pair_metrics(pair, row["launch_ts"], now, bonding_ids)
            reasons = evaluate(metrics, cfg["filters"])
        metrics["passed_hard_filters"] = not reasons
        if not reasons:
            try:
                rc_metrics, reasons = evaluate_report(rug.report(addr), cfg["rugcheck"])
                metrics.update(rc_metrics)
            except Exception as e:
                log.warning("rugcheck %s failed: %s", addr, e)
                reasons = [f"rugcheck_failed: {e}"]
            if metrics.get("rc_total_holders") is not None:
                store.add_snapshot(addr, now, metrics["rc_total_holders"],
                                   metrics["liquidity_usd"], metrics["volume_h24_usd"])
        metrics["passed_rugcheck"] = metrics["passed_hard_filters"] and not reasons
        store.mark_checked(addr, now)
        results.append((metrics, reasons))
    store.commit()

    # Stage 3: ranking gates + post wording for tokens that passed everything so far.
    post_cfg, texts = cfg["posting"], {}
    window_start = now - cfg["ranking"]["holder_growth_window_hours"] * 3600
    for m, reasons in results:
        if reasons:
            continue
        reasons += rank_gate(m, store.snapshots_since(m["address"], window_start), now, cfg["ranking"])
        if not reasons:
            text, prose = compose_post(m, post_cfg["disclaimer"])
            hits = banned_hits(prose, post_cfg["banned_words"])
            if hits:
                reasons.append(f"banned_word_in_post: {', '.join(hits)}")
            else:
                texts[m["address"]] = text
    for m, reasons in results:
        evlog.write(m["address"], reasons, m)

    already = store.posted_to(chat)
    eligible = sorted((m for m, r in results if not r and m["address"] not in already), key=sort_key)
    remaining = max(0, post_cfg["max_posts_per_day"] - store.posts_today(now, chat))
    to_post = eligible[:remaining]

    report(run_id, new, counts, results, cfg)
    publish(to_post, texts, eligible, remaining, store, cfg, now, chat, client)
    store.close()


def publish(to_post, texts, eligible, remaining, store, cfg, now, chat, client):
    """Dry run (client is None) prints the posts; with --post sends them to `chat`."""
    mode = "POSTING" if client else "DRY RUN - would post"
    print(f"\nEligible (not yet posted to this chat): {len(eligible)} | daily slots left: {remaining}"
          f" | {mode}: {len(to_post)}")
    for i, m in enumerate(to_post, 1):
        print(f"\n--- post {i}/{len(to_post)} ---\n{texts[m['address']]}")
        if client:
            try:
                msg_id = client.send(chat, texts[m["address"]])
            except TelegramError as e:
                log.error("telegram post %s failed: %s", m["address"], e)
                print(f"[post FAILED: {e}]")
                continue
            store.add_post(m["address"], now, chat, msg_id)
            store.commit()
            log.info("posted %s (%s) message_id=%s", m["symbol"], m["address"], msg_id)
            print(f"[posted, message_id={msg_id}]")

    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    if (not eligible and cfg["posting"]["post_zero_summary"] and store.posts_today(now, chat) == 0
            and not store.zero_notice_sent(day, chat)):
        line = "Coin Sieve: 0 tokens passed the filter."
        print(f"\n{'Posting' if client else 'DRY RUN - would post'}: {line}")
        if client:
            try:
                client.send(chat, line)
                store.add_zero_notice(day, chat, now)
            except TelegramError as e:
                log.error("telegram zero notice failed: %s", e)


def fmt_usd(v):
    return "-" if v is None else f"{v:,.0f}"


def report(run_id, new, counts, results, cfg):
    # eligible first, then by how far each got (RugCheck pass, hard-filter pass); each by 24h volume
    results = sorted(results, key=lambda x: (bool(x[1]), not x[0].get("passed_rugcheck"),
                                             not x[0].get("passed_hard_filters"),
                                             -(x[0].get("volume_h24_usd") or 0)))
    hard_passed = sum(1 for m, _ in results if m.get("passed_hard_filters"))
    rc_passed = sum(1 for m, _ in results if m.get("passed_rugcheck"))
    print(f"\nCoin Sieve run {run_id}")
    print(f"  new tokens discovered: {new}")
    print(f"  waiting (<{cfg['age']['min_hours']}h): {counts['waiting_too_young']}"
          f" | no pair data yet: {counts['no_pair_data_yet']}"
          f" | newly too old (>{cfg['age']['max_hours']}h): {counts['too_old']}")
    print(f"  in age window: {len(results)} | passed hard filters: {hard_passed}"
          f" | passed RugCheck: {rc_passed}\n")

    if results:
        hdr = (f"{'SYMBOL':<12}{'AGE_H':>6}{'LIQ_USD':>11}{'VOL24_USD':>12}{'TXNS24':>8}  {'DEX':<11}"
               f"{'SOC':>4}{'LOCK%':>7}{'TOP10%':>7}{'HOLD/H':>8}{'V/L':>6}  RESULT")
        print(hdr)
        print("-" * len(hdr))
        for m, reasons in results:
            if "age_h" not in m:
                print(f"{m['address'][:12]:<12}  {reasons[0]}")
                continue
            verdict = "PASS" if not reasons else "; ".join(r.split(":")[0] for r in reasons)
            dash = lambda k: "-" if m.get(k) is None else m[k]  # noqa: E731
            print(f"{(m['symbol'] or '?')[:11]:<12}{m['age_h']:>6.1f}{fmt_usd(m['liquidity_usd']):>11}"
                  f"{fmt_usd(m['volume_h24_usd']):>12}{m['txns_h24']:>8}  {(m['dex_id'] or '?')[:10]:<11}"
                  f"{len(m['socials']):>4}{dash('rc_lp_locked_pct'):>7}{dash('rc_top10_holders_pct'):>7}"
                  f"{dash('holder_growth_per_h'):>8}{dash('vol_liq_ratio'):>6}  {verdict}")

        tally = Counter(r.split(":")[0] for _, reasons in results for r in reasons)
        if tally:
            print("\nRejection reasons (a token can have several):")
            for code, n in tally.most_common():
                print(f"  {code:<22}{n}")


def main():
    parser = argparse.ArgumentParser(description="Coin Sieve")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "telegram-check"],
                        help="run: evaluate tokens (default); telegram-check: send one test line")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--post", action="store_true", help="post to Telegram (default: dry run)")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # token names contain emoji
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    setup_logging(cfg["storage"]["log_dir"])

    # Chat id is not secret: dry runs use it too, so dedupe/cap reflect the target chat.
    env = dotenv_values(".env")
    chat = env.get("TELEGRAM_TEST_CHAT_ID") or "dry-run"
    client = None
    if args.post or args.command == "telegram-check":
        token = env.get("TELEGRAM_BOT_TOKEN")
        if not token or chat == "dry-run":
            sys.exit("Needs TELEGRAM_BOT_TOKEN and TELEGRAM_TEST_CHAT_ID in .env")
        client = Telegram(token, cfg["api"]["timeout_s"])

    if args.command == "telegram-check":
        try:
            msg_id = client.send(chat, "Coin Sieve: connection test.")
        except TelegramError as e:
            sys.exit(f"telegram-check failed: {e}")
        print(f"telegram-check ok: message_id={msg_id} (TEST chat)")
        return
    run(cfg, chat, client)


if __name__ == "__main__":
    main()
