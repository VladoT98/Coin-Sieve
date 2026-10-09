"""Price sparklines (charts.py) and bought/sold volume (jupiter.bought_sold_usd). No network."""
import json
import re
import tempfile
import unittest
from pathlib import Path

import requests

from coinsieve import charts
from coinsieve.jupiter import bought_sold_usd
from coinsieve.store import Store

H = 3600
NOW = 1_800_000_000
CFG = {"chain": "solana",
       "charts": {"history_hours": {"new_launches": 48, "emerging": 168, "established": 168},
                  "refresh_minutes": {"new_launches": 30, "emerging": 120, "established": 240},
                  "tier_order": ["new_launches", "emerging", "established"], "max_calls_per_run": 10,
                  "include_failing": False, "demand_window_minutes": 30, "pool_refresh_hours": 12,
                  "stale_after_hours": 6, "min_points": 2}}
SVG_OK = re.compile(r"^<svg [^<>]*><polyline [^<>]*/></svg>$")  # same check as dashboard.html


def series(closes, end=NOW, step=H):
    return [[end - (len(closes) - 1 - i) * step, c] for i, c in enumerate(closes)]


def row(points, fetched_at=NOW):
    return {"points": json.dumps(points), "fetched_at": fetched_at}


class ViewTest(unittest.TestCase):
    def test_missing_data_is_none_never_a_line(self):
        self.assertIsNone(charts.view(None, 168, CFG, NOW))
        self.assertIsNone(charts.view({"points": None, "fetched_at": None}, 168, CFG, NOW))  # never fetched
        self.assertIsNone(charts.view(row([]), 168, CFG, NOW))                              # pool without candles
        self.assertIsNone(charts.view(row(series([1.0])), 168, CFG, NOW))                   # one point is no line
        self.assertIsNone(charts.view(row(series([0, 0])), 168, CFG, NOW))                  # zero prices dropped

    def test_change_matches_the_drawn_series(self):
        pts = series([2.0, 1.0, 3.0, 2.5])
        v = charts.view(row(pts), 168, CFG, NOW)
        self.assertAlmostEqual(v["change_pct"], 25.0)
        self.assertEqual((v["first"], v["last"], v["points"]), (2.0, 2.5, 4))
        coords = re.search(r'points="([^"]+)"', v["svg"]).group(1).split()
        self.assertEqual(len(coords), 4)
        y_first, y_last = float(coords[0].split(",")[1]), float(coords[-1].split(",")[1])
        self.assertLess(y_last, y_first)  # line ends higher on screen = the same +25%
        self.assertIn("spark up", v["svg"])
        self.assertIn("spark down", charts.view(row(series([3.0, 1.0])), 168, CFG, NOW)["svg"])

    def test_window_cut_per_tier_and_short_history(self):
        pts = series([1.0] * 100 + [2.0])            # 100 h of history
        nl = charts.view(row(pts), 48, CFG, NOW)
        self.assertEqual((nl["points"], nl["span_h"], nl["change_pct"]), (49, 48.0, 100.0))
        em = charts.view(row(series([1.0, 1.5])), 168, CFG, NOW)
        self.assertEqual(em["span_h"], 1.0)           # shows what exists (dashboard labels it "1h")

    def test_stale_series_is_kept_and_flagged(self):
        old = row(series([1.0, 2.0], end=NOW - 30 * H), fetched_at=NOW - 30 * H)
        v = charts.view(old, 168, CFG, NOW)
        self.assertTrue(v["stale"])
        self.assertEqual(v["points"], 2)             # window ends at the last close, not cut to nothing
        self.assertIn("stale", v["svg"])
        self.assertFalse(charts.view(row(series([1.0, 2.0]), fetched_at=NOW - H), 168, CFG, NOW)["stale"])


class SvgTest(unittest.TestCase):
    def test_svg_shape(self):
        svg = charts.sparkline_svg(series([1.0 + (i % 7) for i in range(168)]), 5.0)
        self.assertRegex(svg, SVG_OK)
        self.assertLessEqual(len(re.search(r'points="([^"]+)"', svg).group(1).split()), charts.SPARK_MAX_POINTS)
        self.assertIsNone(re.search(r"[<>]", re.sub(r"<[^>]*>", "", svg)))  # no text content at all

    def test_flat_and_tiny_prices(self):
        flat = charts.sparkline_svg(series([5e-7, 5e-7, 5e-7]), 0.0)
        self.assertIn("spark flat", flat)
        self.assertRegex(flat, SVG_OK)
        self.assertRegex(charts.sparkline_svg(series([1e-9, 3e-9]), 200.0), SVG_OK)


class FakeGT:
    """Answers from a script: list of points, or an exception to raise."""
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def closes(self, pool, hours):
        self.calls.append((pool, hours))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


class FakeDex:
    def __init__(self):
        self.calls = 0

    def token_pairs(self, chain, address):
        self.calls += 1
        return [{"pairAddress": "curve" + address, "dexId": "pumpfun"},          # no liquidity key
                {"pairAddress": "small" + address, "liquidity": {"usd": 1_000}},
                {"pairAddress": "pool" + address, "liquidity": {"usd": 90_000}}]


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.dir.name) / "t.db"))
        # emerging run: A, B, C pass; D fails; nothing viewed yet
        results = [({"address": a}, []) for a in "ABC"] + [({"address": "D"}, ["low_liquidity: x"])]
        self.store.save_latest("emerging", results, NOW - 60)
        self.store.save_run_stats("emerging", {}, NOW - 60)
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_plan_priority_and_failing_tokens(self):
        self.assertEqual([a for a, _, _ in charts.plan(self.store, CFG, NOW)], ["A", "B", "C"])  # D fails
        self.store.add_chart_demand(["D", "C"], NOW - 60)                                      # viewed rows
        order = [a for a, _, _ in charts.plan(self.store, CFG, NOW)]
        self.assertEqual(set(order[:2]), {"C", "D"})                                           # viewed first
        self.assertEqual(order[2:], ["A", "B"])
        self.assertEqual(charts.plan(self.store, CFG, NOW)[0][2], 168)
        self.assertEqual(len(charts.plan(self.store, CFG, NOW, limit=1)), 1)

    def test_fetch_uses_highest_liquidity_pool_and_respects_refresh(self):
        gt, dex = FakeGT([series([1.0, 2.0])] * 3), FakeDex()
        st = charts.fetch(self.store, CFG, gt, dex, NOW)
        self.assertEqual((st["fetched"], st["failed"], st["rate_limited"]), (3, 0, False))
        self.assertEqual(gt.calls[0], ("poolA", 168))
        self.assertEqual(charts.plan(self.store, CFG, NOW + 60), [])           # not due again yet
        self.assertEqual(len(charts.plan(self.store, CFG, NOW + 121 * 60)), 3)  # emerging refresh: 120 min

    def test_429_stops_the_run_and_keeps_cached_series(self):
        old = series([1.0, 1.1], end=NOW - 10 * H)
        self.store.set_chart_pool("B", "poolB", NOW)
        self.store.save_chart_points("B", 168, old, NOW - 10 * H)
        self.store.commit()
        # order: A, C (never fetched), then B (oldest cached); A succeeds, C hits the exhausted 429 backoff
        gt = FakeGT([series([1.0, 2.0]), RuntimeError("GET x failed after 3 attempts"), series([5.0, 6.0])])
        st = charts.fetch(self.store, CFG, gt, FakeDex(), NOW)
        self.assertTrue(st["rate_limited"])
        self.assertEqual((st["fetched"], st["failed"], len(gt.calls)), (1, 1, 2))  # B not attempted
        rows = self.store.chart_rows()
        self.assertIn("rate limited", rows["C"]["error"])
        self.assertIsNone(charts.view(rows["C"], 168, CFG, NOW))                  # nothing cached -> "–"
        self.assertEqual(json.loads(rows["B"]["points"]), old)                    # last good series kept
        v = charts.view(rows["B"], 168, CFG, NOW)
        self.assertTrue(v["stale"])                                               # older than stale_after_hours
        self.assertAlmostEqual(v["change_pct"], 10.0)
        # a later 429 on a token WITH a cached series keeps that series too
        self.store.chart_failed("B", "rate limited", NOW)
        self.assertEqual(json.loads(self.store.chart_rows(["B"])["B"]["points"]), old)

    def test_unknown_pool_is_skipped_not_fatal(self):
        resp = requests.Response()
        resp.status_code = 404
        gt = FakeGT([requests.HTTPError(response=resp), series([1.0, 2.0]), []])
        st = charts.fetch(self.store, CFG, gt, FakeDex(), NOW)
        self.assertEqual((st["failed"], st["fetched"], st["empty"], st["rate_limited"]), (1, 1, 1, False))
        self.assertIsNone(charts.view(self.store.chart_rows(["C"]).get("C"), 168, CFG, NOW))  # [] -> "–"

    def test_pick_pool(self):
        self.assertEqual(charts.pick_pool(FakeDex().token_pairs("solana", "X")), "poolX")
        self.assertEqual(charts.pick_pool([{"pairAddress": "curve", "dexId": "pumpfun"}]), "curve")
        self.assertIsNone(charts.pick_pool([]))


class BoughtSoldTest(unittest.TestCase):
    def test_volume_split(self):
        self.assertEqual(bought_sold_usd({"stats24h": {"buyVolume": 60.004, "sellVolume": 40}}), (60.0, 40))
        # verified 2026-10-09: Jupiter omits the zero side
        self.assertEqual(bought_sold_usd({"stats24h": {"sellVolume": 10.8, "numSells": 12}}), (0, 10.8))
        self.assertEqual(bought_sold_usd({"stats24h": {"holderChange": 1}}), (None, None))
        self.assertEqual(bought_sold_usd(None), (None, None))


if __name__ == "__main__":
    unittest.main()
