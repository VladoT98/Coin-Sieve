"""v4 screener pipeline (Phase 1): budget, cache, universe, source extractors. Offline - no network."""
import tempfile
import unittest
from datetime import datetime, timezone

from coinsieve.screener import cg_source, gh_source, llama_source, universe
from coinsieve.screener.api import Api, BudgetExceeded
from coinsieve.screener.db import ScreenerDB
from coinsieve.screener.pipeline import due

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc).timestamp()


class FakeResponse:
    def __init__(self, status, data, headers=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.content = b"x" if data is not None else b""

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        return self.responses.pop(0)


def api(responses, cache_dir=None, **kw):
    db = ScreenerDB(":memory:")
    a = Api("coingecko", db, "https://x.test/api", cache_dir=cache_dir, min_interval_s=0, backoff_s=0,
            clock=lambda: NOW, sleep=lambda s: None, session=FakeSession(responses), **kw)
    return a, db


class TestApi(unittest.TestCase):
    def test_budget_stops_non_essential_but_keeps_reserve(self):
        a, db = api([FakeResponse(200, [1])] * 5, monthly_budget=10, reserve_pct=20)
        for _ in range(8):
            db.log_call(NOW - 60, "coingecko", "x", 200, 1)
        with self.assertRaises(BudgetExceeded):
            a.get("coins/x")                       # 8 >= 10 * 0.8
        self.assertTrue(a.get("coins/markets", essential=True).ok)
        db.log_call(NOW - 60, "coingecko", "x", 200, 1)
        with self.assertRaises(BudgetExceeded):
            a.get("coins/markets", essential=True)  # 10 >= 10

    def test_last_month_does_not_count(self):
        a, db = api([FakeResponse(200, [1])], monthly_budget=1)
        db.log_call(NOW - 40 * 86400, "coingecko", "x", 200, 1)
        self.assertTrue(a.get("ping").ok)

    def test_cache_hit_costs_nothing_and_expires(self):
        with tempfile.TemporaryDirectory() as d:
            a, db = api([FakeResponse(200, {"v": 1}), FakeResponse(200, {"v": 2})], cache_dir=d)
            self.assertEqual(a.get("p", ttl_hours=1).data, {"v": 1})
            hit = a.get("p", ttl_hours=1)
            self.assertTrue(hit.cached)
            self.assertEqual(db.calls_since("coingecko", 0), 1)
            a.clock = lambda: NOW + 2 * 3600
            self.assertEqual(a.get("p", ttl_hours=1).data, {"v": 2})

    def test_202_not_cached_and_429_retried(self):
        with tempfile.TemporaryDirectory() as d:
            a, db = api([FakeResponse(202, {}), FakeResponse(429, None), FakeResponse(200, [1])], cache_dir=d)
            self.assertEqual(a.get("stats", ttl_hours=5).status, 202)
            self.assertTrue(a.get("stats", ttl_hours=5).ok)
            self.assertEqual(db.calls_since("coingecko", 0), 3)

    def test_github_quota_floor(self):
        a, _ = api([FakeResponse(200, [], {"X-RateLimit-Remaining": "10", "X-RateLimit-Reset": "0"})],
                   min_remaining=50)
        a.get("a")
        with self.assertRaises(BudgetExceeded):
            a.get("b")


UCFG = {"enter_mcap_usd": 100e6, "stay_mcap_usd": 90e6, "exclude_categories": ["stablecoins", "wrapped-tokens"],
        "exclude_ids": ["handpicked"]}


class TestUniverse(unittest.TestCase):
    def rows(self):
        return [{"id": "big", "market_cap": 5e9}, {"id": "tether", "market_cap": 1e11},
                {"id": "edge", "market_cap": 95e6}, {"id": "member", "market_cap": 95e6},
                {"id": "handpicked", "market_cap": 1e9}, {"id": "tiny", "market_cap": 1e6}]

    def test_hysteresis_categories_and_ids(self):
        out = {r["cg_id"]: r["reason"] for r in universe.decide(
            self.rows(), UCFG, {"member"}, {"stablecoins": {"tether"}, "wrapped-tokens": set()})}
        self.assertEqual(out, {"big": "included", "tether": "category:stablecoins", "edge": "below_enter_mcap",
                               "member": "included", "handpicked": "excluded_id"})

    def test_missing_category_list_includes_nobody(self):
        out = universe.decide(self.rows(), UCFG, set(), {"stablecoins": {"tether"}})
        self.assertFalse(any(r["included"] for r in out))
        self.assertIn("categories_unknown", {r["reason"] for r in out})


class TestDefiLlama(unittest.TestCase):
    LITE = {"protocols": [
        {"defillamaId": "1", "name": "Aave V2", "geckoId": "aave", "parentProtocol": "parent#aave", "tvl": 100,
         "tvlPrevWeek": 100, "tvlPrevMonth": 50, "category": "Lending"},
        {"defillamaId": "2", "name": "Aave V3", "parentProtocol": "parent#aave", "tvl": 900, "tvlPrevWeek": 900,
         "tvlPrevMonth": 950},
        {"defillamaId": "3", "name": "Uniswap V3", "parentProtocol": "parent#uniswap", "tvl": 400},
        {"defillamaId": "4", "name": "Other token child", "geckoId": "other", "parentProtocol": "parent#aave",
         "tvl": 7},
        {"defillamaId": "5", "name": "Solo", "geckoId": "solo", "tvl": 10},
    ], "parentProtocols": [{"id": "parent#uniswap", "gecko_id": "uniswap"}, {"id": "parent#aave", "gecko_id": None}]}
    CHAINS = [{"name": "BSC", "gecko_id": "binancecoin", "tvl": 5e9}, {"name": "Binance", "gecko_id": "binancecoin",
              "tvl": 0}, {"name": "Hyperliquid L1", "gecko_id": "uniswap", "tvl": 1}]
    FEES = [{"defillamaId": "2", "total30d": 30, "total7d": 7}, {"defillamaId": "chain#bsc", "protocolType": "chain",
            "name": "BSC", "total30d": 18}, {"defillamaId": "3", "total30d": 100}]

    def metrics(self, cid):
        out = llama_source.coin_metrics([cid], self.LITE, {"5": "solo-slug"}, self.CHAINS, self.FEES, [],
                                        [{"defillamaId": "1", "amount": 5, "date": 1}])
        return {k: (v, url) for m, url in out.get(cid, []) for k, v in m.items()}

    def test_child_gecko_id_rolls_up_parent_without_other_tokens(self):
        m = self.metrics("aave")
        self.assertEqual(m["protocol_tvl_usd"][0], 1000)              # V2 + V3, not the other token's child
        self.assertEqual(m["fees_30d_usd"][0], 30)
        self.assertAlmostEqual(m["protocol_tvl_change_30d_pct"][0], 0.0)   # 1000 vs 1000
        self.assertEqual(m["hacks_count"][0], 1.0)
        self.assertEqual(m["protocol_tvl_usd"][1], "https://defillama.com/protocol/aave")

    def test_parent_only_gecko_id(self):
        m = self.metrics("uniswap")
        self.assertEqual(m["protocol_tvl_usd"][0], 400)
        self.assertEqual(m["fees_30d_usd"][0], 100)                    # protocol fees win over chain fees
        self.assertEqual(m["chain_tvl_usd"][1], "https://defillama.com/chain/Hyperliquid L1")

    def test_duplicate_chain_rows_take_largest_and_chain_fees(self):
        m = self.metrics("binancecoin")
        self.assertEqual(m["chain_tvl_usd"], (5e9, "https://defillama.com/chain/BSC"))
        self.assertEqual(m["fees_30d_usd"][0], 18)

    def test_standalone_uses_slug(self):
        self.assertEqual(self.metrics("solo")["protocol_tvl_usd"][1], "https://defillama.com/protocol/solo-slug")
        self.assertEqual(self.metrics("unknown"), {})


class TestGitHub(unittest.TestCase):
    def test_owner_parsing(self):
        owners, listed = gh_source.owners_and_repos(["https://github.com/Aave/aave-protocol",
                                                     "https://github.com/aave", "https://gitlab.com/x/y",
                                                     "https://github.com/foo/bar.git"])
        self.assertEqual(owners, ["aave", "foo"])
        self.assertEqual(listed, {"aave": {"aave-protocol"}, "foo": {"bar"}})

    def test_pick_repos(self):
        repos = [{"name": "old-listed", "pushed_at": "2020"}, {"name": "fork", "fork": True, "pushed_at": "2027"},
                 {"name": "arch", "archived": True, "pushed_at": "2027"}, {"name": "new", "pushed_at": "2026"},
                 {"name": "mid", "pushed_at": "2025"}]
        self.assertEqual([r["name"] for r in gh_source.pick_repos(repos, {"old-listed"}, 2)], ["old-listed", "new"])

    def test_coin_metrics_windows(self):
        week = gh_source.WEEK
        recent, older = int(NOW - 2 * week), int(NOW - 30 * week)
        raw = {"repos": ["a/x"], "commit_weeks": {recent: 5, older: 7, int(NOW - 60 * week): 100},
               "contributors": {"amy": [recent], "bob": [older]}, "stars": 3.0, "forks": 1.0,
               "last_push_ts": NOW - 86400, "active_repos_90d": 1.0, "incomplete": 0}
        m = gh_source.coin_metrics([raw, None], NOW)
        self.assertEqual((m["commits_90d"], m["commits_1y"], m["active_weeks_1y"]), (5, 12, 2))
        self.assertEqual((m["contributors_90d"], m["contributors_1y"]), (1, 2))
        self.assertEqual(m["days_since_push"], 1.0)
        self.assertIsNone(gh_source.coin_metrics([None], NOW))

    def test_stats_ask_all_first_then_only_pending(self):
        from coinsieve.screener.api import Result

        class FakeApi:
            def __init__(self):
                self.calls = []
                self.answers = {"a": [202, 200], "b": [204], "c": [202, 202, 202]}

            def get(self, path, ttl_hours=0):
                self.calls.append(path)
                st = self.answers[path].pop(0)
                return Result(st, [1] if st == 200 else None)

        fake, sleeps = FakeApi(), []
        out = gh_source._stats_all(fake, ["a", "b", "c"], 1, 5, sleeps.append)
        self.assertEqual(out, {"a": [1], "b": [], "c": None})
        self.assertEqual(fake.calls, ["a", "b", "c", "a", "c", "c"])
        self.assertEqual(sleeps, [5, 5])


class TestCoinGecko(unittest.TestCase):
    def test_ticker_metrics_use_major_exchanges_only(self):
        t = lambda ex, vol, up=0, down=0, spread=None, **kw: {  # noqa: E731
            "market": {"identifier": ex}, "converted_volume": {"usd": vol}, "cost_to_move_up_usd": up,
            "cost_to_move_down_usd": down, "bid_ask_spread_percentage": spread, **kw}
        m = cg_source.ticker_metrics([t("binance", 100, 10, 20, 0.1), t("kraken", 300, 1, 1, 0.5),
                                      t("washex", 10_000, 999, 999, 0.01), t("binance", 50, is_stale=True)],
                                     ["binance", "kraken"])
        self.assertEqual((m["exchanges_count"], m["major_exchanges_count"]), (3, 2))
        self.assertEqual((m["major_volume_24h_usd"], m["depth_2pct_usd"]), (400, 32))
        self.assertAlmostEqual(m["spread_pct"], 0.4)                    # volume-weighted
        self.assertIsNone(cg_source.ticker_metrics([t("washex", 5)], ["binance"])["depth_2pct_usd"])

    def test_backfill_drops_today_and_maps_dates(self):
        day = 86400 * 1000
        start = int(datetime(2026, 10, 10, tzinfo=timezone.utc).timestamp() * 1000)
        chart = {"prices": [[start, 1.0], [start + day, 2.0], [start + day + 3600_000, 3.0]],
                 "market_caps": [[start, 10.0], [start + day, 20.0]], "total_volumes": [[start, 5.0]]}
        self.assertEqual(cg_source.backfill_rows("x", chart),
                         [("x", "2026-10-10", 1.0, 10.0, 5.0, "backfill"), ("x", "2026-10-11", 2.0, 20.0, None,
                                                                             "backfill")])


class TestDB(unittest.TestCase):
    def test_metrics_need_source_and_latest_as_of(self):
        db = ScreenerDB(":memory:")
        with self.assertRaises(ValueError):
            db.save_metrics("2026-10-12", "x", {"a": 1}, "", NOW)
        db.save_metrics("2026-10-05", "x", {"a": 1, "cats": ["L1"]}, "https://s/1", NOW)
        db.save_metrics("2026-10-12", "x", {"a": 2}, "https://s/2", NOW)
        self.assertEqual(db.latest_metrics("x", "2026-10-10")["a"]["value"], 1)
        self.assertEqual(db.latest_metrics("x", "2026-10-12")["a"]["source_url"], "https://s/2")
        self.assertEqual(db.latest_metrics("x", "2026-10-12")["cats"]["text"], '["L1"]')

    def test_backfill_never_overwrites_markets_row(self):
        db = ScreenerDB(":memory:")
        db.save_prices([("x", "2026-10-11", 5.0, 50.0, 1.0, "markets")])
        db.save_prices([("x", "2026-10-11", 4.0, 40.0, 1.0, "backfill"), ("x", "2026-10-10", 3.0, 30.0, 1.0,
                                                                         "backfill")])
        rows = db.conn.execute("SELECT date, price, source FROM price_daily ORDER BY date").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("2026-10-10", 3.0, "backfill"), ("2026-10-11", 5.0, "markets")])

    def test_due_order_never_fetched_by_given_order_then_oldest(self):
        db = ScreenerDB(":memory:")
        db.mark_fetched("p", "b", NOW - 10 * 86400)
        db.mark_fetched("p", "c", NOW - 1 * 86400)
        self.assertEqual(due(db, "p", ["zz", "aa", "c", "b"], 7, NOW), ["zz", "aa", "b"])


class TestStepErrors(unittest.TestCase):
    CFG = {"screener": {"coingecko": {"profile_refresh_days": 7, "major_exchanges": []}}}

    def test_budget_stop_propagates_but_item_failure_is_skipped(self):
        from coinsieve.screener import pipeline

        class Boom:
            def __init__(self, exc):
                self.exc = exc

            def get(self, *a, **k):
                raise self.exc

        db = ScreenerDB(":memory:")
        with self.assertRaises(BudgetExceeded):
            pipeline.step_profiles(self.CFG, db, Boom(BudgetExceeded("cap")), NOW, "2026-10-12", ["a"], 5)
        msg = pipeline.step_profiles(self.CFG, db, Boom(RuntimeError("HTTP 500")), NOW, "2026-10-12", ["a"], 5)
        self.assertIn("0 profiles fetched, 0 still due", msg)        # retried tomorrow, not now
        self.assertEqual(due(db, "cg_profile", ["a"], 7, NOW + 86400), ["a"])

    def test_github_metrics_saved_even_when_owner_was_fetched_by_an_interrupted_run(self):
        from coinsieve.screener import pipeline
        cfg = {"screener": {"github": {"refresh_days": 7}}}
        db = ScreenerDB(":memory:")
        db.set_links("btc", "github", ["https://github.com/bitcoin/bitcoin"], NOW)
        db.set_kv("github:bitcoin", {"repos": ["bitcoin/bitcoin"], "commit_weeks": {int(NOW - 86400 * 7): 9},
                                     "contributors": {}, "stars": 1.0, "forks": 1.0, "last_push_ts": NOW,
                                     "active_repos_90d": 1.0, "incomplete": 0}, NOW)
        db.mark_fetched("github", "bitcoin", NOW)            # fetched earlier, metrics never saved
        msg = pipeline.step_github(cfg | {"screener": {"github": {"refresh_days": 7}}}, db, None, NOW,
                                   "2026-10-12", ["btc"], 5)
        self.assertIn("1 coins updated", msg)
        self.assertEqual(db.latest_metrics("btc", "2026-10-12")["commits_90d"]["value"], 9)


if __name__ == "__main__":
    unittest.main()
