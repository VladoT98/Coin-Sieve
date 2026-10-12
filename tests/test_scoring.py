"""v4 Quality score (Phase 2): percentiles, re-weighting, filters, derived metrics, config checks. Offline."""
import copy
import unittest
from datetime import datetime, timezone

import yaml

from coinsieve.screener import scoring

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc).timestamp()
AS_OF = "2026-10-12"
with open("config.yaml", encoding="utf-8") as f:
    SCFG = yaml.safe_load(f)["screener"]["scoring"]


def small_cfg(**over):
    c = {"categories": {"a": 60, "b": 40}, "min_metrics_per_category": 2, "min_coins_per_metric": 2,
         "metrics": {"a1": {"category": "a", "weight": 1, "direction": "higher"},
                     "a2": {"category": "a", "weight": 1, "direction": "lower"},
                     "b1": {"category": "b", "weight": 1, "direction": "higher"},
                     "b2": {"category": "b", "weight": 3, "direction": "higher"}}}
    c.update(over)
    return c


class TestPercentiles(unittest.TestCase):
    def test_ties_share_average_rank_and_lower_flips(self):
        p = scoring.percentiles({"a": 1, "b": 2, "c": 2, "d": 3, "x": None})
        self.assertEqual(p, {"a": 0.0, "b": 50.0, "c": 50.0, "d": 100.0})
        self.assertEqual(scoring.percentiles({"a": 1, "b": 3}, "lower"), {"a": 100.0, "b": 0.0})
        self.assertEqual(scoring.percentiles({"a": 5}), {"a": 50.0})

    def test_outlier_does_not_stretch_the_scale(self):
        self.assertEqual(scoring.percentiles({"a": 1, "b": 2, "c": 1e12}),
                         scoring.percentiles({"a": 1, "b": 2, "c": 3}))


class TestScore(unittest.TestCase):
    def coins(self):
        return {
            "x": {"metrics": {"a1": 10, "a2": 1, "b1": 5, "b2": 5}},
            "y": {"metrics": {"a1": 20, "a2": 2, "b1": 1, "b2": 9}},
            "z": {"metrics": {"a1": 30, "a2": 3, "b1": None, "b2": 1}},   # one metric in b -> not enough data
        }

    def test_reweight_missing_and_neutral_category(self):
        r = scoring.score(self.coins(), small_cfg())
        self.assertEqual(r["x"]["categories"]["a"]["score"], 50.0)    # a1 pct 0, a2 (lower) pct 100
        self.assertEqual(r["z"]["categories"]["b"], {"score": 50.0, "used": 1, "of": 2, "flag": "not enough data"})
        # y: b1 pct 0, b2 pct 100 with weight 3 -> 75
        self.assertEqual(r["y"]["categories"]["b"]["score"], 75.0)
        self.assertAlmostEqual(r["y"]["total"], 50 * 0.6 + 75 * 0.4)

    def test_metric_with_too_few_coins_is_skipped_for_everyone(self):
        r = scoring.score(self.coins(), small_cfg(min_coins_per_metric=3))
        self.assertNotIn("b1", r["x"]["pct"])     # only 2 coins have b1
        self.assertEqual(r["x"]["categories"]["b"]["flag"], "not enough data")

    def test_rank_passing_only_ties_by_mcap(self):
        coins = {k: {"passes": k != "c", "facts": {"mcap": m}} for k, m in (("a", 1), ("b", 5), ("c", 9))}
        res = {"a": {"total": 60.0}, "b": {"total": 60.0}, "c": {"total": 99.0}}
        self.assertEqual(scoring.rank(res, coins), ["c", "b", "a"])
        self.assertEqual([res[k]["rank"] for k in "abc"], [2, 1, None])
        self.assertEqual(res["c"]["rank_all"], 1)


class TestDerive(unittest.TestCase):
    def raw(self, **kv):
        return {k: {"value": v, "source_url": f"https://src/{k}"} for k, v in kv.items()}

    def test_ratios_relative_change_and_sources(self):
        raw = self.raw(defillama_website=None, mcap_usd=1e9, major_volume_24h_usd=5e7, protocol_tvl_usd=2e9, chain_tvl_usd=5e9,
                       fees_30d_usd=1e6, fees_prev30d_usd=5e5, circ_supply=90, max_supply=100, total_supply=200,
                       fdv_usd=1.1e9, price_change_30d_pct=10, has_website=0, has_whitepaper=1)
        m, src, facts = scoring.derive(raw, [], self.raw(price_change_30d_pct=4), AS_OF)
        self.assertAlmostEqual(m["major_volume_mcap"], 0.05)
        self.assertEqual(m["tvl_mcap"], 2.0)                              # protocol TVL only
        self.assertEqual(src["tvl_mcap"], "https://src/protocol_tvl_usd")
        self.assertEqual((m["chain_tvl"], src["chain_tvl"]), (5e9, "https://src/chain_tvl_usd"))  # absolute
        self.assertAlmostEqual(m["fees_mcap"], 0.012)
        self.assertEqual(m["fees_trend"], 2.0)
        self.assertEqual(m["circulating_ratio"], 0.9)                     # max supply before total
        self.assertAlmostEqual(m["fdv_mcap"], 1.1)
        self.assertEqual(m["rel_change_30d"], 6)
        self.assertIsNone(m["depth_mcap"])
        self.assertTrue(facts["website_or_docs"])
        self.assertNotIn("depth_mcap", src)

    def test_supply_change_from_mcap_over_price(self):
        prices = [("2025-10-12", 1.0, 100.0), ("2026-09-12", 2.0, 220.0), ("2026-10-12", 2.0, 242.0)]
        self.assertAlmostEqual(scoring.supply_change(prices, AS_OF, 30), 10.0)    # 110 -> 121 tokens
        self.assertAlmostEqual(scoring.supply_change(prices, AS_OF, 365), 21.0)   # 100 -> 121
        self.assertIsNone(scoring.supply_change(prices[1:], AS_OF, 365))

    def test_rsi_and_volatility(self):
        self.assertEqual(scoring.rsi([float(i) for i in range(1, 20)]), 100.0)
        self.assertIsNone(scoring.rsi([1.0] * 10))
        self.assertAlmostEqual(scoring.volatility([1.0] * 100), 0.0)


class TestMissingPolicy(unittest.TestCase):
    def test_no_source_detection(self):
        self.assertEqual(scoring.no_source({"has_github": {"value": 0.0}}), {"fundamentals", "development"})
        self.assertEqual(scoring.no_source({"chain_tvl_usd": {"value": None}, "has_github": {"value": 1.0}}), set())
        self.assertEqual(scoring.no_source({"defillama_group": {"text": "x"}}), set())   # profile not fetched yet
        self.assertEqual(scoring.no_source({"has_github": {"value": 0.0}, "commits_90d": {"value": 3},
                                            "protocol_tvl_usd": {"value": 1}}), set())      # override gave a repo

    def test_exclude_spreads_weight_and_zero_counts(self):
        cfg = small_cfg(missing_policy={"b": "exclude"})
        coins = TestScore().coins()
        coins["x"]["no_source"] = {"b"}
        r = scoring.score(coins, cfg)
        self.assertEqual(r["x"]["categories"]["b"]["flag"], "not applicable")
        self.assertIsNone(r["x"]["categories"]["b"]["score"])
        self.assertAlmostEqual(r["x"]["total"], r["x"]["categories"]["a"]["score"])   # only category a counts
        cfg["missing_policy"] = {"b": "zero"}
        r = scoring.score(coins, cfg)
        self.assertEqual(r["x"]["categories"]["b"]["score"], 0.0)
        self.assertAlmostEqual(r["x"]["total"], r["x"]["categories"]["a"]["score"] * 0.6)

    def test_config_policy_validation(self):
        bad = copy.deepcopy(SCFG)
        bad["missing_policy"] = {"fundamentals": "skip", "nope": "zero"}
        errs = " | ".join(scoring.validate(bad))
        self.assertIn("missing_policy fundamentals: skip", errs)
        self.assertIn("missing_policy nope", errs)


class TestFilters(unittest.TestCase):
    def facts(self, **kv):
        f = {"mcap": 1e9, "major_volume": 1e6, "exchanges": 10, "website_or_docs": True,
             "starts": [NOW - 800 * 86400], "first_price": "2025-10-13"}
        f.update(kv)
        return f

    def full_core(self):
        return {mid: 1.0 for mid, d in SCFG["metrics"].items() if d["category"] in SCFG["filters"]["core_categories"]}

    def test_passes_and_each_failure(self):
        self.assertEqual(scoring.filter_reasons(self.facts(), self.full_core(), SCFG, NOW, AS_OF), [])
        young = scoring.filter_reasons(self.facts(starts=[NOW - 100 * 86400], first_price="2026-07-01"),
                                       self.full_core(), SCFG, NOW, AS_OF)
        self.assertEqual(young, ["age 103 d < 365 d"])
        self.assertIn("fewer than 3 exchanges", scoring.filter_reasons(self.facts(exchanges=2), self.full_core(),
                                                                      SCFG, NOW, AS_OF))
        self.assertIn("profile not fetched yet", scoring.filter_reasons(self.facts(website_or_docs=None),
                                                                       self.full_core(), SCFG, NOW, AS_OF))
        sparse = {k: (v if i % 2 else None) for i, (k, v) in enumerate(self.full_core().items())}
        self.assertTrue(any(r.startswith("core data") for r in
                            scoring.filter_reasons(self.facts(), sparse, SCFG, NOW, AS_OF)))


class TestConfig(unittest.TestCase):
    def test_shipped_config_is_valid(self):
        self.assertEqual(scoring.validate(SCFG), [])

    def test_validation_catches_mistakes(self):
        bad = copy.deepcopy(SCFG)
        bad["categories"]["momentum"] = 15
        bad["metrics"]["made_up"] = {"category": "nope", "weight": 1, "direction": "up"}
        errs = " | ".join(scoring.validate(bad))
        for part in ("add up to 105", "unknown category nope", "higher/lower", "no formula"):
            self.assertIn(part, errs)

    def test_sensitivity_counts_changes(self):
        coins = {c: {"metrics": m["metrics"], "passes": True, "facts": {"mcap": 1}}
                 for c, m in TestScore().coins().items()}
        out = scoring.sensitivity(coins, small_cfg(), top_n=1)
        self.assertEqual(set(out), {"a", "b"})
        self.assertTrue(all(len(v) == 2 for v in out.values()))


class TestReportWording(unittest.TestCase):
    def test_review_page_has_no_banned_words_and_shows_disclaimer(self):
        import re
        import tempfile
        from pathlib import Path

        from coinsieve.screener import score_run
        with open("config.yaml", encoding="utf-8") as f:
            pub = yaml.safe_load(f)["publishing"]
        coins = {"x": {"metrics": {m: 1.0 for m in SCFG["metrics"]}, "sources": {}, "facts": {"mcap": 1},
                       "passes": True, "fails": [], "symbol": "XX", "name": "Example"}}
        res = scoring.score(coins, dict(SCFG, min_coins_per_metric=1))
        scoring.rank(res, coins)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.html"
            score_run.write_html(path, AS_OF, ["x"], coins, res, SCFG, {"liquidity": [0, 1]}, "abc")
            text = re.sub(r"<[^>]+>", " ", path.read_text(encoding="utf-8")).lower()
        for w in pub["banned_words"] + pub["banned_phrases"]:
            self.assertNotRegex(text, r"" + re.escape(w.lower()) + r"", w)
        self.assertIn("educational information only, not investment advice", text)
        self.assertIn("coingecko", text)


if __name__ == "__main__":
    unittest.main()
