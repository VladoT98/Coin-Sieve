"""v3 coverage list: criteria, profile facts, link discovery, holder analysis, URL checks, DB-only website."""
import re
import unittest
from unittest import mock

import yaml

from coinsieve import coverage, holders, profiles, site_text, webfetch

with open("config.yaml", encoding="utf-8") as _f:
    CFG = yaml.safe_load(_f)


def tok(**over):
    m = {"address": "A" * 43, "age_d": 500, "mcap_usd": 50e6, "liquidity_usd": 1e6, "tags": ["verified"],
         "mint_authority_disabled": True, "freeze_authority_disabled": True,
         "profile_checked": True, "native": True, "native_detail": "solana", "website_or_docs": True,
         "links_found": ["website", "docs"]}
    m.update(over)
    return m


class CoverageCriteriaTest(unittest.TestCase):
    def test_covered_token_passes_every_criterion(self):
        chks = coverage.checks(tok(), CFG)
        self.assertEqual(coverage.status(chks), "pass")
        self.assertEqual(coverage.reasons(chks), [])

    def test_each_criterion_fails_with_its_reason(self):
        cases = {"too_young": tok(age_d=200), "low_mcap": tok(mcap_usd=5e6), "low_liquidity": tok(liquidity_usd=1e5),
                 "excluded_type": tok(tags=["stable"]), "not_native": tok(native=False, native_detail="Bridged-Tokens"),
                 "no_website_or_docs": tok(website_or_docs=False, links_found=["x"])}
        for code, m in cases.items():
            chks = coverage.checks(m, CFG)
            self.assertEqual(coverage.status(chks), "fail", code)
            self.assertTrue(coverage.reasons(chks)[0].startswith(code), (code, coverage.reasons(chks)))

    def test_unknown_profile_is_not_covered_and_not_failed(self):
        m = tok(profile_checked=False, native=None, native_detail="not checked yet", website_or_docs=None, links_found=[])
        chks = coverage.checks(m, CFG)
        self.assertEqual(coverage.status(chks), "unknown")
        self.assertTrue(all(r.startswith("not_checked_yet") for r in coverage.reasons(chks)))

    def test_requirements_can_be_switched_off(self):
        cfg = {**CFG, "coverage": {**CFG["coverage"], "require_native_chain": False}}
        chks = coverage.checks(tok(native=False), cfg)
        self.assertEqual(coverage.status(chks), "pass")   # shown as a fact, not a criterion
        self.assertTrue(next(c for c in chks if c["id"] == "native")["info"])

    def test_authorities_are_facts_not_criteria(self):
        chks = coverage.checks(tok(mint_authority_disabled=False), CFG)
        self.assertEqual(coverage.status(chks), "pass")
        self.assertEqual(next(c for c in chks if c["id"] == "mint")["value"], "active")

    def test_market_ok_ignores_profile(self):
        self.assertTrue(coverage.market_ok(tok(native=None, website_or_docs=None), CFG))
        self.assertFalse(coverage.market_ok(tok(mcap_usd=1), CFG))

    def test_labels_come_from_site_text(self):
        self.assertEqual(coverage.checks(tok(), CFG)[0]["label"], site_text.load()["checks"]["age"]["label"])


class ProfileFactsTest(unittest.TestCase):
    C = CFG["coverage"]

    def prof(self, cg=None, **links):
        return {"links": {k: {"url": "https://x.io", "status": v} for k, v in links.items()}, "coingecko": cg}

    def test_native_from_coingecko_platform_and_categories(self):
        f = profiles.facts(self.prof({"listed": True, "platform": "solana", "categories": []}, website="ok"), self.C)
        self.assertEqual((f["native"], f["website_or_docs"]), (True, True))
        f = profiles.facts(self.prof({"listed": True, "platform": "polygon-pos", "categories": []}, website="ok"), self.C)
        self.assertEqual((f["native"], f["native_detail"]), (False, "polygon-pos"))
        f = profiles.facts(self.prof({"listed": True, "platform": "solana", "categories": ["Bridged-Tokens"]}), self.C)
        self.assertEqual((f["native"], f["native_detail"]), (False, "Bridged-Tokens"))

    def test_unknowns_stay_unknown(self):
        self.assertIsNone(profiles.facts(None, self.C)["native"])
        self.assertIsNone(profiles.facts(self.prof(None, website="ok"), self.C)["native"])          # lookup failed
        self.assertIsNone(profiles.facts(self.prof({"listed": False}, website="ok"), self.C)["native"])  # not on CoinGecko

    def test_dead_links_do_not_count_blocked_ones_do(self):
        self.assertFalse(profiles.facts(self.prof({"listed": False}, website="dead"), self.C)["website_or_docs"])
        self.assertTrue(profiles.facts(self.prof({"listed": False}, website="blocked"), self.C)["website_or_docs"])
        self.assertFalse(profiles.facts(self.prof({"listed": False}, x="listed", github="listed"), self.C)["website_or_docs"])


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
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        from coinsieve import dashboard
        state = {"mode": "public", "tokens": [{"metrics": {"address": "A" * 43}, "logo": True}]}
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
            detail = json.loads(Path(d, "api", "token", "A" * 43 + ".json").read_text(encoding="utf-8"))
            self.assertEqual(detail["address"], "A" * 43)   # token page data next to the list


if __name__ == "__main__":
    unittest.main()
