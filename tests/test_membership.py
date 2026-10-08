"""Tier membership rules: entered / left (hysteresis, aged out) / graduated / re-entry."""
import unittest

from coinsieve import membership
from coinsieve.store import Store
from coinsieve.tiers import TierRun

H = 3600
CFG = {"leave_after_hours": 2}
GRAD = {"from": "new_launches", "to": "emerging"}


def tier_run(tier, passing=(), failing=(), aged_out=(), outage=False):
    run = TierRun(tier, outage=outage, aged_out=set(aged_out))
    run.results = [({"address": a, "symbol": a.upper(), "tier_member": True}, []) for a in passing]
    run.results += [({"address": a, "symbol": a.upper(), "tier_member": False}, ["low_liquidity: x"])
                    for a in failing]
    return run


class MembershipTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def update(self, run, t):
        return [(e["kind"], e["address"]) for e in membership.update(self.store, run, t * H, CFG, GRAD)]

    def test_enter_then_stay(self):
        self.assertEqual(self.update(tier_run("new_launches", passing=["a"]), 0), [("entered", "a")])
        self.assertEqual(self.update(tier_run("new_launches", passing=["a"]), 1), [])

    def test_leave_only_after_grace_period(self):
        self.update(tier_run("new_launches", passing=["a"]), 0)
        self.assertEqual(self.update(tier_run("new_launches", failing=["a"]), 1), [])   # 1h < 2h
        events = membership.update(self.store, tier_run("new_launches", failing=["a"]), 2 * H, CFG, GRAD)
        self.assertEqual([(e["kind"], e["detail"]) for e in events], [("left", "low_liquidity: x")])

    def test_pass_resets_grace_period(self):
        self.update(tier_run("new_launches", passing=["a"]), 0)
        self.update(tier_run("new_launches", failing=["a"]), 1)
        self.update(tier_run("new_launches", passing=["a"]), 1.5)
        self.assertEqual(self.update(tier_run("new_launches", failing=["a"]), 3), [])   # 1.5h since last pass

    def test_aged_out_leaves_immediately(self):
        self.update(tier_run("new_launches", passing=["a"]), 0)
        events = membership.update(self.store, tier_run("new_launches", aged_out=["a"]), 0.5 * H, CFG, GRAD)
        self.assertEqual([(e["kind"], e["detail"]) for e in events], [("left", "aged_out")])

    def test_config_exclusion_leaves_immediately(self):
        self.update(tier_run("established", passing=["a"]), 0)
        run = tier_run("established")
        run.results = [({"address": "a", "symbol": "A", "tier_member": False}, ["excluded_mint: x"])]
        events = membership.update(self.store, run, 0.1 * H, CFG, GRAD)
        self.assertEqual([(e["kind"], e["detail"]) for e in events], [("left", "excluded_mint: x")])

    def test_outage_changes_nothing(self):
        self.update(tier_run("new_launches", passing=["a"]), 0)
        self.assertEqual(self.update(tier_run("new_launches", outage=True), 10), [])
        self.assertIn("a", self.store.current_members("new_launches"))

    def test_reentry_after_leaving(self):
        self.update(tier_run("new_launches", passing=["a"]), 0)
        self.update(tier_run("new_launches", failing=["a"]), 3)
        self.assertEqual(self.update(tier_run("new_launches", passing=["a"]), 4), [("entered", "a")])

    def test_graduation(self):
        self.update(tier_run("new_launches", passing=["a"]), 0)
        self.update(tier_run("new_launches", aged_out=["a"]), 48)
        self.assertEqual(self.update(tier_run("emerging", passing=["a", "b"]), 200),
                         [("entered", "a"), ("graduated", "a"), ("entered", "b")])


if __name__ == "__main__":
    unittest.main()
