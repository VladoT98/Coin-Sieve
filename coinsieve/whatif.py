"""Re-evaluate the last run's tokens against edited thresholds, without calling any API.

Uses the same rule functions as the pipeline. What cannot be known from stored data is reported as
"unknown" instead of guessed (e.g. a token that never reached RugCheck because it failed earlier).
Editable fields are declared here — the dashboard renders them from FIELDS.
"""
import copy

from coinsieve.config_edit import get_path, set_path
from coinsieve.filters import evaluate as hard_evaluate
from coinsieve.tiers.jupiter_tiers import evaluate as jup_evaluate
from coinsieve.tiers.jupiter_tiers import in_age_range

# (dotted config path, label, kind, help). kind: num | num_or_null | bool
_NL = [
    ("age.min_hours", "Min age (hours)", "num", "Tokens younger than this wait."),
    ("age.max_hours", "Max age (hours)", "num", "Older tokens leave New Launches."),
    ("filters.min_liquidity_usd", "Min liquidity ($)", "num", "Main pool liquidity, both sides."),
    ("filters.min_volume_h24_usd", "Min 24h volume ($)", "num", ""),
    ("filters.min_txns_h24", "Min 24h trades", "num", "Purchases + sales in the main pool."),
    ("filters.min_socials", "Min socials listed", "num", "Twitter/Telegram/... on DexScreener."),
    ("filters.max_dev_mints", "Max creator launches", "num", "Tokens launched by the same creator."),
    ("rugcheck.min_lp_locked_pct", "Min LP locked (%)", "num", "Locked/burned share of all pools."),
    ("rugcheck.max_top1_holder_pct", "Max largest holder (%)", "num", "Excluding pool accounts."),
    ("rugcheck.max_top10_holders_pct", "Max top-10 holders (%)", "num", "Excluding pool accounts."),
    ("tiers.new_launches.leave_after_hours", "Grace before leaving (h)", "num",
     "A member must fail this long before it leaves."),
    ("ranking.min_vol_liq_ratio", "Post: min volume/liquidity", "num", "Daily post selection only."),
    ("ranking.max_vol_liq_ratio", "Post: max volume/liquidity", "num", "Daily post selection only."),
    ("ranking.min_history_hours", "Post: min holder history (h)", "num", "Daily post selection only."),
    ("ranking.min_holder_growth_per_hour", "Post: min holder growth/h", "num", "Daily post selection only."),
]


def _jup(tier):
    p = f"tiers.{tier}."
    return [
        (p + "min_age_days", "Min age (days)", "num", ""),
        (p + "max_age_days", "Max age (days)", "num_or_null", "Empty = no limit."),
        (p + "scope_min_mcap_usd", "Track from mcap ($)", "num",
         "Tokens below this are not evaluated at all (applies from the next run)."),
        (p + "min_mcap_usd", "Min market cap ($)", "num", ""),
        (p + "max_mcap_usd", "Max market cap ($)", "num_or_null", "Empty = no limit."),
        (p + "min_liquidity_usd", "Min liquidity ($)", "num", ""),
        (p + "min_volume_h24_usd", "Min 24h volume ($)", "num", ""),
        (p + "min_holders", "Min holders", "num", ""),
        (p + "min_organic_score", "Min organic score (0-100)", "num", "Jupiter's share of non-bot trading."),
        (p + "require_mint_authority_disabled", "Require mint authority revoked", "bool", ""),
        (p + "require_freeze_authority_disabled", "Require freeze authority revoked", "bool", ""),
        (p + "max_top_holders_pct", "Max top holders (%)", "num_or_null", "Empty = no limit."),
        (p + "leave_after_hours", "Grace before leaving (h)", "num", "A member must fail this long before it leaves."),
    ]


FIELDS = {"new_launches": _NL, "emerging": _jup("emerging"), "established": _jup("established")}

# Stored reasons that don't depend on editable thresholds -> carried over as-is.
_FIXED_NL = {"no_pair_data", "jupiter_incomplete", "jupiter_failed", "rugged", "mint_authority_active",
             "freeze_authority_active", "risk_danger", "rugcheck_incomplete", "rugcheck_failed"}


def current_values(cfg, tier):
    return {path: get_path(cfg, path) for path, *_ in FIELDS[tier]}


def validate(tier, values):
    """Coerce/validate edited values; raises ValueError with a readable message."""
    kinds = {path: (label, kind) for path, label, kind, _ in FIELDS[tier]}
    out = {}
    for path, v in values.items():
        if path not in kinds:
            raise ValueError(f"not an editable field: {path}")
        label, kind = kinds[path]
        if kind == "bool":
            if not isinstance(v, bool):
                raise ValueError(f"{label}: must be true/false")
        elif v is None or v == "":
            if kind != "num_or_null":
                raise ValueError(f"{label}: required")
            v = None
        else:
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{label}: must be a number") from None
            if v < 0:
                raise ValueError(f"{label}: must be >= 0")
            v = int(v) if v.is_integer() else v
        out[path] = v
    return out


def with_values(cfg, values):
    new = copy.deepcopy(cfg)
    for path, v in values.items():
        set_path(new, path, v)
    return new


def new_launch_status(m, stored, cfg):
    """('pass' | 'fail' | 'unknown', reasons) for one stored New Launches evaluation."""
    if "age_h" not in m:
        return "fail", stored
    reasons = []
    a = cfg["age"]
    if not a["min_hours"] <= m["age_h"] <= a["max_hours"]:
        reasons.append(f"outside_age_window: {m['age_h']:.1f}h")
    reasons += hard_evaluate(m, cfg["filters"])
    if reasons:
        return "fail", reasons
    fixed = [r for r in stored if r.split(":")[0] in _FIXED_NL]
    if m.get("dev_mints") is None:
        return ("fail", fixed) if fixed else (
            "unknown", ["not_checked_yet: creator + RugCheck run only after market filters pass (next run)"])
    if m["dev_mints"] > cfg["filters"]["max_dev_mints"]:
        return "fail", [f"serial_creator: creator launched {m['dev_mints']} tokens > {cfg['filters']['max_dev_mints']}"]
    rc = cfg["rugcheck"]
    if m.get("rc_lp_locked_pct") is None:
        return ("fail", fixed) if fixed else ("unknown", ["not_checked_yet: RugCheck (next run)"])
    if m["rc_lp_locked_pct"] < rc["min_lp_locked_pct"]:
        reasons.append(f"lp_not_locked: {m['rc_lp_locked_pct']}% < {rc['min_lp_locked_pct']}%")
    if (m.get("rc_top1_holder_pct") or 0) > rc["max_top1_holder_pct"]:
        reasons.append(f"top1_holder: {m['rc_top1_holder_pct']}% > {rc['max_top1_holder_pct']}%")
    if (m.get("rc_top10_holders_pct") or 0) > rc["max_top10_holders_pct"]:
        reasons.append(f"top10_holders: {m['rc_top10_holders_pct']}% > {rc['max_top10_holders_pct']}%")
    reasons = fixed + reasons
    return ("fail" if reasons else "pass"), reasons


def jupiter_status(m, tier, cfg):
    c = cfg["tiers"][tier]
    excl = {"tags": set(cfg["jupiter"]["exclude_tags"]), "mints": set(cfg["jupiter"]["exclude_mints"])}
    reasons = [] if in_age_range(m.get("age_d"), c) else [f"outside_age_range: {m.get('age_d')} days"]
    reasons += jup_evaluate(m, c, excl)
    return ("fail" if reasons else "pass"), reasons


def evaluate_all(tier, tokens, cfg):
    """tokens: [{"metrics", "reasons"}] -> {address: (status, reasons)}."""
    out = {}
    for t in tokens:
        m = t["metrics"]
        out[m["address"]] = (new_launch_status(m, t["reasons"], cfg) if tier == "new_launches"
                             else jupiter_status(m, tier, cfg))
    return out


# --- per-token checklist ("why it passed / failed"), per-filter impact, typical values ---------

def _usd(v):
    if v is None:
        return "–"
    for div, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:,.1f}{s}"
    return f"${v:,.0f}"


def _n(v):
    return "–" if v is None else f"{v:,.0f}"


def _p(v):
    return "–" if v is None else ("100%" if v >= 99.95 else f"{v:.1f}%")


def _chk(cid, label, ok, value, limit, paths=(), info=False):
    """ok: True / False / None (not checked yet). info=True: shown for context, not a filter."""
    return {"id": cid, "label": label, "ok": ok, "value": value, "limit": limit, "paths": list(paths), "info": info}


def _min(cid, label, v, lim, fmt, path):
    return _chk(cid, label, v is not None and v >= lim, fmt(v), f"min {fmt(lim)}", [path])


def _max(cid, label, v, lim, fmt, path):
    return _chk(cid, label, v is not None and v <= lim, fmt(v), f"max {fmt(lim)}", [path])


_DATA_CODES = {"jupiter_incomplete", "jupiter_failed"}
_RC_CODES = {"rugged", "mint_authority_active", "freeze_authority_active", "risk_danger",
             "rugcheck_incomplete", "rugcheck_failed"}


def new_launch_checks(m, stored, cfg):
    if "age_h" not in m:
        return [_chk("data", "Market data available", False, "missing", "")]
    a, f, rc = cfg["age"], cfg["filters"], cfg["rugcheck"]
    codes = {r.split(":")[0] for r in stored}
    on_curve, liq = m.get("on_bonding_curve"), m.get("liquidity_usd")
    out = [
        _chk("age_min", "Age", m["age_h"] >= a["min_hours"], f"{m['age_h']:.1f}h", f"min {a['min_hours']}h", ["age.min_hours"]),
        _chk("age_max", "Age", m["age_h"] <= a["max_hours"], f"{m['age_h']:.1f}h", f"max {a['max_hours']}h", ["age.max_hours"]),
        _chk("launchpad", "Trades on a DEX pool (left the launchpad)", not on_curve, m.get("dex_id") or "–", ""),
        _chk("liquidity", "Liquidity", None if on_curve else (liq is not None and liq >= f["min_liquidity_usd"]),
             "on launchpad" if on_curve else _usd(liq), f"min {_usd(f['min_liquidity_usd'])}", ["filters.min_liquidity_usd"]),
        _min("volume", "Volume 24h", m.get("volume_h24_usd") or 0, f["min_volume_h24_usd"], _usd, "filters.min_volume_h24_usd"),
        _min("txns", "Trades 24h", m.get("txns_h24") or 0, f["min_txns_h24"], _n, "filters.min_txns_h24"),
        _min("socials", "Socials listed", len(m.get("socials") or []), f["min_socials"], _n, "filters.min_socials"),
    ]
    data_bad = bool(codes & _DATA_CODES)
    dev = m.get("dev_mints")
    out.append(_chk("creator", "Creator's launches", False if data_bad else (None if dev is None else dev <= f["max_dev_mints"]),
                    "not checked yet" if dev is None else _n(dev), f"max {f['max_dev_mints']}", ["filters.max_dev_mints"]))
    lp, top1, top10 = m.get("rc_lp_locked_pct"), m.get("rc_top1_holder_pct"), m.get("rc_top10_holders_pct")
    unchecked = lp is None
    out += [
        _chk("lp", "Liquidity locked or burned", None if unchecked else lp >= rc["min_lp_locked_pct"],
             "not checked yet" if unchecked else _p(lp), f"min {rc['min_lp_locked_pct']}%", ["rugcheck.min_lp_locked_pct"]),
        _chk("top1", "Largest wallet", None if unchecked else (top1 or 0) <= rc["max_top1_holder_pct"],
             "not checked yet" if unchecked else _p(top1), f"max {rc['max_top1_holder_pct']}%", ["rugcheck.max_top1_holder_pct"]),
        _chk("top10", "Top 10 wallets", None if unchecked else (top10 or 0) <= rc["max_top10_holders_pct"],
             "not checked yet" if unchecked else _p(top10), f"max {rc['max_top10_holders_pct']}%", ["rugcheck.max_top10_holders_pct"]),
    ]
    rc_bad = sorted(codes & _RC_CODES)
    out.append(_chk("rugcheck", "RugCheck: no danger flags, authorities revoked",
                    False if rc_bad else (None if unchecked else True),
                    ", ".join(c.replace("_", " ") for c in rc_bad) if rc_bad else ("not checked yet" if unchecked else "clear"), ""))
    if data_bad:
        out.append(_chk("data", "Complete data from Jupiter", False, "missing", ""))
    return out


def jupiter_checks(m, tier, cfg):
    c, j = cfg["tiers"][tier], cfg["jupiter"]
    p = f"tiers.{tier}."
    age = m.get("age_d")
    fmt_d = lambda v: "–" if v is None else f"{v:,.0f} days"  # noqa: E731
    out = [_chk("age_min", "Age", age is not None and age >= c["min_age_days"], fmt_d(age), f"min {fmt_d(c['min_age_days'])}",
                [p + "min_age_days"])]
    if c["max_age_days"] is not None:
        out.append(_max("age_max", "Age", age, c["max_age_days"], fmt_d, p + "max_age_days"))
    bad = sorted(set(m.get("tags") or []) & set(j["exclude_tags"]))
    excluded_mint = m["address"] in set(j["exclude_mints"])
    out.append(_chk("type", "Project token (not a stablecoin, staking or stock token)", not bad and not excluded_mint,
                    ", ".join(bad) or ("excluded asset" if excluded_mint else "project token"), ""))
    out.append(_min("mcap_min", "Market cap", m.get("mcap_usd"), c["min_mcap_usd"], _usd, p + "min_mcap_usd"))
    if c["max_mcap_usd"] is not None:
        out.append(_max("mcap_max", "Market cap", m.get("mcap_usd"), c["max_mcap_usd"], _usd, p + "max_mcap_usd"))
    out += [
        _min("liquidity", "Liquidity", m.get("liquidity_usd"), c["min_liquidity_usd"], _usd, p + "min_liquidity_usd"),
        _min("volume", "Volume 24h", m.get("volume_h24_usd"), c["min_volume_h24_usd"], _usd, p + "min_volume_h24_usd"),
        _min("holders", "Holders", m.get("holders"), c["min_holders"], _n, p + "min_holders"),
        _min("organic", "Organic score", m.get("organic_score"), c["min_organic_score"], _n, p + "min_organic_score"),
    ]
    for key, label, flag in (("mint", "Mint authority revoked", "mint_authority_disabled"),
                             ("freeze", "Freeze authority revoked", "freeze_authority_disabled")):
        required = c[f"require_{flag}"]
        out.append(_chk(key, label, bool(m.get(flag)), "yes" if m.get(flag) else "no",
                        "required" if required else "not required", [p + f"require_{flag}"], info=not required))
    if c["max_top_holders_pct"] is not None:
        out.append(_max("top", "Top wallets", m.get("top_holders_pct"), c["max_top_holders_pct"], _p, p + "max_top_holders_pct"))
    else:
        out.append(_chk("top", "Top wallets", True, _p(m.get("top_holders_pct")), "no limit", [p + "max_top_holders_pct"], info=True))
    return out


def checks_status(checks):
    real = [c for c in checks if not c["info"]]
    if any(c["ok"] is False for c in real):
        return "fail"
    return "unknown" if any(c["ok"] is None for c in real) else "pass"


def checklist(tier, token, cfg):
    m = token["metrics"]
    return new_launch_checks(m, token["reasons"], cfg) if tier == "new_launches" else jupiter_checks(m, tier, cfg)


def impact(tier, checklists):
    """{field path: number of tokens that fail the check driven by that field}."""
    counts = {path: 0 for path, *_ in FIELDS[tier]}
    for checks in checklists:
        for c in checks:
            if c["ok"] is False and not c["info"]:
                for path in c["paths"]:
                    if path in counts:
                        counts[path] += 1
    return counts


# field path -> (metric key, unit) for "typical values" hints
def _hints(tier):
    if tier == "new_launches":
        return {"age.min_hours": ("age_h", "h"), "age.max_hours": ("age_h", "h"),
                "filters.min_liquidity_usd": ("liquidity_usd", "usd"), "filters.min_volume_h24_usd": ("volume_h24_usd", "usd"),
                "filters.min_txns_h24": ("txns_h24", "n"), "filters.min_socials": ("socials", "n"),
                "filters.max_dev_mints": ("dev_mints", "n"), "rugcheck.min_lp_locked_pct": ("rc_lp_locked_pct", "pct"),
                "rugcheck.max_top1_holder_pct": ("rc_top1_holder_pct", "pct"),
                "rugcheck.max_top10_holders_pct": ("rc_top10_holders_pct", "pct"),
                "ranking.min_vol_liq_ratio": ("vol_liq_ratio", "x"), "ranking.max_vol_liq_ratio": ("vol_liq_ratio", "x"),
                "ranking.min_holder_growth_per_hour": ("holder_growth_per_h", "n")}
    p = f"tiers.{tier}."
    return {p + "min_age_days": ("age_d", "d"), p + "max_age_days": ("age_d", "d"), p + "scope_min_mcap_usd": ("mcap_usd", "usd"),
            p + "min_mcap_usd": ("mcap_usd", "usd"), p + "max_mcap_usd": ("mcap_usd", "usd"),
            p + "min_liquidity_usd": ("liquidity_usd", "usd"), p + "min_volume_h24_usd": ("volume_h24_usd", "usd"),
            p + "min_holders": ("holders", "n"), p + "min_organic_score": ("organic_score", "n"),
            p + "max_top_holders_pct": ("top_holders_pct", "pct")}


def typical(tier, tokens):
    """{path: {"p25", "median", "p75", "n", "unit"}} from the latest run's tokens (middle half of values)."""
    out = {}
    for path, (key, unit) in _hints(tier).items():
        vals = sorted(v for v in ((len(t["metrics"].get("socials") or []) if key == "socials" else t["metrics"].get(key))
                                  for t in tokens) if isinstance(v, (int, float)))
        if len(vals) < 4:
            continue
        q = lambda f: vals[min(len(vals) - 1, int(f * (len(vals) - 1) + 0.5))]  # noqa: E731
        out[path] = {"p25": q(.25), "median": q(.5), "p75": q(.75), "n": len(vals), "unit": unit}
    return out


def presets(cfg, tier):
    """Validated presets for a tier: {name: {path: value}}."""
    return {name: validate(tier, values) for name, values in (cfg.get("presets", {}).get(tier) or {}).items()}
