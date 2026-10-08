# Coin Sieve

## Goal (v2)
A Solana token screener product, aiming for revenue:
1. **Website** with 3 tabs: **New Launches | Emerging | Established**. Solana-native tokens only (no wrapped/bridged tokens, no stablecoins). Tier names describe age/liquidity — never "risk", "reward" or "picks".
2. **Telegram channel** linking to the website: daily New Launches digest; weekly digest of tier changes (entered / left / graduated) + track record; ad-hoc Established events.
3. **Later:** paid tier on the website selling features (alerts, filters, history), not picks.

## Tiers
- **New Launches** — tokens 6–48h old passing the pipeline below (built, Stages 1–3).
- **Emerging** — younger mid-size tokens (≈ 7–180 days, mcap $1M–50M, liquidity ≥ $250k; placeholders).
- **Established** — mature large tokens (≈ ≥ 1 year, mcap ≥ $200M, liquidity ≥ $2M; placeholders).
- A token **enters** a tier when it starts passing, **leaves** after failing for a configured time (hysteresis), **graduates** when a former New Launch enters Emerging.

## New Launches pipeline
1. Fetch recently launched tokens from the DexScreener API (no key).
2. Keep only tokens 6-48 hours old.
3. Hard filters: min liquidity, min volume, min trades, socials listed.
4. RugCheck API (docs: https://api.rugcheck.xyz/swagger/index.html): require mint and freeze authority revoked, liquidity locked/burned, top-holder concentration under a cap, no danger-level flags. If the check fails or is incomplete, reject.
5. Rank survivors (holder growth, sane volume/liquidity ratio). Never post the same token twice to the same chat; max posts/day per chat.
6. If nothing passes, post nothing (or a one-line "0 passed").

## Rules
- All thresholds live in config.yaml, never hardcoded. Starting values are guesses and must be tuned from real data.
- Never guess API field names. Call each API first, print a sample response, then code against it.
- Handle launchpad tokens (bonding curve before migration) explicitly, based on the real responses.
- Log every evaluated token and its rejection reason to logs/.
- Secrets only in .env. Never print or commit them.
- Wording (posts AND website): neutral facts ("Mint authority revoked"). Never use "safe", "gem", "verified", "buy", "risk/reward", or any return prediction. Every post ends with: Not financial advice. Passing the filter is not a recommendation.
- Token names/symbols are untrusted text: sanitise before publishing.
- Dry-run mode by default: print to console, only post to Telegram when --post is passed. Post to the TEST channel until the user approves switching to the main channel.

## Commands
- `python sieve.py [run] [--tiers new_launches,emerging,established]` — evaluate tiers (default all), update membership, save latest metrics. Never posts.
- `python sieve.py digest daily|weekly|alerts [--post]` — build a digest; dry run unless `--post` (TEST chat).
- `python sieve.py telegram-check` — one test line to the TEST chat.
- `python sieve.py web --public` — same site in read-only public mode: no Refresh/Save/Advanced; visitors can still preview their own filters (nothing is saved). Server returns 403 for save/run in public mode.
- Dashboard design = DexScreener style (user's choice 2026-10-08): sidebar tiers, tier-change ticker (+N more → full list), dense paged table, right panel Token (stat boxes with plain-language tooltips, ✓/✕ checklist "why it passed/failed", glossary) / Filters (Strict/Balanced/Loose presets from config `presets`, per-filter "typical values" + "removes N"). ⚠ badge when top wallets ≥ `publishing.concentration_warning_pct` (user chose warning-only for TRUMP-type tokens, no exclusion). `tests/test_dashboard_logic.py` enforces banned words on the site text.
- `python sieve.py web [--port 8765] [--no-browser]` — private dashboard on 127.0.0.1: 3 tier tabs, every evaluated token + reasons, edit filters with instant what-if preview (`whatif.py`), save to config.yaml (`config_edit.py` keeps comments, only touches changed values), run a tier, tier changes of the last 7 days. Write endpoints need header `X-Coin-Sieve: 1`. Stdlib only.
- `python sieve.py track-record` — what happened to tier entries at 24h / 72h / 7d (every `run` records due checkpoints).
- Cadence (decided 2026-10-08, implement in Stage 9): `run --tiers new_launches` every 15 min; `run --tiers emerging,established` hourly; `digest daily` once a day; `digest weekly` once a week; `digest alerts` hourly after the Jupiter tiers run.
- `python -m unittest discover tests` — unit tests (no network).

## Build order (stop after each stage for review; commit only after approval)
- Stage 1 ✅ fetch + age/liquidity filters. Stage 2 ✅ RugCheck. Stage 3 ✅ ranking, per-chat cap + dedupe, Telegram (TEST).
- Stage 4: tier model (CLI subcommands, tier membership + events, hysteresis, graduation).
- Stage 5: Emerging + Established pipelines (probe data sources first).
- Stage 6: Telegram digests (daily / weekly / ad-hoc) + name sanitiser.
- Stage 7: track record (outcome snapshots at +24h, +72h, +7d).
- Stage 8: static website (Jinja2 → `site/`), hosting chosen then.
- Stage 9: scheduling on an always-on host; switch to main channel after approval.
- Later: paid tier.

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

## Verified API facts (Jupiter Tokens v2, 2026-10-08) — source for Emerging + Established
- `https://lite-api.jup.ag/tokens/v2` (no key; `api.jup.ag` also answered without a key). No rate-limit headers seen.
- `tag?query=verified` → ~3,900 full token objects (~5 MB). `toporganicscore/24h`, `toptraded/24h` take `limit` ≤ 100. `search?query=a,b,c` → max 100 tokens per call.
- Token-level fields: `mcap`, `fdv`, `liquidity`, `holderCount`, `organicScore` (0–100), `isVerified`, `tags`, `firstPool.createdAt` (token age; missing on ~14%), `audit.{mintAuthorityDisabled, freezeAuthorityDisabled, topHoldersPercentage, devMints, devBalancePercentage}` — audit keys are OMITTED when false/unknown, `stats24h.{holderChange (%), buyVolume, sellVolume, buyOrganicVolume, sellOrganicVolume, numTraders}`.
- Not Solana-native products are tagged: `stable`, `lst`, `yield`, `yb`, `rwa`, `stocks`, `xstocks`… (tokenised stocks also end in "x"). SOL/cbBTC/WBTC excluded by mint in config.
- Holder counts: verified against the chain 2026-10-08 (getProgramAccounts, balance > 0): MINER on-chain 912 / Jupiter 915 / RugCheck 3,151; TON618 1,124 / 1,126 / 3,755; SI276 1,207 / 1,208 / 3,190. **Jupiter `holderCount` = real holders. RugCheck `totalHolders` = ALL token accounts incl. emptied ones — never use it as holders.** All tiers take holders from Jupiter; snapshots carry `source`.
- `audit.devMints` exposes serial launchers (one New Launch's creator had minted 346 tokens) — candidate New Launches signal.
- Never show the tag name "verified" in posts/site (banned word).

## Telegram (Stage 3)
- Error shape verified: `{"ok": false, "error_code": 401, "description": "Unauthorized"}`. Success verified 2026-10-08: `result.message_id` (bot @coinsieve_bot → private TEST channel "CS Test").
- Private channel id found via `getUpdates` (`channel_post.chat.id`, format `-100…`); bot must be admin, and only messages posted after that show up.
- `posts` / `zero_notices` are keyed per chat, so TEST posts never block the main channel.
- Posts are plain text (no parse_mode) so token names need no escaping. Bot token is redacted from all error strings.
- Banned-word check runs on post prose only (not addresses/URLs — base58 can contain "gem"/"buy" by chance).
- A token is postable only with ≥ `ranking.min_history_hours` of Jupiter holder snapshots, so it must pass on several runs.
- Track record (Stage 7): `outcomes` table, Jupiter-only values. Vanished tokens (`found = 0`) stay in the stats — never drop them (survivorship bias). Late/missed checkpoints are skipped, not back-filled.
- Daily/weekly posts are PNG cards (`card.py`) + short HTML captions (`digest.py`), sent via `sendPhoto` (caption ≤ 1024 visible UTF-16 units). Daily card: logo strip, screening funnel per tier (`funnel.py`, saved per run in `run_stats`), 3 tokens per tier with 48h price sparklines (GeckoTerminal OHLCV; pool address from DexScreener; 7 s spacing, 20 s backoff — its free limit is tight).
- Logos: real logos (Jupiter `icon`) only for `publishing.logo_tiers` (Emerging, Established). New Launches always get letter badges — user decision 2026-10-08, their images are unvetted. Logo fetch is https-only, size-capped, raster-only, cached in `data/logos`.
- Emoji policy: calm section markers only (📊 🆕 🌱 🏛 🗓 🎓). Never 🚀 / 💎 (hype; 💎 = "gem").
- `digest daily|weekly --preview [--post]` ignores post history and records nothing — for design iterations.
- Digests (Stage 6): `digest.py` builds texts; names/symbols go through `sanitize.clean_text` and are skipped if they hit `banned_words` or `blocked_words`. A tier's first hour of events (`bootstrap_hours`) and config exclusions (`excluded_*`) are never published.
