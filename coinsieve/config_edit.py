"""Update scalar values in config.yaml in place, keeping comments and layout.

Only `key: value` lines addressed by a dotted path (e.g. "tiers.emerging.min_holders") are touched.
The result is re-parsed and checked: the requested values must be set and nothing else may change.
"""
import copy
import os
import re

import yaml

_KEY_LINE = re.compile(r"^( *)([A-Za-z0-9_]+):(.*)$")
_VALUE_COMMENT = re.compile(r"^(\s*)([^#]*?)(\s+#.*)?$")


def fmt(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def get_path(cfg, path):
    node = cfg
    for k in path.split("."):
        node = node[k]
    return node


def set_path(cfg, path, value):
    *parents, last = path.split(".")
    node = cfg
    for k in parents:
        node = node[k]
    node[last] = value


def set_values(text, updates):
    """Return config text with the dotted-path scalars in `updates` replaced."""
    lines = text.splitlines(keepends=True)
    stack, found = [], set()
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        m = _KEY_LINE.match(body)
        if not m or body.lstrip().startswith("#"):
            continue
        indent, key, rest = len(m.group(1)), m.group(2), m.group(3)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([k for _, k in stack] + [key])
        if path in updates:
            vm = _VALUE_COMMENT.match(rest)
            found.add(path)
            if yaml.safe_load(vm.group(2) or "null") != updates[path]:  # untouched if value unchanged
                lines[i] = f"{m.group(1)}{key}: {fmt(updates[path])}{vm.group(3) or ''}{line[len(body):]}"
        stack.append((indent, key))
    missing = set(updates) - found
    if missing:
        raise KeyError(f"not found in config: {', '.join(sorted(missing))}")
    new_text = "".join(lines)

    expected = copy.deepcopy(yaml.safe_load(text))
    for path, value in updates.items():
        set_path(expected, path, value)
    if yaml.safe_load(new_text) != expected:
        raise ValueError("config edit changed more than the requested values; aborted")
    return new_text


def write_values(path, updates):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    new_text = set_values(text, updates)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new_text)
    os.replace(tmp, path)  # atomic on the same volume
