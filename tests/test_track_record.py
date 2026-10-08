"""Track record: checkpoint scheduling + neutral stats (no network: Jupiter is faked)."""
import unittest

from coinsieve import track_record
from coinsieve.store import Store

H = 3600
TR = {"checkpoints_hours": {"0h": 0, "24h": 24}, "catch_up_hours": 6, "baseline_max_delay_hours": 2}


class FakeJup:
    def __init__(self, tokens):
        self.tokens, self.calls = tokens, 0

    def search(self, mints):
        self.calls += 1
        return [self.tokens[m] for m in mints if m in self.tokens]


def tok(mint, price, liq, holders):
    return {"id": mint, "usdPrice": price, "mcap": price * 1e9, "liquidity": liq, "holderCount": holders}


class TrackRecordTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        for a in ("a", "b"):
            self.store.add_member(a, "new_launches", a.upper(), 0)

    def test_checkpoints_recorded_once_when_due(self):
        jup = FakeJup({"a": tok("a", 1.0, 100, 10), "b": tok("b", 2.0, 200, 20)})
        self.assertEqual(track_record.update(self.store, jup, 0.5 * H, TR), 2)     # two 0h baselines
        self.assertEqual(track_record.update(self.store, jup, 1 * H, TR), 0)       # nothing new due
        self.assertEqual(track_record.update(self.store, jup, 24 * H, TR), 2)      # 24h checkpoints

    def test_missed_checkpoint_is_skipped_not_taken_late(self):
        jup = FakeJup({"a": tok("a", 1.0, 100, 10), "b": tok("b", 2.0, 200, 20)})
        track_record.update(self.store, jup, 0, TR)
        self.assertEqual(track_record.update(self.store, jup, 40 * H, TR), 0)      # 24h + 6h catch-up passed

    def test_summary_counts_vanished_tokens(self):
        track_record.update(self.store, FakeJup({"a": tok("a", 1.0, 100, 10), "b": tok("b", 2.0, 200, 20)}), 0, TR)
        track_record.update(self.store, FakeJup({"a": tok("a", 0.5, 40, 15)}), 24 * H, TR)   # b vanished
        s = track_record.summarize(self.store.outcomes("new_launches"), "24h", float("-inf"), TR)
        self.assertEqual(s["n"], 2)
        self.assertEqual(s["not_found"], 1)
        self.assertEqual(s["median_price_change_pct"], -50.0)
        self.assertEqual(s["median_liquidity_change_pct"], -60.0)
        self.assertEqual(s["liquidity_down_50pct"], 1)
        self.assertEqual(s["in_a_tier"], 2)   # both still current members in this fixture

    def test_late_baseline_and_bootstrap_excluded(self):
        jup = FakeJup({"a": tok("a", 1.0, 100, 10), "b": tok("b", 2.0, 200, 20)})
        track_record.update(self.store, jup, 3 * H, TR)          # baseline 3h after entry > 2h allowed
        track_record.update(self.store, jup, 24 * H, TR)
        rows = self.store.outcomes("new_launches")
        self.assertEqual(track_record.summarize(rows, "24h", float("-inf"), TR)["n"], 0)
        self.assertEqual(track_record.summarize(rows, "24h", 1 * H, TR)["n"], 0)     # entered in bootstrap

    def test_api_failure_records_nothing(self):
        class Broken:
            def search(self, mints):
                raise RuntimeError("down")
        self.assertEqual(track_record.update(self.store, Broken(), 0, TR), 0)
        self.assertEqual(self.store.outcomes("new_launches"), [])


if __name__ == "__main__":
    unittest.main()
