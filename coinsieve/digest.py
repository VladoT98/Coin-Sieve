"""Telegram digest texts (HTML parse mode): daily card caption, weekly tier changes, Established alerts.

Style: neutral facts, calm emojis as section markers only (no 🚀/💎/🔥 — hype, and 💎 = "gem"),
token names as links, contract addresses in <code> (tap-to-copy), disclaimer last.
Banned words are checked on the readable prose only: addresses/URLs are random base58 and can
contain e.g. "gem" by chance.
"""
import html
import re
from datetime import datetime, timezone

from coinsieve.card import compact_usd
from coinsieve.sanitize import clean_text, word_hits

CAPTION_LIMIT = 1024   # Telegram photo caption
TG_LIMIT = 4096        # Telegram message
TIER_LABEL = {"new_launches": "New Launches", "emerging": "Emerging", "established": "Established"}
TIER_EMOJI = {"new_launches": "🆕", "emerging": "🌱", "established": "🏛"}


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


def readable(html_text):
    """Prose a reader sees, minus addresses (<code>) and link targets — for the banned-word check."""
    s = re.sub(r"<code>.*?</code>", "", html_text, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def visible_len(html_text):
    """Length as Telegram counts it: after entity parsing (tags/hrefs removed), in UTF-16 code units."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text)).encode("utf-16-le")) // 2


def check_banned(html_text, pub):
    hits = word_hits(readable(html_text), pub["banned_words"])
    if hits:  # template or token-derived text; never publish it
        raise ValueError(f"banned word in digest: {hits}")


def _footer(pub):
    lines = [f'🔗 <a href="{html.escape(pub["site_url"])}">All tiers on the website</a>'] if pub.get("site_url") else []
    return lines + [f"<i>{html.escape(pub['disclaimer'])}</i>"]


# --- daily card ---------------------------------------------------------------

def card_stats(tier, m):
    """Two stat lines for a card row."""
    if tier == "new_launches":
        return [f"Liquidity {compact_usd(m['liquidity_usd'])}  ·  24h volume {compact_usd(m['volume_h24_usd'])}",
                f"{m['holders']:,} holders  ·  {m['age_h']:.0f}h old"]
    return [f"MCap {compact_usd(m['mcap_usd'])}  ·  Liquidity {compact_usd(m['liquidity_usd'])}",
            f"{m['holders']:,} holders  ·  {m['age_d']:.0f} days old"]


def daily_caption(sections, now, pub):
    """sections: [(tier, [metrics])]. Drops copyable addresses tier by tier (Established first) if needed
    to fit Telegram's caption limit."""
    def build(with_ca):
        lines = [f"📊 <b>Coin Sieve · Daily screen</b>", f"<i>{_date(now)} · Solana</i>"]
        for tier, items in sections:
            if not items:
                continue
            lines += ["", f"{TIER_EMOJI[tier]} <b>{TIER_LABEL[tier]}</b>"]
            for m in items:
                sym, _ = display_name(m, pub)
                link = f'<a href="{html.escape(m["url"])}">{html.escape(sym)}</a>'
                lines.append(f"{link} · <code>{m['address']}</code>" if tier in with_ca else link)
        return "\n".join(lines + [""] + _footer(pub))

    with_ca = [t for t, _ in sections]
    text = build(with_ca)
    for drop in ("established", "emerging", "new_launches"):
        if visible_len(text) <= CAPTION_LIMIT:
            break
        with_ca = [t for t in with_ca if t != drop]
        text = build(with_ca)
    check_banned(text, pub)
    return text


def zero_text(now, pub):
    text = "\n".join([f"📊 <b>Coin Sieve · Daily screen</b>", f"<i>{_date(now)} · Solana</i>", "",
                      "No token passed every filter today.", ""] + _footer(pub))
    check_banned(text, pub)
    return text


# --- weekly / alerts ------------------------------------------------------------

def record_line(label, s):
    """One neutral track-record line from track_record.summarize() output."""
    if not s or not s.get("n"):
        return f"• {label}: not enough data yet"
    pct = lambda v: "n/a" if v is None else f"{v:+.0f}%"  # noqa: E731
    return (f"• {label} (n={s['n']}): still in a tier {s['in_a_tier']} · no longer listed {s['not_found']} · "
            f"median price {pct(s['median_price_change_pct'])} · median liquidity {pct(s['median_liquidity_change_pct'])}")


def _names(events, pub, with_detail=False, cap=12):
    out = []
    for e in events[:cap]:
        sym = html.escape(clean_text(e["symbol"], pub["max_symbol_len"]) or "?")
        if with_detail and e["detail"]:
            codes = sorted({d.split(":")[0].strip().replace("_", " ") for d in e["detail"].split(";")})
            sym += f" <i>({html.escape(', '.join(codes))})</i>"
        out.append(sym)
    if len(events) > cap:
        out.append(f"+{len(events) - cap} more")
    return ", ".join(out)


def weekly_caption(events, now, start, pub, tracked):
    """Short caption for the weekly card (details are in the image).
    events: tier_events rows of the week (bootstrap + blocked names already removed)."""
    by = lambda tier, kind: [e for e in events if e["tier"] == tier and e["kind"] == kind]  # noqa: E731
    listed = len(by("new_launches", "entered"))
    grads = by("emerging", "graduated")
    lines = ["🗓 <b>Coin Sieve · Weekly report</b>", f"<i>{_date(start)} - {_date(now)} · Solana</i>", "",
             f"{TIER_EMOJI['new_launches']} New tokens tracked: <b>{tracked:,}</b> · passed every filter: <b>{listed}</b>",
             f"🎓 Graduated to Emerging: {_names(grads, pub) if grads else 'none'}"]
    for tier in ("emerging", "established"):
        lines.append(f"{TIER_EMOJI[tier]} {TIER_LABEL[tier]}: {len(by(tier, 'entered'))} entered · "
                     f"{len(by(tier, 'left'))} left")
    text = "\n".join(lines + [""] + _footer(pub))
    check_banned(text, pub)
    return text


def alerts_text(events, pub):
    """Established-tier entries/exits since the last alert."""
    entered = [e for e in events if e["kind"] == "entered"]
    left = [e for e in events if e["kind"] == "left"]
    lines = [f"{TIER_EMOJI['established']} <b>Coin Sieve · Established tier update</b>", ""]
    if entered:
        lines.append(f"➕ Entered: {_names(entered, pub)}")
    if left:
        lines.append(f"➖ Left: {_names(left, pub, with_detail=True)}")
    text = "\n".join(lines + [""] + _footer(pub))
    check_banned(text, pub)
    return text[:TG_LIMIT]
