"""Dashboard market strip (market.py). Shapes copied from live responses on 2026-10-09. No network."""
import unittest

from coinsieve import market

NOW = 1_800_000_000
H = 3600
CFG = {"market": {"refresh_minutes": 10, "stale_after_minutes": 60, "change_window_hours": [20, 28], "keep_days": 8,
                  "coingecko_spacing_s": 0,
                  "urls": {"fng": "FNG", "coingecko": "CG", "binance_futures": "BN", "bybit": "BY"}}}

FNG = {"data": [{"value": "64", "value_classification": "Greed", "timestamp": "1791417600"},
                {"value": "71", "value_classification": "Greed", "timestamp": "1791331200"}]}
GLOBAL = {"data": {"total_market_cap": {"usd": 2_800_000_000_000, "btc": 35_000_000}, "total_volume": {"usd": 123e9},
                   "market_cap_change_percentage_24h_usd": -4.83}}
EXCHANGES = [{"open_interest_btc": 400_000, "trade_volume_24h_btc": "1000000.5"},
             {"open_interest_btc": None, "trade_volume_24h_btc": "10"}]
BN_TICKER = {"symbol": "SOLUSDT", "quoteVolume": "3415453579.5946"}
BN_HIST = [{"sumOpenInterestValue": "900000000", "timestamp": NOW * 1000 - 24 * H * 1000},
           {"sumOpenInterestValue": "1000000000", "timestamp": NOW * 1000}]
BY_TICKER = {"result": {"list": [{"openInterestValue": "670640482.45", "singleOpenInterestValue": "300000000",
                                  "turnover24h": "1195012868.259"}]}}
BY_HIST = {"result": {"list": [{"openInterest": "6000000", "singleOpenInterest": "3000000", "timestamp": str(NOW * 1000)},
                               {"openInterest": "5000000", "singleOpenInterest": "2500000",
                                "timestamp": str(NOW * 1000 - 24 * H * 1000)}]}}
BY_KLINE = {"result": {"list": [[str(NOW * 1000), "100", "101", "99", "100", "1", "1"],
                                [str(NOW * 1000 - 24 * H * 1000), "100", "101", "99", "100", "1", "1"]]}}


def fake_get(fail=()):
    def get(url, params=None):
        for key in fail:
            if url.startswith(key):
                raise OSError("HTTP Error 429: Too Many Requests")
        if url == "FNG":
            return FNG
        if url == "CG/global":
            return GLOBAL
        if url == "CG/derivatives/exchanges":
            return EXCHANGES if params["page"] == 1 else []
        return {"BN/fapi/v1/ticker/24hr": BN_TICKER, "BN/futures/data/openInterestHist": BN_HIST,
                "BY/v5/market/tickers": BY_TICKER, "BY/v5/market/open-interest": BY_HIST,
                "BY/v5/market/kline": BY_KLINE}[url]
    return get


class ParseTest(unittest.TestCase):
    def test_fng_and_global(self):
        self.assertEqual(market.parse_fng(FNG), {"value": 64, "label": "Greed", "prev": 71})
        g = market.parse_global(GLOBAL)
        self.assertEqual((g["mcap"], g["volume"], g["btc_usd"]), (2.8e12, 123e9, 80_000))

    def test_total_converts_btc_and_skips_missing(self):
        oi, vol, n = market.total_derivatives(EXCHANGES, 80_000)
        self.assertEqual((oi, n), (32e9, 1))
        self.assertAlmostEqual(vol, 1_000_010.5 * 80_000)

    def test_bybit_uses_one_side_open_interest(self):
        now, ago, vol = market.sol_bybit(BY_TICKER, BY_HIST, BY_KLINE)
        self.assertEqual((now, ago), (300e6, 250e6))  # singleOpenInterest x price at that hour, not the 2x figure
        self.assertAlmostEqual(vol, 1195012868.259)


class FetchTest(unittest.TestCase):
    def test_all_sections(self):
        d = market.fetch(CFG, None, get=fake_get(), sleep=lambda s: None, now=NOW)
        self.assertEqual(d["fng"]["value"], 64)
        self.assertEqual(d["total"]["oi"], 32e9)
        self.assertEqual(d["sol"]["oi"], 1.3e9)                       # Binance 1.0B + Bybit 0.3B
        self.assertAlmostEqual(d["sol"]["oi_change_24h_pct"], 13.04)  # vs 0.9B + 0.25B
        self.assertEqual(set(d["updated"]), {"fng", "global", "total", "sol"})

    def test_failing_source_keeps_last_value(self):
        first = market.fetch(CFG, None, get=fake_get(), sleep=lambda s: None, now=NOW)
        second = market.fetch(CFG, first, get=fake_get(fail=("CG",)), sleep=lambda s: None, now=NOW + 2 * H)
        self.assertEqual(second["global"], first["global"])            # kept on 429
        self.assertEqual(second["updated"]["global"], NOW)             # ...with its old timestamp
        self.assertEqual(second["updated"]["fng"], NOW + 2 * H)
        v = market.view({"ts": second["ts"], "data": second}, [], CFG, NOW + 2 * H)
        self.assertTrue(v["stale"]["global"])
        self.assertFalse(v["stale"]["fng"])

    def test_no_data_at_all(self):
        d = market.fetch(CFG, None, get=fake_get(fail=("FNG", "CG", "BN", "BY")), sleep=lambda s: None, now=NOW)
        self.assertNotIn("fng", d)
        self.assertIsNone(market.view(None, [], CFG, NOW))


class ChangeTest(unittest.TestCase):
    def test_total_oi_change_needs_a_day_of_history(self):
        snap = lambda ts, oi: {"ts": ts, "data": {"ts": ts, "total": {"oi": oi}}}
        latest = snap(NOW, 110e9)
        self.assertIsNone(market.view(latest, [snap(NOW - 2 * H, 100e9), latest], CFG, NOW)["total"]["oi_change_24h_pct"])
        hist = [snap(NOW - 30 * H, 50e9), snap(NOW - 25 * H, 100e9), snap(NOW - 21 * H, 90e9), latest]
        self.assertEqual(market.view(latest, hist, CFG, NOW)["total"]["oi_change_24h_pct"], 10.0)  # closest to 24h


if __name__ == "__main__":
    unittest.main()
