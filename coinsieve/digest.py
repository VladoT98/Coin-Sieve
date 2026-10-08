"""Telegram digest texts: daily New Launches, weekly tier changes, Established alerts.

Plain text (no parse_mode). Every text: neutral facts, site link (if configured), disclaimer.
Banned/blocked words are checked on prose only — addresses/URLs are base58/random and can
contain e.g. "gem" by chance.
"""
from datetime import datetime, timezone

from coinsieve.sanitize import clean_text, word_hits

TG_LIMIT = 4096
TIER_LABEL = {"new_launches": "New Launches", "emerging": "Emerging", "established": "Established"}


def fmt_usd(v):
    if v is None:
        return "n/a"
    return f"${v / 1e6:,.2f}M" if v >= 1e6 else f"${v:,.0f}"


def _date(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y")


def display_name(m, pub):
    sym = clean_text(m.get("symbol"), pub["max_symbol_len"]) or "?"
    name = clean_text(m.get("name"), pub["max_name_len"])
    return sym, name


def blocked_reason(m, pub):
    """Reason a token can't be published (banned/blocked word in its name), or None."""
    sym, name = display_name(m, pub)
    hits = word_hits(f"{sym} {name}", pub["banned_words"] + pub["blocked_words"])
    return f"blocked_name: {', '.join(hits)}" if hits else None


def _footer(pub):
    lines = [f"All tiers: {pub['site_url']}"] if pub.get("site_url") else []
    return lines + [pub["disclaimer"]]


def _check_prose(prose_lines, pub):
    hits = word_hits("\n".join(prose_lines), pub["banned_words"])
    if hits:  # template bug, not token data (names are filtered earlier)
        raise ValueError(f"banned word in digest template: {hits}")


def token_block(i, m, pub):
    """(prose lines, full lines) for one New Launch in the daily digest."""
    sym, name = display_name(m, pub)
    growth = m.get("holder_growth_per_h")
    warns = [r.split(":", 1)[1] for r in m.get("rc_risks", []) if r.startswith("warn:")]
    prose = [
        f"{i}) {sym}" + (f" - {name}" if name and name != sym else ""),
        f"Age {m['age_h']:.0f}h · Liquidity {fmt_usd(m['liquidity_usd'])} "
        f"({m['rc_lp_locked_pct']:.1f}% of LP locked or burned)",
        f"24h volume {fmt_usd(m['volume_h24_usd'])} · 24h trades {m['txns_h24']:,}",
        f"Holders {m['holders']:,}" + (f" ({growth:+,.0f}/h over {m['history_h']:.1f}h)" if growth is not None else "")
        + f" · Top 10 (excl. pools) {m['rc_top10_holders_pct']:.1f}%",
        "Mint and freeze authority revoked · RugCheck warnings: " + (", ".join(warns) if warns else "none"),
    ]
    full = [prose[0], m["address"], *prose[1:], m["url"]]
    return prose, full


def daily_text(tokens, now, pub):
    """tokens: ranked metrics dicts (already filtered for blocked names). Returns (text, used)."""
    head = [f"Coin Sieve · New Launches · {_date(now)}",
            "Solana tokens 6-48h old that passed every filter."]
    if not tokens:
        prose = head[:1] + ["0 tokens passed the filter today."] + _footer(pub)
        _check_prose(prose, pub)
        return "\n".join(prose), []
    used, blocks, prose_all = [], [], list(head)
    for m in tokens:
        prose, full = token_block(len(used) + 1, m, pub)
        if word_hits("\n".join(prose), pub["banned_words"]):
            continue  # e.g. a RugCheck warning name containing a banned word
        candidate = "\n\n".join(["\n".join(head), *blocks, "\n".join(full), "\n".join(_footer(pub))])
        if len(candidate) > TG_LIMIT:
            break
        blocks.append("\n".join(full))
        prose_all += prose
        used.append(m)
    _check_prose(prose_all + _footer(pub), pub)
    return "\n\n".join(["\n".join(head), *blocks, "\n".join(_footer(pub))]), used


def _names(events, pub, with_detail=False, cap=15):
    out = []
    for e in events[:cap]:
        sym = clean_text(e["symbol"], pub["max_symbol_len"]) or "?"
        if with_detail and e["detail"]:
            codes = sorted({d.split(":")[0].strip() for d in e["detail"].split(";")})
            sym += f" ({', '.join(codes)})"
        out.append(sym)
    if len(events) > cap:
        out.append(f"+{len(events) - cap} more")
    return ", ".join(out)


def weekly_text(events, now, start, pub):
    """events: tier_events rows of the week (bootstrap + blocked names already removed)."""
    by = lambda tier, kind: [e for e in events if e["tier"] == tier and e["kind"] == kind]  # noqa: E731
    prose = [f"Coin Sieve · weekly tier changes · {_date(start)} - {_date(now)}", ""]
    grads = by("emerging", "graduated")
    prose.append("Graduated (New Launches -> Emerging): " + (_names(grads, pub) if grads else "none"))
    for tier in ("emerging", "established"):
        entered, left = by(tier, "entered"), by(tier, "left")
        prose.append(f"{TIER_LABEL[tier]} - entered: {_names(entered, pub) if entered else 'none'}"
                     f" · left: {_names(left, pub, with_detail=True) if left else 'none'}")
    prose.append(f"New Launches listed this week: {len(by('new_launches', 'entered'))}")
    prose += [""] + _footer(pub)
    _check_prose(prose, pub)
    return "\n".join(prose)[:TG_LIMIT]


def alerts_text(events, pub):
    """Established-tier entries/exits since the last alert."""
    entered = [e for e in events if e["kind"] == "entered"]
    left = [e for e in events if e["kind"] == "left"]
    prose = ["Coin Sieve · Established tier update", ""]
    if entered:
        prose.append(f"Entered: {_names(entered, pub)}")
    if left:
        prose.append(f"Left: {_names(left, pub, with_detail=True)}")
    prose += [""] + _footer(pub)
    _check_prose(prose, pub)
    return "\n".join(prose)[:TG_LIMIT]
