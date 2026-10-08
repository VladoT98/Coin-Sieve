"""Telegram Bot API posting + neutral post wording.

Error shape verified live: {"ok": false, "error_code": 401, "description": "Unauthorized"}.
"""
import requests


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token, timeout_s):
        self._token = token
        self.timeout = timeout_s

    def _redact(self, text):
        return str(text).replace(self._token, "<token>")  # request errors embed the URL

    def send(self, chat_id, text):
        """Send a plain-text message; return its message_id."""
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        try:
            r = requests.post(url, json=payload, timeout=self.timeout)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            raise TelegramError(self._redact(e)) from None
        if not data.get("ok"):
            raise TelegramError(f"{data.get('error_code')} {self._redact(data.get('description'))}")
        return data["result"]["message_id"]


def fmt_usd(v):
    return f"${v / 1e6:,.2f}M" if v >= 1e6 else f"${v:,.0f}"


def compose_post(m, disclaimer):
    """Neutral, descriptive facts only. Plain text (no parse_mode) so token names need no escaping.
    Returns (text, prose) — prose excludes addresses/URLs, for the banned-word check."""
    growth = m["holder_growth_per_h"]
    warns = [r.split(":", 1)[1] for r in m.get("rc_risks", []) if r.startswith("warn:")]
    prose = [
        f"{m['symbol']} ({m['name']})",
        f"Age: {m['age_h']:.1f}h",
        f"Liquidity: {fmt_usd(m['liquidity_usd'])} ({m['rc_lp_locked_pct']:.1f}% of LP locked or burned)",
        f"24h volume: {fmt_usd(m['volume_h24_usd'])} | 24h trades: {m['txns_h24']:,}",
        f"Holders: {m['rc_total_holders']:,} ({growth:+,.0f}/h over last {m['history_h']:.1f}h)",
        f"Top 10 holders (excl. pools): {m['rc_top10_holders_pct']:.1f}% | largest: {m['rc_top1_holder_pct']:.1f}%",
        "Mint authority revoked. Freeze authority revoked.",
        f"RugCheck warnings: {', '.join(warns) if warns else 'none'}",
        disclaimer,
    ]
    lines = [prose[0], m["address"], "", *prose[1:-1], "",
             m["url"], f"https://rugcheck.xyz/tokens/{m['address']}", "", prose[-1]]
    return "\n".join(lines), "\n".join(prose)


def banned_hits(text, banned_words):
    """Case-insensitive substring match: strict on purpose (catches 'SafeMoon', 'GEMS')."""
    low = text.lower()
    return [w for w in banned_words if w.lower() in low]
