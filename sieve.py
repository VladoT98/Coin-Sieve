"""Coin Sieve: Solana token screener. Evaluates tiers, tracks membership, posts Telegram digests.

Usage:
    python sieve.py [run] [--tiers new_launches,emerging,established]   # evaluate (default: all tiers)
    python sieve.py digest daily|weekly|alerts [--post]                  # dry run unless --post (TEST chat)
    python sieve.py telegram-check                                      # one test line to the TEST chat
    python sieve.py track-record                                        # what happened after tier entries
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import dotenv_values

from coinsieve import card, funnel, logos, membership, track_record
from coinsieve.dexscreener import DexScreener
from coinsieve.digest import (TIER_LABEL, alerts_text, blocked_reason, card_stats, daily_caption, display_name,
                              record_line, weekly_caption, zero_text)
from coinsieve.sanitize import clean_text
from coinsieve.evallog import EvalLog
from coinsieve.geckoterminal import GeckoTerminal
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
            store.save_run_stats(run.tier, funnel.stats(run), now)
    store.commit()
    recorded = track_record.update(store, jup, now, cfg["track_record"])

    for run in runs:
        if run.tier == "new_launches":
            print_tier_run(run_id, run, cfg)
        else:
            print_jupiter_tier_run(run)
    print_events(events, {t: len(store.current_members(t)) for t in ALL_TIERS})
    print(f"Track record snapshots recorded this run: {recorded}")
    store.close()


def bootstrap_end(store, tier, pub):
    first = store.tier_first_event_ts(tier)
    return float("-inf") if first is None else first + pub["bootstrap_hours"] * 3600


def cmd_track_record(cfg):
    store, pub, tr = Store(cfg["storage"]["db_path"]), cfg["publishing"], cfg["track_record"]
    print("Track record (all entries since tracking began; initial tier fill excluded)\n")
    hdr = (f"{'TIER':<14}{'CHECKPOINT':<11}{'N':>5}{'IN TIER':>9}{'GONE':>6}{'MED PRICE':>11}"
           f"{'MED LIQ':>9}{'MED HOLD':>10}{'LIQ<-50%':>10}{'PRICE UP':>10}")
    print(hdr)
    print("-" * len(hdr))
    pct = lambda v: "-" if v is None else f"{v:+.0f}%"  # noqa: E731
    for tier in ALL_TIERS:
        rows = store.outcomes(tier)
        for cp in (c for c in tr["checkpoints_hours"] if tr["checkpoints_hours"][c] > 0):
            s = track_record.summarize(rows, cp, bootstrap_end(store, tier, pub), tr)
            if not s["n"]:
                print(f"{tier:<14}{cp:<11}{0:>5}   (no completed checkpoints yet)")
                continue
            print(f"{tier:<14}{cp:<11}{s['n']:>5}{s['in_a_tier']:>9}{s['not_found']:>6}"
                  f"{pct(s['median_price_change_pct']):>11}{pct(s['median_liquidity_change_pct']):>9}"
                  f"{pct(s['median_holder_change_pct']):>10}{s['liquidity_down_50pct']:>10}{s['price_up']:>10}")
    print(f"\nBaselines recorded so far: {sum(1 for t in ALL_TIERS for r in store.outcomes(t) if r['checkpoint'] == '0h')}")
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


def fresh_members(store, tier, now, pub):
    """Latest metrics of current members: skips stale data and names we won't publish."""
    out = []
    for addr, rec in store.latest(tier, list(store.current_members(tier))).items():
        if now - rec["ts"] > pub["daily_max_metrics_age_minutes"] * 60:
            log.info("daily card: %s %s skipped, metrics stale", tier, addr)
            continue
        reason = blocked_reason(rec["metrics"], pub)
        if reason:
            log.info("daily card: %s %s skipped, %s", tier, addr, reason)
            continue
        out.append(rec["metrics"])
    return out


def pick_daily(store, chat, now, pub, preview=False):
    """[(tier, [metrics])]: New Launches never repeat per chat; Emerging/Established rotate
    (not re-featured within feature_cooldown_days), ranked by 24h holder growth, then volume.
    preview=True ignores post history (design previews)."""
    n = pub["per_tier"]
    already = set() if preview else store.posted_to(chat)
    nl = sorted((m for m in fresh_members(store, "new_launches", now, pub)
                 if m.get("postable") and m["address"] not in already), key=sort_key)[:n]
    picks = [("new_launches", nl)]
    since = now - pub["feature_cooldown_days"] * 86400
    for tier in ("emerging", "established"):
        recent = set() if preview else store.featured_since(tier, chat, since)
        ms = [m for m in fresh_members(store, tier, now, pub) if m["address"] not in recent]
        ms.sort(key=lambda m: (m.get("holder_change_24h_pct") is None, -(m.get("holder_change_24h_pct") or 0),
                               -(m.get("volume_h24_usd") or 0)))
        picks.append((tier, ms[:n]))
    return picks


def card_funnels(store, picks):
    """[(tier, {"stages", "reasons"} | None)] from each tier's latest run stats."""
    out = []
    for tier, ms in picks:
        s = store.latest_run_stats(tier)
        if not s:
            out.append((tier, None))
            continue
        reasons = {}
        for code, n in s["reasons"].items():  # merge codes that share a public label
            reasons[funnel.label(code)] = reasons.get(funnel.label(code), 0) + n
        out.append((tier, {"stages": funnel.stages(tier, s, len(ms)),
                           "reasons": sorted(reasons.items(), key=lambda kv: -kv[1])}))
    return out


def build_card(cfg, store, picks, now, period):
    """Render the daily PNG. Pool addresses come from one DexScreener batch call; GeckoTerminal is
    only asked for price history (its free limit is tight). New Launches get letter badges, never
    their own (unvetted) logos."""
    api, pub = cfg["api"], cfg["publishing"]
    dex = DexScreener(api["dexscreener_base"], api["timeout_s"], api["dexscreener_min_interval_s"], api["retries"])
    gt = GeckoTerminal(api["geckoterminal_base"], api["timeout_s"], api["geckoterminal_min_interval_s"],
                       api["retries"], backoff_s=api["geckoterminal_backoff_s"])
    pairs = dex.main_pairs(cfg["chain"], [m["address"] for _, ms in picks for m in ms])
    sections = []
    for tier, ms in picks:
        items = []
        for m in ms:
            pool = (pairs.get(m["address"]) or {}).get("pairAddress") or m.get("pair_address")
            sym, name = display_name(m, pub)
            items.append({"symbol": sym, "name": name, "stats": card_stats(tier, m),
                          "series": gt.hourly_closes(pool, pub["chart_hours"]) if pool else None,
                          "avatar": logos.avatar(m, tier, card.TIER_STYLE[tier][2], cfg["storage"]["logo_dir"],
                                                 use_logo=tier in pub["logo_tiers"])})
        sections.append((tier, items))
    out_dir = Path(cfg["storage"]["card_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    return card.render_daily(sections, card_funnels(store, picks), now, str(out_dir / f"daily-{period}.png"),
                             pub["disclaimer"])


def build_weekly_card(cfg, store, events, record_lines, start, now, period):
    pub = cfg["publishing"]
    by = lambda tier, kind: [e for e in events if e["tier"] == tier and e["kind"] == kind]  # noqa: E731
    sym = lambda e: clean_text(e["symbol"], pub["max_symbol_len"]) or "?"  # noqa: E731

    def names(evs, cap=8):
        s = ", ".join(sym(e) for e in evs[:cap])
        return s + (f" +{len(evs) - cap} more" if len(evs) > cap else "")

    tracked = store.tokens_seen_since(start)
    listed = len(by("new_launches", "entered"))
    tiles = [(f"{tracked:,}", "new tokens tracked"), (f"{listed:,}", "passed New Launches filters"),
             (f"{listed / tracked * 100:.1f}%" if tracked else "-", "pass rate"),
             (f"{len(by('emerging', 'graduated'))}", "graduated to Emerging")]
    blocks = []
    for tier in ("emerging", "established"):
        lines = []
        if by(tier, "entered"):
            lines.append("Entered: " + names(by(tier, "entered")))
        if by(tier, "left"):
            lines.append("Left: " + names(by(tier, "left")))
        blocks.append((tier, f"{card.TIER_STYLE[tier][0]} - tier changes", lines))
    blocks.append((None, "Track record", [r.lstrip("• ") for r in record_lines]))
    out_dir = Path(cfg["storage"]["card_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    return card.render_weekly(tiles, blocks, start, now, str(out_dir / f"weekly-{period}.png"), pub["disclaimer"])


def cmd_digest(cfg, kind, chat, client, preview=False):
    now = time.time()
    pub = cfg["publishing"]
    store = Store(cfg["storage"]["db_path"])
    d = datetime.fromtimestamp(now, timezone.utc)
    picks, event_ids, photo = [], [], None

    if kind == "daily":
        period = d.strftime("%Y-%m-%d")
        if client and not preview and store.digest_sent(kind, period, chat):
            print(f"[daily card for {period} was already sent to this chat]")
            return store.close()
        picks = pick_daily(store, chat, now, pub, preview)
        if any(ms for _, ms in picks):
            text = daily_caption(picks, now, pub)
            print("Building chart card (price history is fetched slowly to respect GeckoTerminal limits)...")
            photo = build_card(cfg, store, picks, now, period)
        elif pub["post_zero_summary"]:
            text = zero_text(now, pub)
        else:
            print("Daily card: nothing to show (post_zero_summary is off) - nothing to send.")
            return store.close()
    elif kind == "weekly":
        period = f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"
        start = now - 7 * 86400
        tr = cfg["track_record"]
        week_before = (now - 14 * 86400, now - 7 * 86400)  # their 7d checkpoint completed this week
        lines = [record_line(f"{TIER_LABEL[t]} that entered 7-14 days ago, 7 days later",
                             track_record.summarize(store.outcomes(t), "7d", bootstrap_end(store, t, pub), tr,
                                                    week_before))
                 for t in ("new_launches", "emerging")]
        events = publishable_events(store, store.events_since(start), pub)
        text = weekly_caption(events, now, start, pub, store.tokens_seen_since(start))
        photo = build_weekly_card(cfg, store, events, lines, start, now, period)
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

    if not preview and store.digest_sent(kind, period, chat):
        print(f"[{kind} digest for {period} was already sent to this chat]")
        if client:
            return store.close()
    print(f"--- {kind} digest ({len(text)} chars) {'POSTING' if client else 'DRY RUN'} ---\n{text}")
    if photo:
        print(f"[card image: {photo}]")
    if client:
        try:
            msg_id = client.send_photo(chat, photo, text) if photo else client.send(chat, text, parse_mode="HTML")
        except TelegramError as e:
            log.error("%s digest failed: %s", kind, e)
            store.close()
            sys.exit(f"[post FAILED: {e}]")
        if preview:  # design preview: nothing recorded, normal schedule unaffected
            print(f"[preview posted, message_id={msg_id}; nothing recorded]")
            return store.close()
        store.add_digest(kind, period, chat, now, msg_id)
        for tier, ms in picks:
            for m in ms:
                if tier == "new_launches":
                    store.add_post(m["address"], now, chat, msg_id)
                else:
                    store.add_featured(m["address"], tier, chat, now, msg_id)
        store.add_posted_events(event_ids, chat, now)
        log.info("%s digest posted, message_id=%s", kind, msg_id)
        print(f"[posted, message_id={msg_id}]")
    store.close()


def cmd_telegram_check(client, chat):
    try:
        msg_id = client.send(chat, "Coin Sieve: connection test.")  # plain text, no parse mode
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
    p_dig.add_argument("--preview", action="store_true",
                       help="daily only: ignore post history and record nothing (design previews)")
    sub.add_parser("telegram-check", help="send one test line to the TEST chat")
    sub.add_parser("track-record", help="print track-record stats per tier and checkpoint")
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
    if command == "track-record":
        return cmd_track_record(cfg)

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
        cmd_digest(cfg, args.kind, chat, client, args.preview)


if __name__ == "__main__":
    main()
