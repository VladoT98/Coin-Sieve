"""GitHub (token) -> development activity per coin. Verified 2026-10-12 (docs/api_samples/gh_*.json).

CoinGecko's repo link can be stale (AAVE lists aave/aave-protocol, archived-era), so we look at the OWNER: its
`repos_per_owner` most recently pushed non-fork, non-archived repos (+ any repo CoinGecko lists, if active).
Per repo: /stats/commit_activity (52 weeks of commit counts) and /stats/contributors (per author, per week).
Both answer 202 while GitHub computes them -> retried a few times, else the repo counts as "incomplete";
204 = empty repository (zero activity).
"""
import logging
import re
from datetime import datetime

log = logging.getLogger(__name__)

GH_URL = re.compile(r"^https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})(?:/([A-Za-z0-9._-]{1,100}))?/?$")
WEEK = 7 * 86400


def owners_and_repos(urls):
    """(ordered owners, {owner: set(repo names CoinGecko listed)}) from CoinGecko github links."""
    owners, listed = [], {}
    for u in urls:
        m = GH_URL.match((u or "").strip())
        if not m:
            continue
        owner = m.group(1).lower()
        if owner not in listed:
            owners.append(owner)
            listed[owner] = set()
        if m.group(2):
            listed[owner].add(m.group(2).removesuffix(".git").lower())
    return owners, listed


def _iso_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def pick_repos(repos, listed, n):
    """Non-fork, non-archived repos: the listed ones first, then the most recently pushed, n at most."""
    live = [r for r in repos if isinstance(r, dict) and not r.get("fork") and not r.get("archived")]
    live.sort(key=lambda r: r.get("pushed_at") or "", reverse=True)
    first = [r for r in live if (r.get("name") or "").lower() in listed]
    rest = [r for r in live if r not in first]
    return (first + rest)[:n]


def _stats_all(api, paths, ttl_hours, retry_s, sleep, rounds=3):
    """{path: list | None}. All paths are asked first (each 202 starts GitHub's computation, so they run in
    parallel), then only the pending ones are asked again after `retry_s`, up to `rounds` passes in total."""
    out, pending = {}, list(paths)
    for i in range(rounds):
        if i:
            sleep(retry_s)
        still = []
        for path in pending:
            res = api.get(path, ttl_hours=ttl_hours)
            if res.status == 202:
                still.append(path)
            elif res.status == 204:      # empty repository: no activity (complete, not "still computing")
                out[path] = []
            else:
                out[path] = res.data if res.ok and isinstance(res.data, list) else None
        pending = still
        if not pending:
            break
    for path in pending:
        out[path] = None
    return out


def fetch_owner(api, owner, listed, cfg, now, sleep):
    """Raw activity for one owner: {repos: [...], commit_weeks: {week_ts: n}, contributors: {login: [week_ts]},
    stars, forks, last_push_ts, active_repos_90d, incomplete}. None when the owner doesn't exist."""
    ttl = cfg["refresh_days"] * 24 - 1
    res = api.get(f"users/{owner}/repos?sort=pushed&per_page=100&type=owner", ttl_hours=ttl)
    if res.status == 404:
        return None
    if not res.ok or not isinstance(res.data, list):
        raise RuntimeError(f"github repos of {owner}: HTTP {res.status}")
    own = [r for r in res.data if isinstance(r, dict) and not r.get("fork")]
    pushes = [t for t in (_iso_ts(r.get("pushed_at")) for r in own) if t]
    raw = {"repos": [], "commit_weeks": {}, "contributors": {}, "incomplete": 0,
           "stars": float(sum(r.get("stargazers_count") or 0 for r in own)),
           "forks": float(sum(r.get("forks_count") or 0 for r in own)),
           "last_push_ts": max(pushes) if pushes else None,
           "active_repos_90d": float(sum(1 for t in pushes if now - t <= 90 * 86400))}
    picked = [r.get("full_name") for r in pick_repos(res.data, listed, cfg["repos_per_owner"])]
    stats = _stats_all(api, [f"repos/{full}/stats/{kind}" for full in picked
                             for kind in ("commit_activity", "contributors")], ttl, cfg["stats_retry_s"], sleep)
    for full in picked:
        raw["repos"].append(full)
        acts = stats[f"repos/{full}/stats/commit_activity"]
        contr = stats[f"repos/{full}/stats/contributors"]
        if acts is None or contr is None:
            raw["incomplete"] += 1
        for w in acts or []:
            if isinstance(w, dict) and isinstance(w.get("week"), int):
                raw["commit_weeks"][w["week"]] = raw["commit_weeks"].get(w["week"], 0) + (w.get("total") or 0)
        for c in contr or []:
            login = ((c or {}).get("author") or {}).get("login")
            if not login or login.endswith("[bot]"):
                continue
            weeks = [w["w"] for w in c.get("weeks") or [] if isinstance(w, dict) and (w.get("c") or 0) > 0]
            raw["contributors"].setdefault(login, []).extend(weeks)
    return raw


def coin_metrics(raws, now):
    """Combine the raw data of a coin's owners into metrics (weeks counted by their start date)."""
    raws = [r for r in raws if r]
    if not raws:
        return None
    weeks, people = {}, {}
    for r in raws:
        for w, n in r["commit_weeks"].items():
            weeks[int(w)] = weeks.get(int(w), 0) + n
        for login, ws in r["contributors"].items():
            people.setdefault(login, set()).update(int(w) for w in ws)
    in_90 = lambda w: now - w <= 13 * WEEK   # noqa: E731
    in_1y = lambda w: now - w <= 52 * WEEK   # noqa: E731
    pushes = [r["last_push_ts"] for r in raws if r["last_push_ts"]]
    return {
        "commits_90d": float(sum(n for w, n in weeks.items() if in_90(w))),
        "commits_1y": float(sum(n for w, n in weeks.items() if in_1y(w))),
        "active_weeks_1y": float(sum(1 for w, n in weeks.items() if in_1y(w) and n > 0)),
        "contributors_90d": float(sum(1 for ws in people.values() if any(in_90(w) for w in ws))),
        "contributors_1y": float(sum(1 for ws in people.values() if any(in_1y(w) for w in ws))),
        "stars": sum(r["stars"] for r in raws),
        "forks": sum(r["forks"] for r in raws),
        "days_since_push": round((now - max(pushes)) / 86400, 1) if pushes else None,
        "active_repos_90d": sum(r["active_repos_90d"] for r in raws),
        "repos_sampled": [x for r in raws for x in r["repos"]],
        "repos_incomplete": float(sum(r["incomplete"] for r in raws)),
    }
