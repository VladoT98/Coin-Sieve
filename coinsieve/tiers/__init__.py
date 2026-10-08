"""Tier pipelines. Each tier's evaluate() returns a TierRun."""
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class TierRun:
    tier: str
    results: list = field(default_factory=list)    # [(metrics, reasons)] for evaluated tokens
    counts: Counter = field(default_factory=Counter)
    new: int = 0                                     # newly discovered tokens
    outage: bool = False                             # data source failed -> skip membership changes
    aged_out: set = field(default_factory=set)       # addresses that left the tier's age range this run

    def passing(self):
        """Addresses that are tier members this run."""
        return {m["address"] for m, _ in self.results if m.get("tier_member")}
