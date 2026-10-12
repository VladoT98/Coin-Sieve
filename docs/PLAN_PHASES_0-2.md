# Plan — Phases 0–2 (draft for approval, 2026-10-12)

Branch: `quality-score`. `main` (the live v3 site) is not touched until a later merge. Decisions: `PRODUCT.md`.

## Architecture choice
New database file **`data/screener.db`** for v4, separate from the live `coinsieve.db`.
- Why: no migration of live data, the v3 site keeps working, backups are one clean file.
- Alternative: new tables in `coinsieve.db`. One file, but every v4 change risks the live site.

Scheduled GitHub workflows only run from `main`, so v4 runs locally (manually) until Phase 4 merges its
daily workflow next to `site.yml`.

---

## Phase 0 — Audit (this session)
- [x] `docs/PRODUCT.md` (brief + decisions), CLAUDE.md summary, `docs/PHASE0_AUDIT.md` (audit + metric map).
- [ ] You review → commit the docs on `quality-score`.

---

## Phase 1 — Data pipeline (1 session, large)

**Before you start:** create a CoinGecko Demo key and a GitHub fine-grained token (public repos, read-only) and
paste them into `.env` as `COINGECKO_DEMO_KEY=` and `GITHUB_TOKEN=` (I add the placeholders to `.env.example`).
In CI the built-in Actions token is enough for GitHub (1,000 calls/hour).

### 1.1 Backup ✅ (2026-10-12)
1. Live DB from the `state` branch → `data/backup/coinsieve-state-2026-10-12.db` (+ old local DB); tag `v3-final`.
2. Deleting the Solana tiers moved to **Phase 5** (user decision): the live dashboard imports them (what-if,
   funnel, digest, holders → RugCheck/Solana RPC). v4 code never imports them.

### Status 2026-10-12: 1.2–1.7 built (`coinsieve/screener/`, `tests/test_screener.py`). Changes vs. the plan:
- DefiLlama needs `/lite/protocols2`: big protocols (Uniswap, Chainlink, Hyperliquid…) have their CoinGecko id
  only on the parent entry. `/protocols` is still used for page slugs.
- CoinGecko `developer_data` is null → GitHub is the only development source. Derivatives are keyed by symbol
  only → not fetched (optional later).
- Extra: major-exchange volume (small exchanges report inflated volume), TVL change 30d (lite list).
- GitHub Release upload of backups moves to Phase 3/4 (CI token); `sieve.py backup` writes the file locally now.

### 1.2 Probes first (CLAUDE.md rule: never guess field names)
`scripts/probe_v4.py` calls each endpoint once and saves a trimmed sample to `docs/api_samples/`; the
verified fields go into CLAUDE.md "Verified API facts (v4)".
- CoinGecko with the Demo key: `coins/markets`, `coins/{id}` (tickers with `depth=true`, `developer_data`,
  `watchlist_portfolio_users`), `derivatives`, `coins/{id}/market_chart?days=365`, rate-limit headers.
- DefiLlama: `/protocols` (`gecko_id`, `parentProtocol`), `/v2/chains`, `/overview/fees` (fees and revenue).
- GitHub: `/repos/{o}/{r}`, `/stats/commit_activity`, `/stats/contributors` (the "202 = still computing" case),
  `/orgs/{o}/repos`, rate-limit headers.

### 1.3 Call budget and cache (`coinsieve/budget.py`)
- Table `api_calls(ts, provider, endpoint, status, ms)`: every request is logged.
- Config `budget.coingecko_monthly: 10000`, `budget.reserve_pct: 20`. Before a non-essential call the client
  checks the month-to-date count + the planned calls. When the cap would be crossed, it skips the call and
  logs it. Daily `coins/markets` is essential.
- Cache table keyed by URL with a TTL per endpoint from config (markets 1 day, `coins/{id}` 7 days,
  DefiLlama 1 day, GitHub 7 days) → re-running the same day costs 0 calls.
- `python sieve.py budget` prints calls this month per provider and the projection.

### 1.4 Universe (`coinsieve/universe.py`, reuses v3 coverage logic)
- `coins/markets` pages until market cap < $90M.
- Enter at ≥ $100M, stay until < $90M. A coin near the edge doesn't flap in and out.
- Exclude by CoinGecko category lists (weekly) + `exclude_ids`. Store each coin's in/out reason per day.

### 1.5 Fetchers
| Fetcher | Frequency | How |
|---|---|---|
| CoinGecko markets | daily | 2 calls → price, mcap, volume, supplies, ATH, 7d % |
| CoinGecko `coins/{id}` | weekly, 1/7 of the universe per day | links, repos, tickers, categories, platforms |
| CoinGecko history | once per coin (365d), then from our own daily rows | backfill price / mcap / volume |
| CoinGecko derivatives | daily | 1 call → open interest per coin (if the probe confirms the coin link) |
| DefiLlama | daily | `/protocols` + `/v2/chains` + fees + revenue = 4 calls; matched by `gecko_id`, sub-protocols rolled up to the parent |
| GitHub | weekly rotation | repos from CoinGecko; an org link → its 5 most recently pushed non-fork repos, summed |

### 1.6 Storage (`data/screener.db`, long format, SQL- and Excel-friendly)
- `coins(cg_id, symbol, name, first_seen)`
- `universe_daily(date, cg_id, included, reason)`
- `price_daily(cg_id, date, price, mcap, volume, source)`: backfill + daily
- `metric_values(date, cg_id, metric_id, value, source_url, fetched_at)`: one row per data point, **source
  URL required**. ~250 coins × ~35 metrics ≈ 9k rows/day.
- `links(cg_id, kind, url)` (GitHub repos, DefiLlama slugs), `api_calls`, `http_cache`
- Daily backup: compress the DB → GitHub Release `history-2026-10` asset (automated in Phase 3/4; manual
  command `sieve.py backup` in Phase 1).

### 1.7 Commands and tests
- `python sieve.py fetch` (all due fetches), `python sieve.py backfill [--max N]`, `python sieve.py budget`.
- Offline tests from the saved samples: budget cap, cache TTL, universe edges + exclusions, DefiLlama parent
  roll-up, GitHub org → repos aggregation.

### How you'll test Phase 1
1. `python sieve.py fetch` twice: the second run makes ~0 CoinGecko calls (cache).
2. `python sieve.py budget` shows the counts.
3. Open `data/screener.db` in DB Browser for SQLite (free GUI), export 5 coins' metrics to CSV, and compare them
   with their CoinGecko / DefiLlama / GitHub pages by clicking the stored source URLs.

---

## Phase 2 — Scoring engine (1 session)

### 2.1 Config (`config.yaml` → `scoring:`, the single place for weights and rules)
```yaml
scoring:
  categories: {liquidity: 25, fundamentals: 20, development: 20, tokenomics: 25, momentum: 10}
  min_metrics_per_category: 2        # fewer -> category = 50, flagged "not enough data"
  min_coins_per_metric: 30           # metric skipped for everyone if fewer coins have it
  metrics:
    volume_mcap_30d: {category: liquidity, weight: 1, direction: higher, winsor_pct: 95}
    fdv_mcap:        {category: tokenomics, weight: 1, direction: lower}
    # ... one line per metric from the audit's metric map
  filters:                           # hard rules before ranking
    min_age_days: 365
    min_volume_h24_usd: 500000
    min_exchanges: 3
    require_website_or_docs: true
    min_data_completeness_pct: 60
  list: {enter_rank: 30, leave_rank: 40}
```
`winsor_pct` caps extreme values (e.g. wash-traded volume) so one outlier doesn't dominate.

### 2.2 Math (`coinsieve/scoring.py`, pure functions, no I/O)
1. **Metric percentile:** rank among coins that have the value (ties = average rank), scaled 0–100;
   `direction: lower` flips it.
2. **Category score:** weighted mean of the coin's available metric percentiles (re-weight). Below
   `min_metrics_per_category` → 50 + flag.
3. **Quality score:** weighted sum of the categories (weights must add to 100; config check fails otherwise).
4. Note for the methodology page: scores are relative. A coin's score can move when others change, even if
   its own data didn't.

### 2.3 Output
- `scores_daily(date, cg_id, category, score, metrics_used, metrics_total)` + `total`.
- `python sieve.py score [--date D]` → ranked table in the console with filter pass/fail.
- `python sieve.py score --explain bitcoin` → every metric: value, percentile, weight, source URL.
- `python sieve.py score --csv out.csv` → for checking in Excel.
- `python sieve.py score --sensitivity` → how many top-30 coins change when each category weight moves ±5.

### 2.4 Wording updates
"Quality score" becomes an allowed term (banned-words test updated); other judgement words stay banned.
The disclaimer becomes "Educational information only, not investment advice."

### 2.5 Tests
Percentile math (ties, direction, winsor), re-weighting, the neutral-50 fallback, config validation
(weights = 100, every metric has a category and source), and one end-to-end check on a fixture universe.

### How you'll test Phase 2
1. `python sieve.py score` → check where well-known coins land (BTC, ETH, a DeFi coin, a meme coin).
2. `--explain` on 2–3 coins you know; click the source URLs.
3. `--csv` into Excel; change a weight in config, re-run and see the ranking move.

---

## Open items (not blocking Phase 1)
- CoinGecko Demo terms for commercial use: check before the paid tier (Phase 7).
- Telegram visual for the daily post: design in Phase 4. Emoji policy stays (calm markers, no 🚀 / 💎).
- Dropping the old Solana tables from the live DB: ask at the Phase 5 merge.
