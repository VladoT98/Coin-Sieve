"""Loads coinsieve/site_text.yaml — every text the website shows (one place, checked by the wording tests)."""
from functools import lru_cache
from pathlib import Path

import yaml

PATH = Path(__file__).with_name("site_text.yaml")


@lru_cache(maxsize=1)
def _cached(mtime):
    with open(PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load():
    """Parsed texts; re-read when the file changes (edits show up without a server restart)."""
    return _cached(PATH.stat().st_mtime)


def check_label(cid):
    return (load().get("checks") or {}).get(cid, {}).get("label", cid)


def strings(node=None):
    """Every string in the file (for the banned-words test)."""
    node = load() if node is None else node
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from strings(v)
