"""Sanitiser, digest wording (HTML) and card rendering rules."""
import os
import tempfile
import unittest

from coinsieve.card import compact_usd, printable, render_daily, render_weekly, series_change
from coinsieve.digest import (CAPTION_LIMIT, blocked_reason, card_stats, check_banned, daily_caption, readable,
                              visible_len, weekly_caption, zero_text)
from coinsieve.funnel import stats as funnel_stats
from coinsieve.logos import fetch_logo, letter_badge
from coinsieve.sanitize import clean_text, word_hits
from coinsieve.tiers import TierRun

PUB = {"max_symbol_len": 12, "max_name_len": 32, "site_url": "", "banned_words": ["safe", "gem", "verified", "buy"],
       "blocked_words": ["pussy", "nazi"], "disclaimer": "Not financial advice. Passing the filter is not a recommendation."}
NOW = 1791400000


def nl(i=0, **over):
    m = {"address": f"Gem1BuyXsafeAddr11111111111111111111{i:04d}pump", "symbol": f"TOK{i}", "name": "Token",
         "age_h": 20, "liquidity_usd": 50000, "volume_h24_usd": 1.2e6, "holders": 1500,
         "url": f"https://dexscreener.com/solana/gembuysafe{i}"}
    m.update(over)
    return m


def big(i=0):
    return {"address": f"Est{i:041d}", "symbol": f"BIG{i}", "name": "Big", "age_d": 400, "mcap_usd": 3e8,
            "liquidity_usd": 5e6, "holders": 90000, "url": f"https://dexscreener.com/solana/Est{i}"}


class SanitizeTest(unittest.TestCase):
    def test_strips_urls_handles_and_invisible_chars(self):
        self.assertEqual(clean_text("Moon​ https://scam.xyz/x @rugger t.me/abc coin", 40), "Moon coin")

    def test_length_cap(self):
        self.assertEqual(clean_text("A" * 50, 12), "A" * 11 + "…")

    def test_word_hits_case_insensitive(self):
        self.assertEqual(word_hits("SafeMoon GEMS", ["safe", "gem", "buy"]), ["safe", "gem"])


class DigestTest(unittest.TestCase):
    def test_blocked_and_banned_names(self):
        self.assertTrue(blocked_reason(nl(name="Uncle Pussy"), PUB).startswith("blocked_name"))
        self.assertTrue(blocked_reason(nl(symbol="GEMS"), PUB).startswith("blocked_name"))
        self.assertIsNone(blocked_reason(nl(), PUB))

    def test_caption_html_and_disclaimer_last(self):
        text = daily_caption([("new_launches", [nl(symbol="A<b>")]), ("established", [big()])], NOW, PUB)
        self.assertIn("A&lt;b&gt;", text)                      # names are HTML-escaped
        self.assertIn(f"<code>{nl()['address']}</code>", text)  # tap-to-copy address
        self.assertTrue(text.endswith(f"<i>{PUB['disclaimer']}</i>"))

    def test_addresses_and_links_do_not_trip_banned_words(self):
        text = daily_caption([("new_launches", [nl()])], NOW, PUB)   # address/url contain gem/buy/safe
        check_banned(text, PUB)                                      # must not raise
        self.assertNotIn("Gem1Buy", readable(text))

    def test_caption_fits_telegram_limit(self):
        sections = [("new_launches", [nl(i) for i in range(5)]), ("emerging", [big(i) for i in range(5)]),
                    ("established", [big(i + 10) for i in range(5)])]
        self.assertLessEqual(visible_len(daily_caption(sections, NOW, PUB)), CAPTION_LIMIT)
        self.assertEqual(visible_len("<b>📊</b> <a href=\"https://x.y/very/long\">ab</a>"), 5)  # emoji = 2 units

    def test_zero_text(self):
        self.assertIn("No token passed", zero_text(NOW, PUB))

    def test_weekly_caption(self):
        ev = lambda tier, kind, sym, detail=None: {"tier": tier, "kind": kind, "symbol": sym, "detail": detail}  # noqa: E731
        text = weekly_caption([ev("emerging", "graduated", "AAA"), ev("emerging", "entered", "AAA"),
                               ev("established", "left", "BBB", "low_volume: 1 < 2"),
                               ev("new_launches", "entered", "CCC")], NOW, NOW - 7 * 86400, PUB, tracked=1234)
        self.assertIn("New tokens tracked: <b>1,234</b> · passed every filter: <b>1</b>", text)
        self.assertIn("Graduated to Emerging: AAA", text)
        self.assertIn("Established: 0 entered · 1 left", text)
        self.assertLessEqual(visible_len(text), CAPTION_LIMIT)


class CardTest(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(compact_usd(1_234_567), "$1.2M")
        self.assertEqual(compact_usd(950), "$950")
        self.assertEqual(printable("PEPE 🐸 中文"), "PEPE")
        self.assertEqual(series_change([(0, 2.0), (1, 3.0)]), 50.0)

    def assert_png(self, path):
        with open(path, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")

    def test_render_daily_png(self):
        sections = [("new_launches", [{"symbol": "AAA", "name": "A", "stats": card_stats("new_launches", nl()),
                                       "series": [(i, 1 + i % 3) for i in range(48)],
                                       "avatar": letter_badge("AAA", "#5b8def")}]),
                    ("established", [{"symbol": "BIG", "name": "Big", "stats": card_stats("established", big()),
                                      "series": None, "avatar": None}])]
        funnels = [("new_launches", {"stages": [("checked", 120), ("market", 9), ("safety", 4), ("shown", 1)],
                                     "reasons": [("low liquidity", 57)]}), ("established", None)]
        with tempfile.TemporaryDirectory() as d:
            self.assert_png(render_daily(sections, funnels, NOW, os.path.join(d, "c.png"), PUB["disclaimer"]))

    def test_render_weekly_png(self):
        with tempfile.TemporaryDirectory() as d:
            self.assert_png(render_weekly([("1,234", "tracked")] * 4, [("emerging", "Emerging", ["Entered: A"]),
                                                                       (None, "Track record", [])],
                                          NOW - 7 * 86400, NOW, os.path.join(d, "w.png"), PUB["disclaimer"]))


class LogoTest(unittest.TestCase):
    def test_letter_badge(self):
        img = letter_badge("$wif", "#e8b04b")
        self.assertEqual(img.size, (128, 128))
        self.assertEqual(img.getpixel((0, 0))[3], 0)        # transparent corner = round

    def test_only_https_logos_are_fetched(self):
        self.assertIsNone(fetch_logo("http://example.com/x.png", tempfile.gettempdir()))
        self.assertIsNone(fetch_logo(None, tempfile.gettempdir()))


class FunnelTest(unittest.TestCase):
    def test_stats_first_reason_and_rank_codes_ignored(self):
        run = TierRun("new_launches")
        run.results = [({"address": "a", "tier_member": True, "passed_hard_filters": True}, ["insufficient_history: x"]),
                       ({"address": "b", "passed_hard_filters": False}, ["low_liquidity: 1", "low_volume: 2"]),
                       ({"address": "c", "passed_hard_filters": False}, ["low_liquidity: 3"])]
        s = funnel_stats(run)
        self.assertEqual((s["evaluated"], s["market_passed"], s["members"]), (3, 1, 1))
        self.assertEqual(s["reasons"], {"low_liquidity": 2})


if __name__ == "__main__":
    unittest.main()
