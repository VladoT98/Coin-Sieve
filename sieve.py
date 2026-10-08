"""Coin Sieve: Solana token screener. Evaluates tiers, tracks membership, posts Telegram digests.

Usage:
    python sieve.py [run] [--tiers new_launches,emerging,established]   # evaluate (default: all tiers)
    python sieve.py digest daily|weekly|alerts [--post]                  # dry run unless --post (TEST chat)
    python sieve.py telegram-check                                      # one test line to the TEST chat
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
from coinsieve.digest import alerts_text, blocked_reason, daily_text, weekly_text
from coinsieve.evallog import EvalLog
from coinsieve.jupiter import Jupiter
from coinsieve.ranking import sort_key
from coinsieve.report import print_events, print_jupiter_tier_run, print_tier_run
from coinsieve.rugcheck import RugCheck
from coinsieve.store import Store
from coinsieve.telegram import Telegram, TelegramError
from coinsieve.tiers import jupiter_tiers, new_launches

log = logging.getLogger("sieve")

ALL_TIERS = ("new_launches", "emerging", "established")


def setup_logging(log_dir):
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_h = logging.FileHandler(Path(log_dir) / "sieve.log", encoding="utf-8")
    file_h.setFormatter(fmt)
    console_h = logging.StreamHandler()
    console_h.setFormatter(fmt)
    console_h.setLevel(logging.WARNING)  # console shows the report; details go to file
    logging.basicConfig(level=logging.INFO, handlers=[file_h, console_h])


def cmd_run(cfg, tiers):
    now = time.time()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    api = cfg["api"]
    store = Store(cfg["storage"]["db_path"])
    evlog = EvalLog(cfg["storage"]["log_dir"], run_id)
    jup = Jupiter(cfg["jupiter"]["base_url"], api["timeout_s"], api["jupiter_min_interval_s"], api["retries"])

    runs = []
    if "new_launches" in tiers:
        dex = DexScreener(api["dexscreener_base"], api["timeout_s"], api["dexscreener_min_interval_s"], api["retries"])
        rug = RugCheck(api["rugcheck_base"], api["timeout_s"], api["rugcheck_min_interval_s"], api["retries"])
        runs.append(new_launches.evaluate_tier(cfg, dex, rug, jup, store, evlog, now))
    jup_tiers = [t for t in jupiter_tiers.TIERS if t in tiers]
    if jup_tiers:
        runs += jupiter_tiers.evaluate_tiers(cfg, jup, store, evlog, now, jup_tiers)

    events = []
    for run in runs:
        events += membership.update(store, run, now, cfg["tiers"][run.tier], cfg.get("graduation"))
        if not run.outage:
            store.save_latest(run.tier, run.results, now)
    store.commit()

    for run in runs:
        if run.tier == "new_launches":
            print_tier_run(run_id, run, cfg)
        else:
            print_jupiter_tier_run(run)
    print_events(events, {t: len(store.current_members(t)) for t in ALL_TIERS})
    store.close()


def publishable_events(store, events, pub):
    """Drop the initial tier fill, internal config exclusions and blocked names."""
    first = {}
    out = []
    for e in map(dict, events):
        if e["tier"] not in first:
            first[e["tier"]] = store.tier_first_event_ts(e["tier"])
        if e["ts"] <= first[e["tier"]] + pub["bootstrap_hours"] * 3600:
            continue
        if (e["detail"] or "").startswith("excluded_"):
            continue
        if blocked_reason({"symbol": e["symbol"], "name": ""}, pub):
            continue
        out.append(e)
    return out


def daily_candidates(store, chat, now, pub):
    members = store.current_members("new_launches")
    already = store.posted_to(chat)
    cands = []
    for addr, rec in store.latest("new_launches", list(members)).items():
        m = rec["metrics"]
        if addr in already or not m.get("postable"):
            continue
        if now - rec["ts"] > pub["daily_max_metrics_age_minutes"] * 60:
            log.info("daily digest: %s skipped, metrics stale", addr)
            continue
        reason = blocked_reason(m, pub)
        if reason:
            log.info("daily digest: %s skipped, %s", addr, reason)
            continue
        cands.append(m)
    return sorted(cands, key=sort_key)[:pub["daily_max_tokens"]]


def cmd_digest(cfg, kind, chat, client):
    now = time.time()
    pub = cfg["publishing"]
    store = Store(cfg["storage"]["db_path"])
    d = datetime.fromtimestamp(now, timezone.utc)
    used, event_ids = [], []

    if kind == "daily":
        period = d.strftime("%Y-%m-%d")
        cands = daily_candidates(store, chat, now, pub)
        if not cands and not pub["post_zero_summary"]:
            print("Daily digest: no postable New Launches (post_zero_summary is off) - nothing to send.")
            return store.close()
        text, used = daily_text(cands, now, pub)
    elif kind == "weekly":
        period = f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"
        start = now - 7 * 86400
        text = weekly_text(publishable_events(store, store.events_since(start), pub), now, start, pub)
    else:  # alerts
        period = str(int(now))
        posted = store.posted_event_ids(chat)
        events = [e for e in publishable_events(
            store, store.events_since(now - pub["alerts_lookback_hours"] * 3600, "established"), pub)
            if e["id"] not in posted and e["kind"] in ("entered", "left")]
        if not events:
            print("Established alerts: no new tier changes - nothing to send.")
            return store.close()
        text, event_ids = alerts_text(events, pub), [e["id"] for e in events]

    if store.digest_sent(kind, period, chat):
        print(f"[{kind} digest for {period} was already sent to this chat]")
        if client:
            return store.close()
    print(f"--- {kind} digest ({len(text)} chars) {'POSTING' if client else 'DRY RUN'} ---\n{text}")
    if client:
        try:
            msg_id = client.send(chat, text)
        except TelegramError as e:
            log.error("%s digest failed: %s", kind, e)
            store.close()
            sys.exit(f"[post FAILED: {e}]")
        store.add_digest(kind, period, chat, now, msg_id)
        for m in used:
            store.add_post(m["address"], now, chat, msg_id)
        store.add_posted_events(event_ids, chat, now)
        log.info("%s digest posted, message_id=%s", kind, msg_id)
        print(f"[posted, message_id={msg_id}]")
    store.close()


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
    p_run.add_argument("--tiers", default=",".join(ALL_TIERS),
                       help=f"comma-separated subset of {','.join(ALL_TIERS)} (default: all)")
    p_dig = sub.add_parser("digest", help="build a Telegram digest")
    p_dig.add_argument("kind", choices=["daily", "weekly", "alerts"])
    p_dig.add_argument("--post", action="store_true", help="send to the TEST chat (default: dry run)")
    sub.add_parser("telegram-check", help="send one test line to the TEST chat")
    args = parser.parse_args()
    command = args.command or "run"

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # token names contain emoji
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    setup_logging(cfg["storage"]["log_dir"])

    if command == "run":
        tiers = [t.strip() for t in getattr(args, "tiers", ",".join(ALL_TIERS)).split(",") if t.strip()]
        unknown = set(tiers) - set(ALL_TIERS)
        if unknown:
            sys.exit(f"Unknown tier(s): {', '.join(sorted(unknown))}")
        return cmd_run(cfg, tiers)

    # Chat id is not secret: dry runs use it too, so dedupe reflects the target chat.
    env = dotenv_values(".env")
    chat = env.get("TELEGRAM_TEST_CHAT_ID") or "dry-run"
    client = None
    if getattr(args, "post", False) or command == "telegram-check":
        token = env.get("TELEGRAM_BOT_TOKEN")
        if not token or chat == "dry-run":
            sys.exit("Needs TELEGRAM_BOT_TOKEN and TELEGRAM_TEST_CHAT_ID in .env")
        client = Telegram(token, cfg["api"]["timeout_s"])

    if command == "telegram-check":
        cmd_telegram_check(client, chat)
    else:
        cmd_digest(cfg, args.kind, chat, client)


if __name__ == "__main__":
    main()
