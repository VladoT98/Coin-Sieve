"""Console report for a run."""
from collections import Counter


def fmt_usd(v):
    return "-" if v is None else f"{v:,.0f}"


def print_tier_run(run_id, run, cfg):
    # postable first, then by how far each got (RugCheck pass, hard-filter pass); each by 24h volume
    results = sorted(run.results, key=lambda x: (bool(x[1]), not x[0].get("passed_rugcheck"),
                                                 not x[0].get("passed_hard_filters"),
                                                 -(x[0].get("volume_h24_usd") or 0)))
    hard_passed = sum(1 for m, _ in results if m.get("passed_hard_filters"))
    members = sum(1 for m, _ in results if m.get("tier_member"))
    print(f"\nCoin Sieve run {run_id} - tier: {run.tier}")
    print(f"  new tokens discovered: {run.new}")
    print(f"  waiting (<{cfg['age']['min_hours']}h): {run.counts['waiting_too_young']}"
          f" | no pair data yet: {run.counts['no_pair_data_yet']}"
          f" | newly too old (>{cfg['age']['max_hours']}h): {run.counts['too_old']}")
    if run.outage:
        print("\n  DexScreener returned no data for any in-window token (likely outage)."
              " Evaluation skipped; nothing logged as rejected; membership unchanged.")
        return
    print(f"  in age window: {len(results)} | passed hard filters: {hard_passed}"
          f" | passed RugCheck (tier members): {members} | postable: {len(run.texts)}\n")
    if not results:
        return

    hdr = (f"{'SYMBOL':<12}{'AGE_H':>6}{'LIQ_USD':>11}{'VOL24_USD':>12}{'TXNS24':>8}  {'DEX':<11}"
           f"{'SOC':>4}{'LOCK%':>7}{'TOP10%':>7}{'HOLD/H':>8}{'V/L':>6}  RESULT")
    print(hdr)
    print("-" * len(hdr))
    for m, reasons in results:
        if "age_h" not in m:
            print(f"{m['address'][:12]:<12}  {reasons[0]}")
            continue
        verdict = "POSTABLE" if not reasons else "; ".join(r.split(":")[0] for r in reasons)
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


def print_events(events, members_now):
    print(f"\nTier changes this run: {len(events)} | current members: "
          + ", ".join(f"{t}={n}" for t, n in members_now.items()))
    for e in events:
        detail = f"  ({e['detail']})" if e["detail"] else ""
        print(f"  {e['kind'].upper():<10}{e['tier']:<14}{(e['symbol'] or '?')[:12]:<13}{e['address']}{detail}")
