"""Append-only JSONL log of every token evaluation (one file per UTC day)."""
import json
from datetime import datetime, timezone
from pathlib import Path


class EvalLog:
    def __init__(self, log_dir, run_id):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id

    def write(self, address, reasons, metrics=None, tier=None):
        now = datetime.now(timezone.utc)
        record = {
            "ts": now.isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "tier": tier,
            "address": address,
            "result": "reject" if reasons else "pass",
            "reasons": reasons,
            **(metrics or {}),
        }
        path = self.dir / f"evaluations-{now:%Y-%m-%d}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
