"""Coin Sieve: Solana token screener. Evaluates tiers, tracks tier membership, posts to Telegram.

Usage:
    python sieve.py [run]            # evaluate tiers, dry run (console only) — default
    python sieve.py run --post       # also post postable New Launches to the TEST chat
    python sieve.py telegram-check   # send one test line to the TEST chat
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import dotenv_values

from coinsieve import membership
from coinsieve.dexscreener import DexScreener
from coinsieve.evallog import EvalLog
from coinsieve.jupiter import Jupiter
from coinsieve.ranking import sort_key
from coinsieve.report import print_events, print_jupiter_tier_run, print_tier_run
from coinsieve.rugcheck import RugCheck
from coinsieve.store import Store
from coinsieve.telegram import Telegram, TelegramError
from coinsieve.tiers import jupiter_tiers, new_launches

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


ALL_TIERS = ("new_launches", "emerging", "established")


def cmd_run(cfg, chat, client, tiers):
    now = time.time()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    api = cfg["api"]
    store = Store(cfg["storage"]["db_path"])
    evlog = EvalLog(cfg["storage"]["log_dir"], run_id)

    runs = []
    if "new_launches" in tiers:
        dex = DexScreener(api["dexscreener_base"], api["timeout_s"], api["dexscreener_min_interval_s"], api["retries"])
        rug = RugCheck(api["rugcheck_base"], api["timeout_s"], api["rugcheck_min_interval_s"], api["retries"])
        runs.append(new_launches.evaluate_tier(cfg, dex, rug, store, evlog, now))
    jup_tiers = [t for t in jupiter_tiers.TIERS if t in tiers]
    if jup_tiers:
        jup = Jupiter(cfg["jupiter"]["base_url"], api["timeout_s"], api["jupiter_min_interval_s"], api["retries"])
        runs += jupiter_tiers.evaluate_tiers(cfg, jup, store, evlog, now, jup_tiers)

    events = []
    for run in runs:
        events += membership.update(store, run, now, cfg["tiers"][run.tier], cfg.get("graduation"))

    for run in runs:
        if run.tier == "new_launches":
            print_tier_run(run_id, run, cfg)
        else:
            print_jupiter_tier_run(run)
    print_events(events, {t: len(store.current_members(t)) for t in ALL_TIERS})

    nl = next((r for r in runs if r.tier == "new_launches"), None)
    if nl and not nl.outage:
        already = store.posted_to(chat)
        eligible = sorted((m for m, _ in nl.results if m["address"] in nl.texts and m["address"] not in already),
                          key=sort_key)
        remaining = max(0, cfg["posting"]["max_posts_per_day"] - store.posts_today(now, chat))
        publish(eligible[:remaining], nl.texts, eligible, remaining, store, cfg, now, chat, client)
    store.close()


def publish(to_post, texts, eligible, remaining, store, cfg, now, chat, client):
    """Dry run (client is None) prints the posts; with --post sends them to `chat`."""
    mode = "POSTING" if client else "DRY RUN - would post"
    print(f"\nPostable (not yet posted to this chat): {len(eligible)} | daily slots left: {remaining}"
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


def cmd_telegram_check(client, chat):
    try:
        msg_id = client.send(chat, "Coin Sieve: connection test.")
    except TelegramError as e:
        sys.exit(f"telegram-check failed: {e}")
    print(f"telegram-check ok: message_id={msg_id} (TEST chat)")


def main():
    parser = argparse.ArgumentParser(description="Coin Sieve")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command")
    p_run = sub.add_parser("run", help="evaluate tiers (default command)")
    p_run.add_argument("--post", action="store_true", help="post to the TEST chat (default: dry run)")
    p_run.add_argument("--tiers", default=",".join(ALL_TIERS),
                       help=f"comma-separated subset of {','.join(ALL_TIERS)} (default: all)")
    sub.add_parser("telegram-check", help="send one test line to the TEST chat")
    args = parser.parse_args()
    command = args.command or "run"
    post = getattr(args, "post", False)
    tiers = [t.strip() for t in getattr(args, "tiers", ",".join(ALL_TIERS)).split(",") if t.strip()]
    unknown = set(tiers) - set(ALL_TIERS)
    if unknown:
        sys.exit(f"Unknown tier(s): {', '.join(sorted(unknown))}")

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # token names contain emoji
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    setup_logging(cfg["storage"]["log_dir"])

    # Chat id is not secret: dry runs use it too, so dedupe/cap reflect the target chat.
    env = dotenv_values(".env")
    chat = env.get("TELEGRAM_TEST_CHAT_ID") or "dry-run"
    client = None
    if post or command == "telegram-check":
        token = env.get("TELEGRAM_BOT_TOKEN")
        if not token or chat == "dry-run":
            sys.exit("Needs TELEGRAM_BOT_TOKEN and TELEGRAM_TEST_CHAT_ID in .env")
        client = Telegram(token, cfg["api"]["timeout_s"])

    if command == "telegram-check":
        cmd_telegram_check(client, chat)
    else:
        cmd_run(cfg, chat, client, tiers)


if __name__ == "__main__":
    main()
