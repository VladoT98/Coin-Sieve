"""config.yaml editing (comments preserved) + what-if re-evaluation."""
import unittest

import yaml

from coinsieve import whatif
from coinsieve.config_edit import set_values

TEXT = """# top comment
filters:
  min_liquidity_usd: 20000   # liquidity.usd
  min_volume_h24_usd: 50000
tiers:
  emerging:
    min_holders: 5000          # keep me
    max_age_days: 180
  established:
    min_holders: 20000
    max_age_days: null
"""


class ConfigEditTest(unittest.TestCase):
    def test_updates_values_and_keeps_comments(self):
        new = set_values(TEXT, {"filters.min_liquidity_usd": 25000, "tiers.established.max_age_days": 900})
        self.assertIn("  min_liquidity_usd: 25000   # liquidity.usd", new)
        self.assertIn("# top comment", new)
        cfg = yaml.safe_load(new)
        self.assertEqual(cfg["tiers"]["established"]["max_age_days"], 900)
        self.assertEqual(cfg["tiers"]["emerging"]["max_age_days"], 180)   # same key, other section untouched

    def test_nested_same_key_names_are_disambiguated(self):
        new = set_values(TEXT, {"tiers.emerging.min_holders": 7000})
        cfg = yaml.safe_load(new)
        self.assertEqual((cfg["tiers"]["emerging"]["min_holders"], cfg["tiers"]["established"]["min_holders"]),
                         (7000, 20000))
        self.assertIn("min_holders: 7000          # keep me", new)

    def test_null_bool_float(self):
        new = set_values(TEXT, {"tiers.emerging.max_age_days": None, "filters.min_volume_h24_usd": 1.5})
        cfg = yaml.safe_load(new)
        self.assertIsNone(cfg["tiers"]["emerging"]["max_age_days"])
        self.assertEqual(cfg["filters"]["min_volume_h24_usd"], 1.5)

    def test_unknown_path_rejected(self):
        with self.assertRaises(KeyError):
            set_values(TEXT, {"filters.nope": 1})


class WordingTest(unittest.TestCase):
    """The website follows the same wording rules as the posts."""

    def test_no_banned_words_in_site_or_labels(self):
        import re
        from pathlib import Path
        from coinsieve import funnel
        texts = [Path(__file__).parents[1].joinpath("coinsieve", "dashboard.html").read_text(encoding="utf-8"),
                 " ".join(funnel.REASON_LABELS.values()),
                 " ".join(f"{lab} {h}" for fields in whatif.FIELDS.values() for _, lab, _, h in fields)]
        for text in texts:
            low = text.lower()
            for word in ("safe", "gem", "verified"):
                self.assertNotIn(word, low)
            self.assertIsNone(re.search(r"\bbuy", low))

    def test_new_column_texts_are_covered(self):
        """The bought/sold and price-chart texts exist (so the scan above covers them) and stay neutral."""
        import re
        from pathlib import Path
        html = Path(__file__).parents[1].joinpath("coinsieve", "dashboard.html").read_text(encoding="utf-8")
        for needle in ('label: "Bought / sold"', "flow: \"Share of the last 24 hours' trading volume",
                       'chart: "Hourly closing prices', '" Price"'):
            self.assertIn(needle, html)
        texts = re.findall(r'^\s+(?:flow|chart): "([^"]+)"', html, re.M)
        self.assertEqual(len(texts), 2)
        for text in texts:
            low = text.lower()
            for word in ("safe", "gem", "verified", "sentiment", "bullish", "bearish", "signal", "recommend"):
                self.assertNotIn(word, low)
            self.assertIsNone(re.search(r"\bbuy", low))


CFG = {"age": {"min_hours": 6, "max_hours": 48},
       "filters": {"min_liquidity_usd": 20000, "min_volume_h24_usd": 50000, "min_txns_h24": 300, "min_socials": 1,
                   "max_dev_mints": 3},
       "rugcheck": {"min_lp_locked_pct": 95, "max_top1_holder_pct": 10, "max_top10_holders_pct": 30}}


def nl(**over):
    m = {"address": "A", "age_h": 20, "liquidity_usd": 30000, "volume_h24_usd": 100000, "txns_h24": 500,
         "socials": ["twitter"], "on_bonding_curve": False, "dex_id": "pumpswap", "dev_mints": 1,
         "rc_lp_locked_pct": 100, "rc_top1_holder_pct": 3, "rc_top10_holders_pct": 20}
    m.update(over)
    return m


class WhatIfTest(unittest.TestCase):
    def test_pass_and_threshold_change(self):
        self.assertEqual(whatif.new_launch_status(nl(), [], CFG), ("pass", []))
        stricter = whatif.with_values(CFG, {"filters.min_liquidity_usd": 40000})
        status, reasons = whatif.new_launch_status(nl(), [], stricter)
        self.assertEqual(status, "fail")
        self.assertTrue(reasons[0].startswith("low_liquidity"))

    def test_unknown_when_never_checked_by_rugcheck(self):
        m = nl(liquidity_usd=15000, dev_mints=None, rc_lp_locked_pct=None)
        looser = whatif.with_values(CFG, {"filters.min_liquidity_usd": 10000})
        self.assertEqual(whatif.new_launch_status(m, ["low_liquidity: 15,000 < 20,000"], looser)[0], "unknown")

    def test_fixed_reasons_carried_over(self):
        m = nl(rc_lp_locked_pct=None)
        status, reasons = whatif.new_launch_status(m, ["risk_danger: Low Liquidity ($1)"], CFG)
        self.assertEqual(status, "fail")
        self.assertEqual(reasons, ["risk_danger: Low Liquidity ($1)"])

    def test_checklist_agrees_with_status(self):
        cases = [(nl(), []), (nl(liquidity_usd=5000), ["low_liquidity: x"]),
                 (nl(dev_mints=None, rc_lp_locked_pct=None), []),
                 (nl(rc_lp_locked_pct=None), ["risk_danger: Low Liquidity ($1)"]),
                 (nl(dev_mints=9), []), (nl(rc_top10_holders_pct=45), []),
                 (nl(on_bonding_curve=True, liquidity_usd=None, dex_id="pumpfun"), ["bonding_curve: x"])]
        for m, stored in cases:
            checks = whatif.new_launch_checks(m, stored, CFG)
            self.assertEqual(whatif.checks_status(checks), whatif.new_launch_status(m, stored, CFG)[0], (m, stored))

    def test_passing_token_lists_every_check_green(self):
        checks = whatif.new_launch_checks(nl(), [], CFG)
        self.assertTrue(all(c["ok"] for c in checks))
        self.assertIn({"id": "liquidity", "label": "Liquidity", "ok": True, "value": "$30.0K", "limit": "min $20.0K",
                       "paths": ["filters.min_liquidity_usd"], "info": False}, checks)

    def test_impact_counts_failing_filter(self):
        lists = [whatif.new_launch_checks(nl(liquidity_usd=v), [], CFG) for v in (5000, 15000, 30000)]
        self.assertEqual(whatif.impact("new_launches", lists)["filters.min_liquidity_usd"], 2)

    def test_typical_values(self):
        toks = [{"metrics": nl(liquidity_usd=v)} for v in (1000, 2000, 3000, 4000, 5000)]
        t = whatif.typical("new_launches", toks)["filters.min_liquidity_usd"]
        self.assertEqual((t["p25"], t["median"], t["p75"], t["unit"]), (2000, 3000, 4000, "usd"))

    def test_validate(self):
        self.assertEqual(whatif.validate("emerging", {"tiers.emerging.max_age_days": ""}),
                         {"tiers.emerging.max_age_days": None})
        self.assertEqual(whatif.validate("new_launches", {"filters.min_txns_h24": "250"}),
                         {"filters.min_txns_h24": 250})
        for bad in ({"filters.min_txns_h24": "-1"}, {"filters.min_txns_h24": "abc"}, {"filters.unknown": 1}):
            with self.assertRaises(ValueError):
                whatif.validate("new_launches", bad)


if __name__ == "__main__":
    unittest.main()


class LogoEndpointTest(unittest.TestCase):
    def test_logo_url_only_comes_from_stored_metrics(self):
        from unittest import mock
        from coinsieve.dashboard import App
        app = App.__new__(App)  # skip config/db loading
        app.icons, app.bad_icons, app.logo_dir = {}, {}, "unused"
        with mock.patch("coinsieve.logos.logo_png") as fetch:
            self.assertIsNone(app.logo("UnknownAddr1111111111111111111111111"))
            fetch.assert_not_called()  # unknown address -> nothing fetched
            app.icons["A"] = "https://example.com/x.png"
            fetch.return_value = None
            self.assertIsNone(app.logo("A"))
            self.assertIsNone(app.logo("A"))
            fetch.assert_called_once()  # failed logos are not retried
