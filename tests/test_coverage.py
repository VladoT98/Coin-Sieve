"""Coverage list (all chains, CoinGecko): criteria, CoinGecko parsing, category exclusions, age bound, profile facts,
link discovery, holder analysis, URL checks, DB-only website."""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from coinsieve import coingecko, coverage, holders, profiles, site_text, webfetch
from coinsieve.evallog import EvalLog
from coinsieve.store import Store

with open("config.yaml", encoding="utf-8") as _f:
    CFG = yaml.safe_load(_f)

NOW = 1_791_700_000.0
DAY = 86400


def tok(**over):
    m = {"address": "zcash", "age_d": 3000, "age_checked": True, "mcap_usd": 20e9, "volume_h24_usd": 4e8,
         "excluded_categories": [], "exchanges": 40, "profile_checked": True, "website_or_docs": True,
         "links_found": ["website", "whitepaper"]}
    m.update(over)
    return m


class CoverageCriteriaTest(unittest.TestCase):
    def test_covered_token_passes_every_criterion(self):
        chks = coverage.checks(tok(), CFG)
        self.assertEqual(coverage.status(chks), "pass")
        self.assertEqual(coverage.reasons(chks), [])

    def test_each_criterion_fails_with_its_reason(self):
        cases = {"too_young": tok(age_d=200), "low_mcap": tok(mcap_usd=5e6), "low_volume": tok(volume_h24_usd=1e4),
                 "excluded_type": tok(excluded_categories=["stablecoins"]), "few_exchanges": tok(exchanges=1),
                 "no_website_or_docs": tok(website_or_docs=False, links_found=["x"])}
        for code, m in cases.items():
            chks = coverage.checks(m, CFG)
            self.assertEqual(coverage.status(chks), "fail", code)
            self.assertTrue(coverage.reasons(chks)[0].startswith(code), (code, coverage.reasons(chks)))

    def test_excluded_by_hand(self):
        cfg = {**CFG, "coverage": {**CFG["coverage"], "exclude_ids": ["zcash"]}}
        self.assertTrue(coverage.reasons(coverage.checks(tok(), cfg))[0].startswith("excluded_type"))

    def test_unknown_profile_is_not_covered_and_not_failed(self):
        m = tok(profile_checked=False, website_or_docs=None, links_found=[], exchanges=None, excluded_categories=None,
                age_d=100, age_checked=False)
        chks = coverage.checks(m, CFG)
        self.assertEqual(coverage.status(chks), "unknown")
        self.assertTrue(all(r.startswith("not_checked_yet") for r in coverage.reasons(chks)), coverage.reasons(chks))

    def test_young_lower_bound_fails_only_once_age_was_checked(self):
        self.assertIsNone(next(c for c in coverage.checks(tok(age_d=100, age_checked=False), CFG) if c["id"] == "age")["ok"])
        self.assertFalse(next(c for c in coverage.checks(tok(age_d=100, age_checked=True), CFG) if c["id"] == "age")["ok"])

    def test_links_requirement_can_be_switched_off(self):
        cfg = {**CFG, "coverage": {**CFG["coverage"], "require_website_or_docs": False}}
        chks = coverage.checks(tok(website_or_docs=False), cfg)
        self.assertEqual(coverage.status(chks), "pass")   # shown as a fact, not a criterion
        self.assertTrue(next(c for c in chks if c["id"] == "links")["info"])

    def test_market_ok_ignores_profile(self):
        self.assertTrue(coverage.market_ok(tok(website_or_docs=None, exchanges=None, age_d=50, age_checked=False), CFG))
        self.assertFalse(coverage.market_ok(tok(mcap_usd=1), CFG))
        self.assertFalse(coverage.market_ok(tok(excluded_categories=["wrapped-tokens"]), CFG))
        self.assertFalse(coverage.market_ok(tok(age_d=50, age_checked=True), CFG))

    def test_labels_come_from_site_text(self):
        self.assertEqual(coverage.checks(tok(), CFG)[0]["label"], site_text.load()["checks"]["age"]["label"])


# Shapes of real CoinGecko answers (zcash, 2026-10-11), trimmed.
ZEC_MARKET = {"id": "zcash", "symbol": "zec", "name": "Zcash", "image": "https://coin-images.coingecko.com/coins/images/486/large/z.png",
              "current_price": 1230.7, "market_cap": 20897495055, "market_cap_rank": 10, "fully_diluted_valuation": 20897527745,
              "total_volume": 402659792, "circulating_supply": 16980234.5, "total_supply": 16980390.8, "max_supply": 21000000.0,
              "ath_date": "2016-10-28T16:00:00.000Z", "atl_date": "2024-07-04T20:22:29.000Z",
              "last_updated": "2026-10-11T13:05:47.000Z", "price_change_percentage_1h_in_currency": -0.262,
              "price_change_percentage_24h_in_currency": 0.26867, "price_change_percentage_7d_in_currency": -7.5385,
              "sparkline_in_7d": {"price": [1300.0 - i for i in range(168)]}}
ZEC_COIN = {"id": "zcash", "asset_platform_id": None, "platforms": {"": ""}, "genesis_date": "2016-10-28",
            "categories": ["Privacy Coins", "Layer 1 (L1)"],
            "links": {"homepage": ["https://z.cash/"], "whitepaper": "https://zips.z.cash/protocol/protocol.pdf",
                      "repos_url": {"github": ["https://github.com/zcash/zcash"]}, "twitter_screen_name": "zcash"},
            "market_data": {"circulating_supply": 16980234.5, "total_supply": 16980261.1, "max_supply": 21000000.0},
            "tickers": [{"market": {"identifier": x}, "is_stale": False, "is_anomaly": False}
                        for x in ("binance", "binance", "coinbase", "kraken")]
            + [{"market": {"identifier": "stalex"}, "is_stale": True, "is_anomaly": False}]}


class CoinGeckoParsingTest(unittest.TestCase):
    def test_metrics_from_markets(self):
        m = coingecko.metrics(ZEC_MARKET, NOW)
        self.assertEqual((m["address"], m["symbol"], m["rank"], m["max_supply"]), ("zcash", "ZEC", 10, 21000000.0))
        self.assertGreater(m["history_days"], 3000)   # ATH in 2016 -> priced since at least then

    def test_sparkline_gets_hourly_timestamps_ending_at_last_update(self):
        pts = coingecko.sparkline(ZEC_MARKET)
        self.assertEqual(len(pts), 168)
        self.assertEqual(pts[-1][0] - pts[-2][0], 3600)
        self.assertEqual(pts[-1][0], round(coingecko._ts(ZEC_MARKET["last_updated"])))

    def test_summarize_coin(self):
        s = coingecko.summarize(ZEC_COIN)
        self.assertEqual((s["platform"], s["platforms"], s["exchanges"]), (None, {}, 3))   # stale ticker not counted
        s = coingecko.summarize({**ZEC_COIN, "asset_platform_id": "ethereum",
                                 "platforms": {"ethereum": " 0xabc ", "bad chain!": "x", "solana": "<script>"}})
        self.assertEqual((s["platform"], s["platforms"]), ("ethereum", {"ethereum": "0xabc"}))
        self.assertEqual(coingecko.summarize(None), {"listed": False})


class CategoryAndAgeTest(unittest.TestCase):
    def test_excluded_by_needs_every_list_for_a_clean_result(self):
        cats = CFG["coverage"]["exclude_categories"]
        full = {c: set() for c in cats}
        full["stablecoins"] = {"tether"}
        self.assertEqual(coverage.excluded_by("tether", full, CFG), ["stablecoins"])
        self.assertEqual(coverage.excluded_by("zcash", full, CFG), [])
        partial = {"stablecoins": {"tether"}}
        self.assertEqual(coverage.excluded_by("tether", partial, CFG), ["stablecoins"])   # a hit is certain
        self.assertIsNone(coverage.excluded_by("zcash", partial, CFG))                     # "no hit" is not

    def test_age_is_the_largest_known_lower_bound(self):
        m = {"history_days": 30}
        self.assertEqual(coverage.age_days(m, {"genesis_ts": NOW - 400 * DAY, "first_price_ts": None}, NOW), 400)
        self.assertEqual(coverage.age_days(m, {"genesis_ts": None, "first_price_ts": NOW - 200 * DAY}, NOW), 200)
        self.assertEqual(coverage.age_days(m, {}, NOW), 30)
        self.assertIsNone(coverage.age_days({"history_days": None}, {}, NOW))


class FakeCG:
    """CoinGecko client stand-in: markets pages, categories, coins, charts."""
    def __init__(self, coins, cats, charts=None, fail_markets=False):
        self.coins, self.cats, self.charts, self.fail = coins, cats, charts or {}, fail_markets
        self.calls = []

    def markets(self, page=1, ids=None, category=None, sparkline=True):
        self.calls.append(("markets", page, category))
        if self.fail:
            raise RuntimeError("HTTP 429")
        if category is not None:
            return [{"id": i} for i in self.cats.get(category, [])]
        rows = [c for c in self.coins if not ids or c["id"] in ids]
        return rows if page == 1 else []

    def coin(self, cid):
        self.calls.append(("coin", cid))
        return {**ZEC_COIN, "id": cid, "genesis_date": ZEC_COIN["genesis_date"] if cid == "zcash" else None}

    def market_chart(self, cid, days):
        self.calls.append(("chart", cid))
        return self.charts.get(cid, {"prices": []})


def market(cid, mcap, ath_days_ago, **over):
    d = {**ZEC_MARKET, "id": cid, "symbol": cid[:4], "name": cid.title(), "market_cap": mcap,
         "ath_date": f"{_iso(NOW - ath_days_ago * DAY)}", "atl_date": f"{_iso(NOW - ath_days_ago * DAY)}"}
    d.update(over)
    return d


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class EvaluateTest(unittest.TestCase):
    """coverage.evaluate + profiles.update end to end, offline, on a temporary database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "t.db"))
        self.log = EvalLog(self.tmp.name, "test")
        self.cfg = json.loads(json.dumps(CFG))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_once(self, cg):
        return coverage.evaluate(self.cfg, cg, self.store, self.log, NOW)

    def test_pipeline(self):
        coins = [market("zcash", 20e9, 3000), market("tether", 100e9, 3000), market("newcoin", 300e6, 100),
                 market("tiny", 1e6, 3000)]
        cg = FakeCG(coins, {"stablecoins": ["tether"]},
                    charts={"newcoin": {"prices": [[(NOW - 100 * DAY) * 1000, 1.0], [NOW * 1000, 2.0]]}})
        run = self.run_once(cg)
        by = {m["address"]: (m, why) for m, why in run.results}
        self.assertNotIn("tiny", by)                                       # below scope: only counted
        self.assertEqual(run.counts["below_scope_mcap"], 1)
        self.assertTrue(by["tether"][1][0].startswith("excluded_type"))
        self.assertTrue(all(r.startswith("not_checked_yet") for r in by["zcash"][1]))   # no profile yet
        self.assertEqual(len(json.loads(self.store.chart_rows(["zcash"])["zcash"]["points"])), 168)

        # profiles for the market-ok tokens; newcoin's age is checked via its 1-year chart
        cands = {m["address"]: m for m, _ in run.results if coverage.market_ok(m, self.cfg)}
        self.assertEqual(set(cands), {"zcash", "newcoin"})
        with mock.patch.object(profiles, "build", lambda cid, m, d, cgs, old, p, now, f: {
                "links": {"website": {"url": "https://z.cash/", "status": "ok"}}, "coingecko": cgs, "icon": None,
                **{k: (old or {})[k] for k in ("first_price_ts", "age_checked_at") if k in (old or {})}}):
            st = profiles.update(self.store, self.cfg, list(cands), cands, cg, NOW)
        self.assertEqual((st["done"], st["age_checks"]), (2, 1))
        self.assertNotIn(("chart", "zcash"), cg.calls)                    # old enough from market data alone

        run = self.run_once(cg)
        by = {m["address"]: (m, why) for m, why in run.results}
        self.assertEqual(by["zcash"][1], [])
        self.assertTrue(by["newcoin"][1][0].startswith("too_young"), by["newcoin"][1])

    def test_outage_and_legacy_members(self):
        self.store.add_member("A" * 43, "coverage", "OLD", NOW - DAY)   # Solana-only list
        run = self.run_once(FakeCG([], {}, fail_markets=True))
        self.assertTrue(run.outage)
        self.assertEqual(self.store.current_members("coverage"), {})   # closed without an event
        self.assertEqual(self.store.events_since(0), [])

    def test_category_lists_are_cached(self):
        cg = FakeCG([market("zcash", 20e9, 3000)], {})
        self.run_once(cg)
        self.run_once(cg)
        n = sum(1 for c in cg.calls if c[0] == "markets" and c[2] is not None)
        self.assertEqual(n, len(self.cfg["coverage"]["exclude_categories"]))


class ProfileFactsTest(unittest.TestCase):
    C = CFG["coverage"]

    def prof(self, cg=None, **links):
        return {"links": {k: {"url": "https://x.io", "status": v} for k, v in links.items()}, "coingecko": cg}

    def test_facts_from_coingecko(self):
        cg = {"listed": True, "platform": "ethereum", "platforms": {"ethereum": "0xabc"}, "exchanges": 7,
              "genesis_date": "2020-01-02"}
        f = profiles.facts({**self.prof(cg, website="ok"), "age_checked_at": 1}, self.C)
        self.assertEqual((f["exchanges"], f["platform"], f["contracts"], f["age_checked"], f["website_or_docs"]),
                         (7, "ethereum", {"ethereum": "0xabc"}, True, True))
        self.assertEqual(f["genesis_ts"], 1577923200.0)

    def test_unknowns_stay_unknown(self):
        self.assertIsNone(profiles.facts(None, self.C)["exchanges"])
        self.assertIsNone(profiles.facts(self.prof(None, website="ok"), self.C)["exchanges"])          # lookup failed
        self.assertIsNone(profiles.facts(self.prof({"listed": False}, website="ok"), self.C)["exchanges"])  # not listed
        self.assertFalse(profiles.facts(self.prof({"listed": True, "exchanges": 3}), self.C)["age_checked"])

    def test_dead_links_do_not_count_blocked_ones_do(self):
        self.assertFalse(profiles.facts(self.prof({"listed": False}, website="dead"), self.C)["website_or_docs"])
        self.assertTrue(profiles.facts(self.prof({"listed": False}, website="blocked"), self.C)["website_or_docs"])
        self.assertFalse(profiles.facts(self.prof({"listed": False}, x="listed", github="listed"), self.C)["website_or_docs"])


class DexInfoTest(unittest.TestCase):
    def test_issuing_chain_contract_matched_case_insensitively(self):
        dex = mock.Mock()
        dex._get.return_value = [{"baseToken": {"address": "0x514910771AF9Ca656af840dff83E8264EcF986CA"},
                                  "info": {"websites": [{"url": "https://chain.link/", "label": "Website"}]}}]
        cg = {"platform": "ethereum", "platforms": {"ethereum": "0x514910771af9ca656af840dff83e8264ecf986ca"}}
        self.assertEqual(profiles.dex_info(dex, cg)["websites"][0]["url"], "https://chain.link/")
        dex._get.assert_called_once_with("tokens/v1/ethereum/0x514910771af9ca656af840dff83e8264ecf986ca")
        self.assertIsNone(profiles.dex_info(dex, {"platform": None, "platforms": {}}))   # own-chain coin: no contract

    def test_only_website_entries_become_the_website(self):
        info = {"websites": [{"url": "https://www.tiktok.com/@x", "label": "TikTok"}, {"url": "https://proj.io", "label": "Website"}]}
        seen = []
        def f(url, max_bytes):
            seen.append(url)
            return page(url)
        p = profiles.build("a", {}, info, {"listed": True, "homepage": []}, None, CFG["profiles"], 1.0, f)
        self.assertEqual(p["links"]["website"]["url"], "https://proj.io")
        self.assertNotIn("https://www.tiktok.com/@x", seen)


class SolanaMintsTest(unittest.TestCase):
    def test_only_tokens_issued_on_solana(self):
        mint = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
        profs = {"jupiter": {"data": {"coingecko": {"platform": "solana", "platforms": {"solana": mint}}}},
                 "bridged": {"data": {"coingecko": {"platform": "ethereum", "platforms": {"solana": mint}}}},
                 "zcash": {"data": {"coingecko": {"platform": None, "platforms": {}}}}}
        self.assertEqual(holders.solana_mints(profs), {"jupiter": mint})


def page(url, status=200, ctype="text/html", body=b"<html></html>"):
    return webfetch.Page(url, status, ctype, body, False)


class LinkDiscoveryTest(unittest.TestCase):
    PC = CFG["profiles"]
    HOME = (b'<a href="/docs">Docs</a><a href="/paper.pdf">Whitepaper</a><a href="/terms">Terms of use</a>'
            b'<a href="https://github.com/proj">GitHub</a><a href="https://evil.example/x">elsewhere</a>')

    def fetcher(self, pages):
        def f(url, max_bytes):
            if url in pages:
                return pages[url]
            raise webfetch.BlockedURL("DNS lookup failed for test")
        return f

    def test_homepage_links_are_classified(self):
        pages = {"https://proj.io": page("https://proj.io", body=self.HOME),
                 "https://proj.io/docs": page("https://proj.io/docs"),
                 "https://proj.io/paper.pdf": page("https://proj.io/paper.pdf", ctype="application/pdf", body=b"%PDF-1.7")}
        p = profiles.build("A", {"website": "https://proj.io", "twitter": "https://twitter.com/proj"}, None, None, None,
                           self.PC, 1.0, self.fetcher(pages))
        L = p["links"]
        self.assertEqual((L["website"]["status"], L["whitepaper"]["format"], L["docs"]["url"]), ("ok", "pdf", "https://proj.io/docs"))
        self.assertEqual((L["github"]["url"], L["x"]["source"]), ("https://github.com/proj", "jupiter"))

    def test_whitepaper_that_is_the_homepage_is_ignored_and_dead_links_marked(self):
        cg = {"listed": True, "platform": "solana", "categories": [], "homepage": [], "whitepaper": "https://proj.io/",
              "github": [], "twitter": None, "supply": {}}
        pages = {"https://proj.io": page("https://proj.io")}
        p = profiles.build("A", {"website": "https://proj.io"}, None, cg, None, self.PC, 1.0, self.fetcher(pages))
        self.assertIsNone(p["links"]["whitepaper"])
        self.assertIsNone(p["links"]["docs"])   # docs.proj.io did not answer -> not listed
        p = profiles.build("A", {"website": "https://gone.io"}, None, None, None, self.PC, 1.0, self.fetcher({}))
        self.assertEqual(p["links"]["website"]["status"], "dead")

    def test_failed_coingecko_lookup_keeps_old_data(self):
        old = {"coingecko": {"listed": True, "platform": "solana", "categories": []}, "coingecko_checked_at": 5}
        p = profiles.build("A", {}, None, None, old, self.PC, 9.0, self.fetcher({}))
        self.assertEqual((p["coingecko"]["platform"], p["coingecko_checked_at"]), ("solana", 5))


def rc_report():
    """Shape of a real RugCheck report (JUP, 2026-10-09), trimmed and with made-up addresses."""
    hold = lambda addr, owner, pct: {"address": addr, "owner": owner, "pct": pct, "insider": False}  # noqa: E731
    th = [hold("V1", "PoolAuth", 30.0), hold("T1", "W1", 20.0), hold("T1b", "W1", 2.0), hold("T2", "Locker1", 5.0),
          hold("T3", "Stake1", 4.0), hold("T4", "Cex1", 3.0)] + [hold(f"T{i}", f"W{i}", 1.0) for i in range(5, 16)]
    return {"topHolders": th, "markets": [{"pubkey": "M1", "liquidityA": "V1", "liquidityB": "V2"}],
            "knownAccounts": {"Locker1": {"name": "Jupiter Locker", "type": "LOCKER"}, "M1": {"name": "Pool", "type": "AMM"}}}


class HoldersTest(unittest.TestCase):
    H = {**CFG["holders"], "exchange_wallets": {"Cex1": {"name": "Some Exchange", "source": "test"}},
         "program_labels": {"StakeProg": "Staking program"}}

    def test_kinds_aggregation_and_top10(self):
        accounts = {"Stake1": {"program": "StakeProg", "executable": False}, "W1": {"program": holders.SYSTEM_PROGRAM}}
        d = holders.analyse(rc_report(), accounts, self.H)
        kinds = {r["owner"]: (r["kind"], r["label"]) for r in d["holders"]}
        self.assertEqual(kinds["PoolAuth"][0], "pool")                       # owns the pool vault V1
        self.assertEqual(kinds["Locker1"], ("locker", "Jupiter Locker"))
        self.assertEqual(kinds["Stake1"], ("contract", "Staking program"))  # owner account controlled by a program
        self.assertEqual(kinds["Cex1"], ("exchange", "Some Exchange"))
        self.assertEqual(kinds["W1"], ("wallet", None))
        self.assertEqual(next(r["pct"] for r in d["holders"] if r["owner"] == "W1"), 22.0)  # two accounts, one owner
        self.assertEqual(d["top10_all_pct"], 30 + 22 + 5 + 4 + 3 + 5 * 1.0)
        self.assertEqual(d["top10_excl_pct"], 22 + 9 * 1.0)
        self.assertEqual(d["excluded_pct"], 42.0)
        self.assertTrue(d["complete"])

    def test_owner_held_by_token_program_is_not_a_contract(self):
        accounts = {"W1": {"program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "executable": False}}
        d = holders.analyse(rc_report(), accounts, self.H)
        self.assertEqual(next(r["kind"] for r in d["holders"] if r["owner"] == "W1"), "wallet")

    def test_lower_bound_when_too_few_unidentified_wallets(self):
        r = rc_report()
        r["topHolders"] = r["topHolders"][:8]
        d = holders.analyse(r, {}, self.H)
        self.assertFalse(d["complete"])


class WebFetchTest(unittest.TestCase):
    def test_only_public_https(self):
        self.assertEqual(webfetch.check_url("http://proj.io/a#x", resolve=False), "https://proj.io/a")
        for bad in ("ftp://proj.io", "javascript:alert(1)", "https://user:pw@proj.io", "https://proj.io:8443/", ""):
            with self.assertRaises(webfetch.BlockedURL):
                webfetch.check_url(bad, resolve=False)
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(webfetch.BlockedURL):
                webfetch.check_url("https://localhost.example")
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.1.2.3", 443))]):
            with self.assertRaises(webfetch.BlockedURL):
                webfetch.check_url("https://intranet.example")

    def test_links_and_domain(self):
        found = webfetch.links(b'<a href="/docs">Read the <b>docs</b></a><a href="mailto:a@b.c">m</a>', "https://www.proj.io/")
        self.assertEqual(found, [("https://www.proj.io/docs", "Read the docs")])
        self.assertEqual(webfetch.site_domain("https://www.bonkcoin.com/x"), "bonkcoin.com")


class SiteTextWordingTest(unittest.TestCase):
    def test_no_banned_words_or_judgement_phrases(self):
        pub = CFG["publishing"]
        for s in site_text.strings():
            low = s.lower()
            for w in ("safe", "gem", "verified"):
                self.assertNotIn(w, low, s)
            self.assertIsNone(re.search(r"\bbuy", low), s)
            for phrase in pub["banned_phrases"]:
                self.assertNotIn(phrase, low, s)

    def test_disclaimer(self):
        self.assertEqual(site_text.load()["disclaimer"], "Not financial advice. Not a recommendation.")


class DashboardReadsOnlyTest(unittest.TestCase):
    def test_server_module_makes_no_outbound_calls(self):
        from pathlib import Path
        src = Path("coinsieve/dashboard.py").read_text(encoding="utf-8")
        for needle in ("import requests", "urllib.request", "urlopen", "logo_png", "fetch_logo"):
            self.assertNotIn(needle, src)


class StaticExportTest(unittest.TestCase):
    def test_export_writes_static_public_site(self):
        from coinsieve import dashboard
        state = {"mode": "public", "tokens": [{"metrics": {"address": "zcash"}, "logo": True}]}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(dashboard.App, "__init__", lambda self, *a, **k: None), \
                mock.patch.object(dashboard.App, "state", lambda self: state), \
                mock.patch.object(dashboard.App, "market_view", lambda self: ({"market": None}, None)),                 mock.patch.object(dashboard.App, "token_detail", lambda self, a: {"address": a, "points": []}):
            dashboard.App.icons, dashboard.App.logo_dir = {}, Path(d)
            try:
                self.assertEqual(dashboard.export("config.yaml", d), 1)
            finally:
                del dashboard.App.icons, dashboard.App.logo_dir
            html = Path(d, "index.html").read_text(encoding="utf-8")
            self.assertIn("window.CS_STATIC = true", html)
            saved = json.loads(Path(d, "api", "state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["mode"], "public")
            self.assertFalse(saved["tokens"][0]["logo"])   # no cached file -> no broken image link
            self.assertTrue(Path(d, ".nojekyll").exists())
            detail = json.loads(Path(d, "api", "token", "zcash.json").read_text(encoding="utf-8"))
            self.assertEqual(detail["address"], "zcash")   # token page data next to the list


if __name__ == "__main__":
    unittest.main()
