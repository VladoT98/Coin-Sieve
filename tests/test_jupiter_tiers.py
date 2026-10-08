"""Emerging/Established rules on Jupiter token objects (shapes copied from live responses)."""
import unittest
from datetime import datetime, timezone

from coinsieve.tiers.jupiter_tiers import evaluate, in_age_range, jup_metrics

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc).timestamp()
EMERGING = {"min_age_days": 7, "max_age_days": 180, "min_mcap_usd": 1e6, "max_mcap_usd": 50e6,
            "min_liquidity_usd": 250e3, "min_volume_h24_usd": 100e3, "min_holders": 5000,
            "min_organic_score": 50, "require_mint_authority_disabled": True,
            "require_freeze_authority_disabled": True, "max_top_holders_pct": 30}
EXCL = {"tags": {"stable", "lst"}, "mints": {"So11111111111111111111111111111111111111112"}}


def token(**over):
    t = {"id": "Tok1", "symbol": "TOK", "name": "Token", "mcap": 5e6, "fdv": 5e6, "liquidity": 500e3,
         "holderCount": 20000, "organicScore": 80, "tags": ["verified"],
         "firstPool": {"createdAt": "2026-09-08T00:00:00Z"},
         "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 15},
         "stats24h": {"holderChange": 1.5, "buyVolume": 300e3, "sellVolume": 200e3,
                      "buyOrganicVolume": 150e3, "sellOrganicVolume": 100e3}}
    t.update(over)
    return t


class JupiterTierTest(unittest.TestCase):
    def test_metrics(self):
        m = jup_metrics(token(), NOW)
        self.assertEqual(m["age_d"], 30.0)
        self.assertEqual(m["volume_h24_usd"], 500e3)
        self.assertEqual(m["organic_volume_pct"], 50.0)

    def test_clean_token_is_member(self):
        self.assertEqual(evaluate(jup_metrics(token(), NOW), EMERGING, EXCL), [])

    def test_missing_audit_keys_count_as_not_disabled(self):
        reasons = evaluate(jup_metrics(token(audit={"topHoldersPercentage": 10}), NOW), EMERGING, EXCL)
        self.assertIn("mint_authority_not_disabled", reasons)
        self.assertIn("freeze_authority_not_disabled", reasons)

    def test_exclusions(self):
        reasons = evaluate(jup_metrics(token(tags=["stable", "verified"]), NOW), EMERGING, EXCL)
        self.assertTrue(any(r.startswith("excluded_tag: stable") for r in reasons))
        sol = token(id="So11111111111111111111111111111111111111112")
        self.assertTrue(any(r.startswith("excluded_mint") for r in evaluate(jup_metrics(sol, NOW), EMERGING, EXCL)))

    def test_missing_liquidity_rejects(self):
        reasons = evaluate(jup_metrics(token(liquidity=None), NOW), EMERGING, EXCL)
        self.assertIn("missing_liquidity_usd", reasons)

    def test_unknown_age_is_out_of_scope(self):
        m = jup_metrics(token(firstPool=None), NOW)
        self.assertIsNone(m["age_d"])
        self.assertFalse(in_age_range(m["age_d"], EMERGING))

    def test_open_ended_age_range(self):
        self.assertTrue(in_age_range(5000, {"min_age_days": 365, "max_age_days": None}))


if __name__ == "__main__":
    unittest.main()
