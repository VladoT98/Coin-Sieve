# Coin Sieve

## Goal
Python script that finds newly launched Solana tokens, filters them very strictly, and posts at most 5 per day to a Telegram channel. Descriptive facts only.

## Pipeline
1. Fetch recently launched tokens from the DexScreener API (no key).
2. Keep only tokens 6-48 hours old.
3. Hard filters: min liquidity, min volume, min trades, socials listed.
4. RugCheck API (docs: https://api.rugcheck.xyz/swagger/index.html): require mint and freeze authority revoked, liquidity locked/burned, top-holder concentration under a cap, no danger-level flags. If the check fails or is incomplete, reject.
5. Rank survivors (holder growth, sane volume/liquidity ratio). Post top 5 max. Never post the same token twice.
6. If nothing passes, post nothing (or a one-line "0 passed").

## Rules
- All thresholds live in config.yaml, never hardcoded. Starting values are guesses and must be tuned from real data.
- Never guess API field names. Call each API first, print a sample response, then code against it.
- Handle launchpad tokens (bonding curve before migration) explicitly, based on the real responses.
- Log every evaluated token and its rejection reason to logs/.
- Secrets only in .env. Never print or commit them.
- Post wording: neutral facts ("Mint authority revoked"). Never use "safe", "gem", "verified", "buy", or any return prediction. Every post ends with: Not financial advice. Passing the filter is not a recommendation.
- Dry-run mode by default: print to console, only post to Telegram when --post is passed.

## Build order
Stage 1: fetch + age/liquidity filters, console output only.
Stage 2: add RugCheck checks.
Stage 3: ranking, daily cap, dedupe, Telegram posting to the TEST channel.
Stage 4: scheduling.
Stop after each stage and wait for review.

## Verified API facts (DexScreener, 2026-10-07)
- Discovery feeds (`token-profiles/latest/v1`, `token-boosts/latest/v1`, `token-boosts/top/v1`, `community-takeovers/latest/v1`) return only the ~30 newest entries, mixed chains (`chainId`, `tokenAddress`). Measured turnover: 6 new Solana tokens in 5 min (~70/h); profiles feed's ~24 Solana entries span ~30 min → poll every ≤15 min; store accumulates candidates across runs (SQLite, `data/coinsieve.db`).
- `tokens/v1/{chain}/{a,b,...}` (≤30 addrs) returns ONE main pair per token. For migrated tokens that is the AMM pool, whose `pairCreatedAt` is migration time, not launch.
- `token-pairs/v1/{chain}/{addr}` returns ALL pairs incl. the old bonding-curve pair → launch = min `pairCreatedAt` (ms).
- Bonding-curve dexIds seen: `pumpfun`, `meteoradbc` — they have no `liquidity` key. Migrated pump.fun tokens trade on `pumpswap`.
- Some pairs have no `liquidity`/`marketCap` and volume 0 even when not on a bonding curve (DexScreener can't price them) → rejected as `no_liquidity_data`.
- Tickers collide (two different "UP" tokens seen) → always identify tokens by address.
- Outage mode seen 2026-10-07: pair endpoints answer HTTP 200 with `[]` for every token (even BONK) while feeds still work. sieve.py skips evaluation when no in-window token gets pair data.

## Verified API facts (RugCheck, 2026-10-07, 51 reports)
- `GET /v1/tokens/{mint}/report` — no key. Header `X-Rate-Limit-Limit: 15`; 1 req/s gave no 429s. Bulk endpoints need auth.
- `mintAuthority` / `freezeAuthority` top-level, null = revoked (all pump.fun tokens are revoked).
- `risks[]`: {name, value, description, score, level}; levels seen: `warn`, `danger`. Danger names seen: Low Liquidity, Creator history of rugged tokens.
- `markets[].lp.lpLockedPct/lpLockedUSD/baseUSD/quoteUSD`. pump_fun (curve) and pump_fun_amm report 100% locked; third-party Meteora/Orca pools report 0% → locked share computed across ALL markets.
- `topHolders[]` includes pool vaults (4–96%). Excluded via market pubkey, `liquidityA/B` (vault addresses), `liquidityA/BAccount.owner`, or `knownAccounts` type AMM. Unidentified holders count.
- Data can be inconsistent (one token's top-10 summed to 115%).
