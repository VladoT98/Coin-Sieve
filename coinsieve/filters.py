"""Hard filters on DexScreener pair data. Thresholds come from config.yaml."""


def pair_metrics(pair, launch_ts, now, bonding_dex_ids):
    """Flatten a DexScreener pair into the fields we filter and log on."""
    base = pair.get("baseToken") or {}
    info = pair.get("info") or {}
    h24 = (pair.get("txns") or {}).get("h24") or {}
    return {
        "address": base.get("address"),
        "symbol": base.get("symbol"),
        "name": base.get("name"),
        "dex_id": pair.get("dexId"),
        "pair_address": pair.get("pairAddress"),
        "url": pair.get("url"),
        "age_h": round((now - launch_ts) / 3600, 2),
        # `liquidity` is absent on bonding-curve pairs (pumpfun, meteoradbc)
        "liquidity_usd": (pair.get("liquidity") or {}).get("usd"),
        "volume_h24_usd": (pair.get("volume") or {}).get("h24"),
        "txns_h24": h24.get("buys", 0) + h24.get("sells", 0),
        "market_cap_usd": pair.get("marketCap"),
        "socials": [s.get("type") for s in info.get("socials") or []],
        "websites": len(info.get("websites") or []),
        "icon": info.get("imageUrl"),  # cdn.dexscreener.com; untrusted, only served via logos.py
        "on_bonding_curve": pair.get("dexId") in bonding_dex_ids,
    }


def evaluate(m, f):
    """Return all rejection reasons ('code: detail'); empty list = passed."""
    reasons = []
    liq = m["liquidity_usd"]
    if m["on_bonding_curve"]:
        reasons.append(f"bonding_curve: main pair on {m['dex_id']}, not migrated")
    elif liq is None:
        reasons.append(f"no_liquidity_data: dexId={m['dex_id']}")
    elif liq < f["min_liquidity_usd"]:
        reasons.append(f"low_liquidity: {liq:,.0f} < {f['min_liquidity_usd']:,}")

    vol = m["volume_h24_usd"] or 0
    if vol < f["min_volume_h24_usd"]:
        reasons.append(f"low_volume: {vol:,.0f} < {f['min_volume_h24_usd']:,}")
    if m["txns_h24"] < f["min_txns_h24"]:
        reasons.append(f"few_txns: {m['txns_h24']} < {f['min_txns_h24']}")
    if len(m["socials"]) < f["min_socials"]:
        reasons.append(f"few_socials: {len(m['socials'])} < {f['min_socials']}")
    return reasons
