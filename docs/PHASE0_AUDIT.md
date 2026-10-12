# Phase 0 — Repo audit and metric map (2026-10-12)

## 1. What exists today

**Stack:** Python 3.12, stdlib + requests / PyYAML / python-dotenv / Pillow / matplotlib / pypdf. SQLite
(`data/coinsieve.db`). No web framework: the dashboard is a stdlib HTTP server + one `dashboard.html`
(vanilla JS); the public site is the same page exported as static files. 133 unit tests (`tests/`, no network).

**Hosting:** GitHub Actions `site.yml` runs every 30 min (+ on every push to `main`): `run` → `profiles` →
`holders` → `run` → `history` → `summaries` → `market` → `export` → GitHub Pages
(https://vladot98.github.io/Coin-Sieve/). The DB + logo cache live on the `state` branch, which is
**overwritten by one force-pushed commit per run** → the live DB has no history/backup.

**Telegram:** bot @coinsieve_bot, only the private TEST channel "CS Test". Posting is manual
(`sieve.py digest daily|weekly|alerts --post`); nothing scheduled; no main channel yet. Daily/weekly posts are
PNG cards (`card.py`) the user rejected on 2026-10-08 (see memory).

| Area | Modules | Data sources |
|---|---|---|
| Website "Tokens" list (v3, live) | `coverage.py`, `profiles.py`, `history.py`, `holders.py`, `ai_summary.py`, `docs.py`, `facts.py` | CoinGecko keyless, DexScreener (links fallback), RugCheck + Solana RPC (holders), DeepSeek (summaries) |
| Solana tiers (Telegram only) | `tiers/new_launches.py`, `tiers/jupiter_tiers.py`, `filters.py`, `ranking.py`, `rugcheck.py`, `jupiter.py`, `dexscreener.py` | DexScreener, RugCheck, Jupiter |
| Membership + events | `membership.py`, `tiers/__init__.py`, `store.py` (`tier_members`, `tier_events`) | – |
| Track record | `track_record.py` (`outcomes`: +24h/+72h/+7d) | Jupiter prices |
| Telegram | `telegram.py`, `digest.py`, `card.py`, `funnel.py`, `sanitize.py` | – |
| Dashboard / site | `dashboard.py`, `dashboard.html`, `whatif.py`, `config_edit.py`, `site_text.yaml`, `logos.py`, `charts.py`, `market.py`, `report.py` | GeckoTerminal, Binance, Bybit, alternative.me |
| Plumbing | `http.py` (throttled client + backoff), `webfetch.py`, `evallog.py` | – |

## 2. Keep / adapt / retire

**Keep as is:** `http.py` (throttling/backoff), `sanitize.py`, `telegram.py`, `logos.py`, `webfetch.py`,
`config_edit.py`, banned-words test, `site_text.yaml` pattern, static export + GitHub Pages workflow.

**Adapt:**
- `coingecko.py` → add Demo key header + a call-budget counter; keep the verified field notes.
- `coverage.py` → becomes the **universe** step (category exclusions, category cache, mcap floor). Its
  age/volume/exchanges/website checks turn into filter rules in config.
- `profiles.py` → weekly per-coin metadata (`coins/{id}`: links, repos, tickers, categories, platforms).
- `history.py` → one-time 365-day price/mcap backfill per coin; afterwards daily closes come from our own snapshots.
- `membership.py` + `tier_members`/`tier_events` → "on the list" membership with entered/left events and a
  hysteresis band.
- `track_record.py` → list performance vs BTC (needs a new method: equal-weight, entry→exit).
- `digest.py` → new text post (entered/left + reasons + top list). `card.py` probably retired (rejected design).
- `store.py` → new tables (daily snapshots, metric values with source URLs, scores, API call log).

**Retire (needs your OK; I'd stop running them, not delete code until you say so):** Solana tiers (New
Launches / Emerging / Established), RugCheck, Jupiter, DexScreener discovery, GeckoTerminal sparklines,
Solana holders, market strip (Binance/Bybit/F&G), what-if/presets dashboard, AI summaries (DeepSeek costs money).

## 3. Risks found

1. **History has no backup.** The live DB is force-pushed as a single commit every run; one bad run loses
   everything. The brief calls history a core asset → needs an append-only backup (decide in Phase 1/3).
2. **CoinGecko budget vs the live site.** The live site calls CoinGecko keyless every 30 min (markets + up to 25
   profiles + 40 history calls per run). Moving that onto the Demo key would cost far more than 10k/month. → The
   new daily job uses the key; the live site stays keyless until Phase 5 replaces it.
3. **Wording conflict.** CLAUDE.md bans "quality words"; the brief says "quality ranking/score". Needs a decision.
4. **Terms.** Demo API attribution is required; whether it allows commercial use (paid tier) must be checked
   before Phase 7 — a paid CoinGecko plan may be needed then.
5. **Sparse metrics.** TVL/fees/revenue exist only for DeFi protocols and chains; GitHub only where a repo is
   listed. Missing-data handling changes rankings a lot (decision below).

## 4. CryptoScores — what they do (public pages, our summary)

- 6 categories, ~100 metrics: Community 21, Liquidity 14, Opportunity 21, Security 15, Technology 11, Tokenomics 21.
- Each metric compares a coin with thousands of others; per-metric docs page explains what it shows + source link;
  "clickable source for every data point"; colour bands by percentile (top 10% … bottom 10%).
- Ratios normalised by market cap (volume/mcap, TVL/mcap, followers/mcap) so big and small coins compare fairly.
- Own composite indicators (e.g. supply increase combined with price drop; a "defensive" multi-score).
- Alerts on conditions; daily digest when a matching list changes; Telegram bot; 14-day trial; 25% recurring
  affiliate commission; $29 / $99 / $299 per month.
- Heavy use of paid/closed data (LunarCrush, CertiK, DappRadar, Messari, CryptoRank, order books, WHOIS).

Takeaways for us: per-metric docs pages and source links are cheap to copy as *ideas*; mcap-normalised ratios
are the key trick; their "opportunity" metrics are timing signals with buy-side wording we must not use.

## 5. Metrics we can compute from free sources

✓ = field already verified in this repo; ? = probe in Phase 1 before coding (rule: never guess field names).
"Own history" = needs our daily snapshots (or the one-time 365-day backfill).

### Liquidity / market activity
| Metric | Source | Status |
|---|---|---|
| 24h volume / market cap | CG markets | ✓ |
| 7d / 30d average volume / mcap (smoother than 24h) | own history | backfill |
| Number of markets, distinct exchanges | CG `coins/{id}` tickers | ✓ |
| Listed on major spot exchanges (count of Binance, Coinbase, Kraken, OKX, Bybit, Upbit) | CG tickers | ✓ (ids ?) |
| Bid-ask spread, ±2% order-book depth / mcap | CG tickers (`depth=true`) | ? |
| Perpetual futures listed; open interest / mcap | CG `derivatives` (1 call, all coins) | ? |

### Fundamentals / revenue (DefiLlama, DeFi + chains only)
| Metric | Source | Status |
|---|---|---|
| TVL, TVL / mcap | `/protocols` (`gecko_id`), `/v2/chains` (`gecko_id`) | ? |
| TVL change 30d | `/protocols` change fields or own history | ? |
| Fees 30d / mcap, revenue 30d / mcap | `/overview/fees` (+ `dataType=dailyRevenue`) | ? matching via parent protocol |
| Fees 7d vs 30d (trend) | same | ? |

### Development (GitHub, where CoinGecko lists a repo)
| Metric | Source | Status |
|---|---|---|
| Commits last 90d / 1y, weeks with commits (of 52) | `/repos/{o}/{r}/stats/commit_activity` | ? (async 202) |
| Active contributors 90d | `/stats/contributors` | ? |
| Days since last commit, stars, forks, repo age | `/repos/{o}/{r}` | ? |
| (Cheaper alt) CoinGecko `developer_data` | `coins/{id}` | ? may be stale |

### Tokenomics / supply
| Metric | Source | Status |
|---|---|---|
| Circulating / max (or total) supply % | CG markets | ✓ |
| FDV / market cap | CG markets | ✓ |
| Supply change 30d / 1y (mcap ÷ price over time) | backfill + own history | backfill |
| Has a max supply | CG markets | ✓ |
| Market cap now vs market cap at ATH | backfill (365d max only) | partial |

### Momentum / price (shown as facts, never "oversold = chance")
| Metric | Source | Status |
|---|---|---|
| Price change 7d / 30d / 90d, and vs BTC | CG markets + own history | ✓ / backfill |
| RSI-14 (daily closes) | own history | backfill |
| Distance from ATH | CG markets `ath_change_percentage` | ? |
| 90d volatility, 1y max drawdown | own history | backfill |

### Maturity / interest (community stand-ins without X/Twitter)
| Metric | Source | Status |
|---|---|---|
| Age (earliest price / genesis) | CG + backfill | ✓ |
| Chains deployed on | CG `platforms` | ✓ |
| CoinGecko watchlist users, change 30d | CG `coins/{id}` `watchlist_portfolio_users` | ? |
| Website / docs / repo present | CG links | ✓ |

### Flags (not scored, shown as neutral notes)
Supply up ≥ X% in 7d; TVL down ≥ X% in 30d; no commits for 180d; volume/mcap outlier.

### Skipped (paid / no free source)
X followers, LunarCrush, CertiK/audits, hack history (DefiLlama hacks endpoint — probe), unlock schedules,
whale/holder behaviour across chains, order-book data beyond CoinGecko tickers.

## 6. CoinGecko Demo budget (estimate, ~300 coins ≥ $100M before exclusions)

| Call | Frequency | Calls / month |
|---|---|---|
| `coins/markets` (2 pages) | daily | ~60 |
| Exclusion category lists (7) | weekly | ~30 |
| `coins/{id}?tickers=true` per coin | weekly | ~1,300 |
| 365d `market_chart` backfill | once per coin + new entrants | ~300 once, then ~20 |
| `derivatives` | daily | ~30 |
| **Total** | | **~1,450 / month** of 10,000 |

Headroom (~8,500) is left for coin pages, retries and a later hourly price refresh.
