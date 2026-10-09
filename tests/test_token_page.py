"""Token page data: CoinGecko history parsing/planning/staleness, and the 'points to check' rules."""
import re
import unittest

import yaml

from coinsieve import facts, history, site_text

with open("config.yaml", encoding="utf-8") as _f:
    CFG = yaml.safe_load(_f)


class HistoryTest(unittest.TestCase):
    def test_parse_joins_series_on_timestamp(self):
        j = {"prices": [[2000, 1.5], [1000, 1.0], [3000, None]],
             "market_caps": [[1000, 10.0], [2000, 15.0]], "total_volumes": [[2000, 7.0], ["x", 1]]}
        self.assertEqual(history.parse(j), [[1, 1.0, 10.0, None], [2, 1.5, 15.0, 7.0]])   # ms -> s, sorted, no-price dropped
        self.assertEqual(history.parse(None), [])

    def test_candidates_need_a_clean_coingecko_id(self):
        profs = {"A": {"data": {"coingecko": {"id": "jupiter-exchange-solana"}}}, "B": {"data": {"coingecko": {"id": "../x"}}},
                 "C": {"data": {"coingecko": {"listed": False}}}}
        self.assertEqual(history.candidates(profs), {"A": "jupiter-exchange-solana"})

    def test_plan_orders_never_fetched_first_and_respects_refresh(self):
        now = 100 * 3600
        ids = {"A": "a", "B": "b"}
        attempts = {("A", "30d"): now - 1 * 3600, ("A", "365d"): now - 30 * 3600, ("B", "30d"): now - 5 * 3600}
        todo = history.plan(ids, attempts, CFG, now)
        self.assertEqual(todo[0], ("B", "b", "365d"))                # never fetched
        self.assertIn(("A", "a", "365d"), todo)                       # older than 24 h
        self.assertNotIn(("A", "a", "30d"), todo)                     # fetched 1 h ago, refresh every 3 h
        self.assertEqual(len(history.plan(ids, attempts, CFG, now, limit=1)), 1)

    def test_view_marks_stale_and_skips_empty(self):
        now = 1000 * 3600
        rows = {"30d": {"points": [[1, 1, 1, 1]], "fetched_at": now - 9 * 3600}, "365d": {"points": None, "fetched_at": None}}
        v = history.view(rows, CFG, now)
        self.assertEqual(list(v), ["30d"])
        self.assertTrue(v["30d"]["stale"])

    def test_fetch_stops_on_rate_limit_and_keeps_old_series(self):
        from unittest import mock
        store = mock.Mock()
        store.profiles.return_value = {"A": {"data": {"coingecko": {"id": "a"}}}, "B": {"data": {"coingecko": {"id": "b"}}}}
        store.history_attempts.return_value = {}
        cg = mock.Mock()
        cg.market_chart.side_effect = RuntimeError("GET failed after 3 attempts")
        st = history.fetch(store, CFG, cg, ["A", "B"], 1.0)
        self.assertTrue(st["rate_limited"])
        self.assertEqual(cg.market_chart.call_count, 1)
        store.save_history.assert_not_called()


def ids(points):
    return [p["id"] for p in points]


class PointsTest(unittest.TestCase):
    M = {"circ_supply": 900, "total_supply": 1000, "liquidity_usd": 5e6, "mcap_usd": 100e6}

    def test_nothing_when_data_is_unremarkable(self):
        h = {"top10_excl_pct": 20, "complete": True, "excluded_pct": 5, "authorities": {"mint": None, "freeze": None}}
        self.assertEqual(facts.points(self.M, h, {"max": 1000}, ["website", "docs"], True, [], CFG), [])

    def test_each_rule(self):
        h = {"top10_excl_pct": 60, "complete": False, "excluded_pct": 30, "authorities": {"mint": "Abc", "freeze": "Def"}}
        m = {**self.M, "circ_supply": 300, "liquidity_usd": 1e5}
        hist = [[0, 1000], [3 * 86400, 990], [8 * 86400, 900]]
        p = facts.points(m, h, {"max": 1000}, ["website"], True, hist, CFG)
        self.assertEqual(ids(p), ["top10", "contracts", "circulating", "mint", "freeze", "no_docs", "liquidity", "holders_down"])
        self.assertEqual(p[0]["vars"]["pct"], "≥60%")                        # lower bound kept
        self.assertEqual(p[2]["vars"], {"pct": "30%", "of": "max"})
        self.assertEqual(p[-1]["vars"]["pct"], "10.0%")   # 900/1000 - 1 = -9.99..%

    def test_unknown_data_makes_no_claim(self):
        m = {"circ_supply": None, "mcap_usd": None}
        p = facts.points(m, {"top10_excl_pct": None, "authorities": {}}, None, [], False, [[0, 5], [86400, 1]], CFG)
        self.assertEqual(p, [])   # no authorities known, profile unchecked, history shorter than the window

    def test_every_point_has_neutral_text(self):
        texts = site_text.load()["points"]
        rules = ["top10", "contracts", "circulating", "mint", "freeze", "no_docs", "liquidity", "holders_down"]
        self.assertEqual(sorted(texts), sorted(rules))
        for t in texts.values():
            low = t.lower()
            for w in ("safe", "gem", "verified", "risky", "avoid", "warning", "danger", "scam", "red flag"):
                self.assertNotIn(w, low)
            self.assertIsNone(re.search(r"\bbuy", low))


if __name__ == "__main__":
    unittest.main()
