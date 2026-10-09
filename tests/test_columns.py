"""Column picker: config defaults, every column has its texts, and the browser's pickColumns() (run under Node)."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

from coinsieve import site_text

HTML = Path("coinsieve/dashboard.html").read_text(encoding="utf-8")
with open("config.yaml", encoding="utf-8") as _f:
    CFG = yaml.safe_load(_f)
COLS = dict(re.findall(r'\{ key: "(\w+)", group: (?:"(\w+)"|null)', HTML))   # key -> group ("" for Token)
PICK = re.search(r"// <pick-columns>.*?\n(function pickColumns.*?\n)// </pick-columns>", HTML, re.S).group(1)


class ColumnConfigTest(unittest.TestCase):
    def test_default_set(self):
        site = CFG["site"]
        default = site["default_columns"]
        self.assertNotIn("unlock", default)                        # selectable, not default
        self.assertIn("unlock", COLS)
        self.assertLessEqual(len(default), site["columns_max"])
        self.assertEqual(len(default), len(set(default)))
        for k in default:
            self.assertTrue(COLS.get(k), f"default column {k!r} is not a selectable column")

    def test_unlocks_admin_only_until_terms_cleared(self):
        self.assertIs(CFG["unlocks"]["public"], False)
        self.assertRegex(HTML, r'key: "unlock", group: "unlocks", admin: 1')

    def test_every_selectable_column_has_a_picker_group(self):
        # tooltip texts + Methodology anchors: test_dashboard_logic.WordingTest
        groups = site_text.load()["column_groups"]
        self.assertGreaterEqual(len([g for g in COLS.values() if g]), 15)
        for key, group in COLS.items():
            if group:
                self.assertIn(group, groups, key)

    def test_unlock_tooltip_names_source_and_coverage(self):
        body = site_text.load()["columns"]["unlock"]["body"]
        self.assertIn("DefiLlama", body)
        self.assertIn("Admin view only", body)


@unittest.skipUnless(shutil.which("node"), "Node.js not installed")
class PickColumnsTest(unittest.TestCase):
    AVAIL = ["price", "change_24h", "mcap", "holders", "unlock", "docs"]
    DEFAULT = ["price", "mcap", "docs"]

    def pick(self, saved, available=None, defaults=None, max_n=4):
        js = PICK + "process.stdout.write(JSON.stringify(pickColumns(...JSON.parse(process.argv[1]))));"
        args = json.dumps([saved, available or self.AVAIL, defaults or self.DEFAULT, max_n])
        return json.loads(subprocess.run(["node", "-e", js, args], capture_output=True, text=True, check=True).stdout)

    def test_nothing_saved_gives_default(self):
        for saved in (None, "price", 42, {}, []):
            self.assertEqual(self.pick(saved), self.DEFAULT, saved)

    def test_saved_order_is_kept(self):
        self.assertEqual(self.pick(["docs", "price"]), ["docs", "price"])

    def test_max_limit(self):
        self.assertEqual(self.pick(self.AVAIL, max_n=4), self.AVAIL[:4])
        self.assertEqual(self.pick(None, defaults=self.AVAIL, max_n=2), self.AVAIL[:2])

    def test_unknown_and_duplicate_ids_ignored(self):
        self.assertEqual(self.pick(["gone", "price", 7, None, "price", "mcap"]), ["price", "mcap"])
        self.assertEqual(self.pick(["gone", "removed"]), self.DEFAULT)

    def test_public_mode_drops_unlock(self):
        public = [k for k in self.AVAIL if k != "unlock"]
        self.assertEqual(self.pick(["unlock", "price"], available=public), ["price"])


if __name__ == "__main__":
    unittest.main()
