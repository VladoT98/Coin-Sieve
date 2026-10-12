# Product brief (v4, 2026-10-12)

Long-term crypto screener inspired by CryptoScores (cryptoscores.com). Their public pages are used for
feature and metric ideas only: never copy their text, design or branding; write everything in our own words.

## What we're building
A daily-updated quality ranking of coins above $100M market cap, scored on transparent rules,
with a Telegram channel that posts which coins ENTERED and LEFT the filtered list each day
(with the reason). Later: a website with a ranked table, coin pages, and a paid tier.

Positioning vs CryptoScores: simpler (20-40 coins on the list, one-line reason each),
cheaper, fully transparent rules, public track record. They charge $29-299/month,
use 100+ metrics and paid data. We use free data and publish our methodology.

## Data sources (free tier only)
- CoinGecko Demo API (backbone): price, market cap, volume, supply, ATH, categories,
  links, GitHub repo, contract addresses. LIMITS: 10,000 calls/month, 100/min.
  Budget calls: /coins/markets returns 250 coins per call (daily, ~2 calls).
  Per-coin /coins/{id} metadata changes slowly: refresh WEEKLY, not daily.
  Show "Data provided by CoinGecko" with a link wherever data appears.
- DefiLlama free API (no key): TVL, fees, revenue. /protocols includes each
  project's CoinGecko ID: use it to match coins.
- GitHub API (free token): commits, active contributors, stars, last commit,
  using repo links from CoinGecko.
- Compute RSI and price-based metrics ourselves from CoinGecko history.
- Skip for now: X/Twitter, LunarCrush, CertiK, token-unlock providers (all paid).

## Universe rules
Coins with market cap >= $100M, excluding stablecoins, wrapped and staked/liquid-staking
tokens (use CoinGecko categories). Master ID = CoinGecko ID.

## Scoring
- Each metric -> percentile rank within the universe (0-100).
- Group into 4-5 categories (e.g. Liquidity, Fundamentals/Revenue, Development,
  Tokenomics/Supply, Momentum), weighted into a total score.
- ALL weights, thresholds and filter rules live in one config file, not in code.
- Store the source URL for every data point.

## Daily output
1. Fetch -> score -> apply filter rules -> compare with yesterday's list.
2. Save the full daily snapshot to the database (history is a core asset; never delete it).
3. Telegram post: coins entered/left + reason, current top list.
4. Weekly: performance of the list vs BTC (public track record, wins and losses).

## Rules
- Wording: "Educational information only, not investment advice" on every output.
  Never use "buy"/"sell" language; say "passes/fails our screening criteria".
- Secrets in .env (gitignored). Windows dev machine.
- Work on a new branch; don't break what's live.
- ASK before deleting existing features, dropping/migrating data, or force-pushing.
- I know Python, SQL and Excel; I'm new to web dev. Explain new tools/concepts the first
  time, skip basic syntax. Push back if something I ask is a bad idea.
- One phase per session; at the end of each, show me how to test it, then commit.

## Decisions (interview 2026-10-12)
- Solana tiers (New Launches / Emerging / Established) and their digests: **delete** (after a DB backup and a
  final confirmation right before deletion).
- Score name: **"Quality score"** (allowed as the product term; other judgement words stay banned).
- List rule: hard filters, then **rank band** — enter at rank ≤ 30, leave when rank > 40 or a filter fails.
- Missing metrics: **re-weight** within the category; a category with too little data = neutral 50 + flagged.
- Universe keeps meme coins and exchange tokens; they are scored on the same rules.
- Momentum: **10%** of the total; RSI shown as a fact only.
- Minimum age: **365 days**.
- History backup: **GitHub Release assets** (one compressed daily snapshot per day, monthly release).
- Telegram daily post: **text + emojis + one visual**; visuals get dropped if they don't work.
- Free vs paid: list, total + category scores, entered/left, methodology, track record free; per-metric detail,
  full history, filters, alerts, CSV paid.
- Starting weights: Liquidity 25 · Fundamentals 20 · Development 20 · Tokenomics 25 · Momentum 10.
- Keys: none yet → user creates a CoinGecko Demo key and a GitHub token before Phase 1.

## Phases
0. Audit the current repo (stack, what exists, what's reusable)
1. Data pipeline: universe + CoinGecko + DefiLlama + GitHub, with call budgeting and caching
2. Scoring engine with config file
3. Daily snapshots + entered/left diff with reasons
4. Telegram channel posting + scheduled daily run
5. Website: ranked table, coin page with score breakdown and sources, methodology page
6. Track record vs BTC
7. Later: screener filters, Telegram bot lookups/alerts, paid tier, affiliate program,
   one docs page per metric
