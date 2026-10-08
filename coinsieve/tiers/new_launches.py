"""New Launches tier: tokens 6-48h old that pass hard filters + RugCheck.

Membership = passed hard filters and RugCheck. Ranking gates (history, holder growth,
vol/liq ratio) only decide what gets posted, not membership.
"""
import logging

from coinsieve.filters import evaluate, pair_metrics
from coinsieve.ranking import rank_gate
from coinsieve.rugcheck import evaluate_report
from coinsieve.telegram import banned_hits, compose_post
from coinsieve.tiers import TierRun

log = logging.getLogger(__name__)

TIER = "new_launches"


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


def age_gate(store, evlog, cfg, now, run):
    """Split active tokens by age. too_old is permanent; too_young is logged once."""
    age = cfg["age"]
    min_s, max_s = age["min_hours"] * 3600, age["max_hours"] * 3600
    in_window = []
    for row in store.active():
        addr, launch = row["address"], row["launch_ts"]
        if launch is None:
            # launch <= first_seen, so once first_seen is past the window the token is too old
            if now - row["first_seen"] > max_s:
                store.set_status(addr, "too_old")
                evlog.write(addr, ["too_old: no pair data before window closed"], tier=TIER)
                run.counts["too_old"] += 1
                run.aged_out.add(addr)
            else:
                run.counts["no_pair_data_yet"] += 1
            continue
        age_s = now - launch
        if age_s > max_s:
            store.set_status(addr, "too_old")
            evlog.write(addr, [f"too_old: {age_s / 3600:.1f}h > {age['max_hours']}h"], tier=TIER)
            run.counts["too_old"] += 1
            run.aged_out.add(addr)
        elif age_s < min_s:
            if row["last_checked"] is None:
                evlog.write(addr, [f"too_young: {age_s / 3600:.1f}h < {age['min_hours']}h (queued)"], tier=TIER)
                store.mark_checked(addr, now)
            run.counts["waiting_too_young"] += 1
        else:
            in_window.append(row)
    store.commit()
    return in_window


def evaluate_tier(cfg, dex, rug, store, evlog, now):
    run = TierRun(TIER)
    chain = cfg["chain"]
    bonding_ids = set(cfg["launchpad"]["bonding_curve_dex_ids"])

    run.new = discover(dex, store, cfg, now)
    resolve_launch_times(dex, store, chain)
    in_window = age_gate(store, evlog, cfg, now, run)

    # Hard filters on main-pair data, then RugCheck only for tokens that pass them.
    pairs = dex.main_pairs(chain, [r["address"] for r in in_window])
    if in_window and not pairs:
        # Seen live: DexScreener answering 200 with empty data for every token (even BONK).
        # Don't log that as real rejections; tokens are re-evaluated next run.
        log.error("tokens/v1 returned no pairs for any of %d tokens - treating as API outage", len(in_window))
        run.outage = True
        return run

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
        metrics["tier_member"] = metrics["passed_rugcheck"]
        store.mark_checked(addr, now)
        run.results.append((metrics, reasons))
    store.commit()

    # Ranking gates + post wording decide what is postable (not membership).
    post_cfg = cfg["posting"]
    window_start = now - cfg["ranking"]["holder_growth_window_hours"] * 3600
    for m, reasons in run.results:
        if reasons:
            continue
        reasons += rank_gate(m, store.snapshots_since(m["address"], window_start), now, cfg["ranking"])
        if not reasons:
            text, prose = compose_post(m, post_cfg["disclaimer"])
            hits = banned_hits(prose, post_cfg["banned_words"])
            if hits:
                reasons.append(f"banned_word_in_post: {', '.join(hits)}")
            else:
                run.texts[m["address"]] = text
    for m, reasons in run.results:
        evlog.write(m["address"], reasons, m, tier=TIER)
    return run
