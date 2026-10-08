"""Sanitiser + digest wording rules."""
import unittest

from coinsieve.digest import TG_LIMIT, blocked_reason, daily_text, weekly_text
from coinsieve.sanitize import clean_text, word_hits

PUB = {"max_symbol_len": 12, "max_name_len": 32, "site_url": "", "banned_words": ["safe", "gem", "verified", "buy"],
       "blocked_words": ["pussy", "nazi"], "disclaimer": "Not financial advice. Passing the filter is not a recommendation."}
NOW = 1791400000


def token(**over):
    m = {"address": "Gem1BuyXsafeAddr111111111111111111111pump", "symbol": "TOK", "name": "Token",
         "age_h": 20, "liquidity_usd": 50000, "rc_lp_locked_pct": 100.0, "volume_h24_usd": 1.2e6,
         "txns_h24": 9000, "holders": 1500, "holder_growth_per_h": 40, "history_h": 2.0,
         "rc_top10_holders_pct": 18.0, "rc_risks": [], "url": "https://dexscreener.com/solana/gembuysafe"}
    m.update(over)
    return m


class SanitizeTest(unittest.TestCase):
    def test_strips_urls_handles_and_invisible_chars(self):
        self.assertEqual(clean_text("Moon​ https://scam.xyz/x @rugger t.me/abc coin", 40), "Moon coin")

    def test_length_cap(self):
        self.assertEqual(clean_text("A" * 50, 12), "A" * 11 + "…")

    def test_word_hits_case_insensitive(self):
        self.assertEqual(word_hits("SafeMoon GEMS", ["safe", "gem", "buy"]), ["safe", "gem"])


class DigestTest(unittest.TestCase):
    def test_blocked_and_banned_names(self):
        self.assertTrue(blocked_reason(token(name="Uncle Pussy"), PUB).startswith("blocked_name"))
        self.assertTrue(blocked_reason(token(symbol="GEMS"), PUB).startswith("blocked_name"))
        self.assertIsNone(blocked_reason(token(), PUB))

    def test_daily_contains_facts_and_disclaimer_last(self):
        text, used = daily_text([token()], NOW, PUB)
        self.assertEqual(len(used), 1)
        self.assertTrue(text.endswith(PUB["disclaimer"]))
        self.assertIn("Holders 1,500 (+40/h over 2.0h)", text)
        self.assertIn(token()["address"], text)   # address may contain 'gem'/'buy'/'safe' by chance

    def test_daily_skips_token_with_banned_warning(self):
        text, used = daily_text([token(rc_risks=["warn:Large buy wall"]), token(symbol="OK2")], NOW, PUB)
        self.assertEqual([m["symbol"] for m in used], ["OK2"])

    def test_daily_respects_telegram_limit(self):
        text, used = daily_text([token(symbol=f"T{i}") for i in range(40)], NOW, PUB)
        self.assertLessEqual(len(text), TG_LIMIT)
        self.assertLess(len(used), 40)

    def test_daily_zero(self):
        text, used = daily_text([], NOW, PUB)
        self.assertIn("0 tokens passed", text)
        self.assertEqual(used, [])

    def test_weekly(self):
        ev = lambda tier, kind, sym, detail=None: {"tier": tier, "kind": kind, "symbol": sym, "detail": detail}  # noqa: E731
        text = weekly_text([ev("emerging", "graduated", "AAA"), ev("emerging", "entered", "AAA"),
                            ev("established", "left", "BBB", "low_volume: 1 < 2; few_holders: 3 < 4"),
                            ev("new_launches", "entered", "CCC")], NOW, NOW - 7 * 86400, PUB)
        self.assertIn("Graduated (New Launches -> Emerging): AAA", text)
        self.assertIn("left: BBB (few_holders, low_volume)", text)
        self.assertIn("New Launches listed this week: 1", text)
        self.assertTrue(text.endswith(PUB["disclaimer"]))


if __name__ == "__main__":
    unittest.main()
