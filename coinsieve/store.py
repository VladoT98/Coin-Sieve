"""SQLite store: discovered tokens, holder snapshots, tiers, latest metrics, post history."""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    address      TEXT PRIMARY KEY,
    first_seen   REAL NOT NULL,              -- unix seconds
    sources      TEXT NOT NULL,              -- comma-separated feed names
    launch_ts    REAL,                       -- earliest pairCreatedAt, unix seconds
    status       TEXT NOT NULL DEFAULT 'active',  -- active | too_old
    last_checked REAL
);
-- Holder snapshots for New Launches holder growth. `source` = where `holders` came from:
-- rows before 2026-10-08 have NULL (RugCheck totalHolders, which counts emptied accounts) and are ignored.
CREATE TABLE IF NOT EXISTS snapshots (
    address        TEXT NOT NULL,
    ts             REAL NOT NULL,
    holders        INTEGER,
    liquidity_usd  REAL,
    volume_h24_usd REAL,
    source         TEXT,
    PRIMARY KEY (address, ts)
);
-- Latest evaluation per token per tier (JSON), read by digests and the website.
CREATE TABLE IF NOT EXISTS latest_metrics (
    address TEXT NOT NULL,
    tier    TEXT NOT NULL,
    ts      REAL NOT NULL,
    metrics TEXT NOT NULL,
    reasons TEXT NOT NULL,
    PRIMARY KEY (address, tier)
);
-- One row per sent digest: prevents sending the same daily/weekly digest twice to a chat.
CREATE TABLE IF NOT EXISTS digests (
    kind       TEXT NOT NULL,                -- daily | weekly | alerts
    period     TEXT NOT NULL,                -- UTC date, ISO week, or alert timestamp
    chat       TEXT NOT NULL,
    sent_at    REAL NOT NULL,
    message_id INTEGER,
    PRIMARY KEY (kind, period, chat)
);
-- Track record: what happened to each tier entry at fixed checkpoints after it entered.
-- All values from Jupiter (one source). found = 0 means Jupiter no longer lists the token.
CREATE TABLE IF NOT EXISTS outcomes (
    address       TEXT NOT NULL,
    tier          TEXT NOT NULL,
    entered_at    REAL NOT NULL,
    checkpoint    TEXT NOT NULL,             -- e.g. 0h (baseline), 24h, 72h, 7d
    ts            REAL NOT NULL,             -- when the snapshot was actually taken
    found         INTEGER NOT NULL,
    price_usd     REAL,
    mcap_usd      REAL,
    liquidity_usd REAL,
    holders       INTEGER,
    tiers_now     TEXT,                      -- tiers the token is a member of at snapshot time
    PRIMARY KEY (address, tier, entered_at, checkpoint)
);
-- Funnel numbers per tier per run (JSON from funnel.stats), shown on the daily card.
CREATE TABLE IF NOT EXISTS run_stats (
    ts    REAL NOT NULL,
    tier  TEXT NOT NULL,
    stats TEXT NOT NULL,
    PRIMARY KEY (ts, tier)
);
-- Emerging/Established tokens featured on a daily card (rotation: not repeated within a cooldown).
CREATE TABLE IF NOT EXISTS featured (
    address    TEXT NOT NULL,
    tier       TEXT NOT NULL,
    chat       TEXT NOT NULL,
    ts         REAL NOT NULL,
    message_id INTEGER,
    PRIMARY KEY (address, tier, chat, ts)
);
CREATE TABLE IF NOT EXISTS posted_events (
    event_id INTEGER NOT NULL,
    chat     TEXT NOT NULL,
    sent_at  REAL NOT NULL,
    PRIMARY KEY (event_id, chat)
);
-- Dedupe + daily cap are per chat, so TEST-channel posts never block the main channel.
CREATE TABLE IF NOT EXISTS posts (
    address    TEXT NOT NULL,                -- never post the same token twice to a chat
    chat       TEXT NOT NULL,
    posted_at  REAL NOT NULL,
    message_id INTEGER,
    PRIMARY KEY (address, chat)
);
-- Tier membership history. A row with left_at NULL is a current member; re-entry adds a new row.
CREATE TABLE IF NOT EXISTS tier_members (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    address      TEXT NOT NULL,
    tier         TEXT NOT NULL,
    symbol       TEXT,
    entered_at   REAL NOT NULL,
    last_pass_at REAL NOT NULL,
    left_at      REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tier_members_current
    ON tier_members (address, tier) WHERE left_at IS NULL;
CREATE TABLE IF NOT EXISTS tier_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    address TEXT NOT NULL,
    tier    TEXT NOT NULL,
    kind    TEXT NOT NULL,                   -- entered | left | graduated
    symbol  TEXT,
    detail  TEXT
);
CREATE TABLE IF NOT EXISTS zero_notices (
    day       TEXT NOT NULL,                 -- UTC date; at most one "0 passed" per day per chat
    chat      TEXT NOT NULL,
    posted_at REAL NOT NULL,
    PRIMARY KEY (day, chat)
);
-- Dashboard price sparklines: hourly closes from GeckoTerminal for the token's highest-liquidity pool.
-- A failed fetch only sets attempted_at/error; points and fetched_at keep the last good series.
CREATE TABLE IF NOT EXISTS chart_series (
    address          TEXT PRIMARY KEY,
    pool             TEXT,
    pool_resolved_at REAL,
    hours            INTEGER,                -- history window that was requested
    points           TEXT,                   -- JSON [[ts, close], ...] oldest first; NULL = never fetched
    fetched_at       REAL,                   -- last successful fetch
    attempted_at     REAL,                   -- last attempt (success or not)
    error            TEXT                    -- last failure, NULL after a success
);
-- Dashboard market strip (market.py): one JSON snapshot per refresh, kept `market.keep_days`.
CREATE TABLE IF NOT EXISTS market_snapshots (
    ts   REAL PRIMARY KEY,
    data TEXT NOT NULL
);
-- Rows the dashboard showed recently; the chart fetcher serves these first.
CREATE TABLE IF NOT EXISTS chart_demand (
    address TEXT PRIMARY KEY,
    ts      REAL NOT NULL
);
-- v3 coverage profiles (profiles.py): links found, CoinGecko platform/categories, supply, logo URL. JSON per token.
CREATE TABLE IF NOT EXISTS token_profiles (
    chain      TEXT NOT NULL,
    address    TEXT NOT NULL,
    data       TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (chain, address)
);
-- Latest top-holder analysis per token (holders.py): RugCheck top 20 + owner programs from Solana RPC.
CREATE TABLE IF NOT EXISTS holder_reports (
    chain          TEXT NOT NULL,
    address        TEXT NOT NULL,
    ts             REAL NOT NULL,
    data           TEXT NOT NULL,
    top10_all_pct  REAL,
    top10_excl_pct REAL,
    PRIMARY KEY (chain, address)
);
"""

# Columns added after the first release. Non-destructive: old rows get the default.
_ADDED_COLUMNS = [("snapshots", "source", "TEXT")] + [
    (t, "chain", "TEXT NOT NULL DEFAULT 'solana'")
    for t in ("tier_members", "tier_events", "latest_metrics", "snapshots", "chart_series", "outcomes")]

# Stage 3 first draft keyed these tables without `chat`. Recreate only if empty.
_PER_CHAT_TABLES = ("posts", "zero_notices")


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._migrate_per_chat()
        self.db.executescript(SCHEMA)
        for table, col, decl in _ADDED_COLUMNS:
            if col not in [r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")]:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def _migrate_per_chat(self):
        for table in _PER_CHAT_TABLES:
            pk = [r["name"] for r in self.db.execute(f"PRAGMA table_info({table})") if r["pk"]]
            if not pk or "chat" in pk:
                continue  # table absent or already per-chat
            if self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                raise RuntimeError(f"{table} has old schema and contains rows; migrate manually")
            self.db.execute(f"DROP TABLE {table}")

    def add_seen(self, address, source, now):
        """Insert a token or append the source. Returns True if the token is new."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO tokens (address, first_seen, sources) VALUES (?, ?, ?)",
            (address, now, source),
        )
        if cur.rowcount == 0:
            self.db.execute(
                "UPDATE tokens SET sources = sources || ',' || ? "
                "WHERE address = ? AND instr(',' || sources || ',', ',' || ? || ',') = 0",
                (source, address, source),
            )
        return cur.rowcount == 1

    def active(self):
        return self.db.execute("SELECT * FROM tokens WHERE status = 'active'").fetchall()

    def set_launch(self, address, launch_ts):
        self.db.execute("UPDATE tokens SET launch_ts = ? WHERE address = ?", (launch_ts, address))

    def set_status(self, address, status):
        self.db.execute("UPDATE tokens SET status = ? WHERE address = ?", (status, address))

    def mark_checked(self, address, now):
        self.db.execute("UPDATE tokens SET last_checked = ? WHERE address = ?", (now, address))

    # --- snapshots ---
    def add_snapshot(self, address, now, holders, liquidity_usd, volume_h24_usd, source):
        self.db.execute(
            "INSERT OR REPLACE INTO snapshots (address, ts, holders, liquidity_usd, volume_h24_usd, source) "
            "VALUES (?, ?, ?, ?, ?, ?)", (address, now, holders, liquidity_usd, volume_h24_usd, source),
        )

    def snapshots_since(self, address, since, source):
        return self.db.execute(
            "SELECT ts, holders FROM snapshots WHERE address = ? AND ts >= ? AND holders IS NOT NULL "
            "AND source = ? ORDER BY ts", (address, since, source),
        ).fetchall()

    # --- latest metrics ---
    def save_latest(self, tier, results, now):
        self.db.executemany(
            "INSERT OR REPLACE INTO latest_metrics (address, tier, ts, metrics, reasons) VALUES (?, ?, ?, ?, ?)",
            [(m["address"], tier, now, json.dumps(m), json.dumps(r)) for m, r in results],
        )

    def latest(self, tier, addresses):
        if not addresses:
            return {}
        q = ",".join("?" * len(addresses))
        rows = self.db.execute(f"SELECT * FROM latest_metrics WHERE tier = ? AND address IN ({q})",
                               [tier, *addresses])
        return {r["address"]: {"ts": r["ts"], "metrics": json.loads(r["metrics"]),
                               "reasons": json.loads(r["reasons"])} for r in rows}

    # --- posts / digests ---
    def posted_to(self, chat):
        return {r[0] for r in self.db.execute("SELECT address FROM posts WHERE chat = ?", (chat,))}

    def add_post(self, address, now, chat, message_id):
        self.db.execute("INSERT OR IGNORE INTO posts (address, chat, posted_at, message_id) VALUES (?, ?, ?, ?)",
                        (address, chat, now, message_id))

    def digest_sent(self, kind, period, chat):
        return self.db.execute("SELECT 1 FROM digests WHERE kind = ? AND period = ? AND chat = ?",
                               (kind, period, chat)).fetchone() is not None

    def add_digest(self, kind, period, chat, now, message_id):
        self.db.execute("INSERT INTO digests VALUES (?, ?, ?, ?, ?)", (kind, period, chat, now, message_id))

    def save_run_stats(self, tier, stats, now):
        self.db.execute("INSERT OR REPLACE INTO run_stats VALUES (?, ?, ?)", (now, tier, json.dumps(stats)))

    def latest_run_stats(self, tier):
        r = self.db.execute("SELECT stats FROM run_stats WHERE tier = ? ORDER BY ts DESC LIMIT 1", (tier,)).fetchone()
        return json.loads(r[0]) if r else None

    def latest_run_ts(self, tier):
        return self.db.execute("SELECT MAX(ts) FROM run_stats WHERE tier = ?", (tier,)).fetchone()[0]

    def latest_since(self, tier, since):
        """Every token evaluated for `tier` at or after `since` (i.e. in the latest run)."""
        rows = self.db.execute("SELECT * FROM latest_metrics WHERE tier = ? AND ts >= ?", (tier, since))
        return [{"ts": r["ts"], "metrics": json.loads(r["metrics"]), "reasons": json.loads(r["reasons"])}
                for r in rows]

    def tokens_seen_since(self, since):
        return self.db.execute("SELECT COUNT(*) FROM tokens WHERE first_seen >= ?", (since,)).fetchone()[0]

    def featured_since(self, tier, chat, since):
        return {r[0] for r in self.db.execute(
            "SELECT address FROM featured WHERE tier = ? AND chat = ? AND ts >= ?", (tier, chat, since))}

    def add_featured(self, address, tier, chat, now, message_id):
        self.db.execute("INSERT OR IGNORE INTO featured VALUES (?, ?, ?, ?, ?)",
                        (address, tier, chat, now, message_id))

    def posted_event_ids(self, chat):
        return {r[0] for r in self.db.execute("SELECT event_id FROM posted_events WHERE chat = ?", (chat,))}

    def add_posted_events(self, event_ids, chat, now):
        self.db.executemany("INSERT OR IGNORE INTO posted_events VALUES (?, ?, ?)",
                            [(i, chat, now) for i in event_ids])

    # --- tiers ---
    def current_members(self, tier):
        return {r["address"]: r for r in self.db.execute(
            "SELECT * FROM tier_members WHERE tier = ? AND left_at IS NULL", (tier,))}

    def all_tracked(self):
        """Every address that has ever been a member of any tier."""
        return [r[0] for r in self.db.execute("SELECT DISTINCT address FROM tier_members")]

    def was_ever_member(self, address, tier):
        return self.db.execute("SELECT 1 FROM tier_members WHERE address = ? AND tier = ?",
                               (address, tier)).fetchone() is not None

    def add_member(self, address, tier, symbol, now):
        self.db.execute("INSERT INTO tier_members (address, tier, symbol, entered_at, last_pass_at) "
                        "VALUES (?, ?, ?, ?, ?)", (address, tier, symbol, now, now))

    def touch_member(self, member_id, symbol, now):
        self.db.execute("UPDATE tier_members SET last_pass_at = ?, symbol = COALESCE(?, symbol) WHERE id = ?",
                        (now, symbol, member_id))

    def close_member(self, member_id, now):
        self.db.execute("UPDATE tier_members SET left_at = ? WHERE id = ?", (now, member_id))

    def add_event(self, now, address, tier, kind, symbol, detail=None):
        self.db.execute("INSERT INTO tier_events (ts, address, tier, kind, symbol, detail) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (now, address, tier, kind, symbol, detail))

    def events_since(self, since, tier=None):
        sql, args = "SELECT * FROM tier_events WHERE ts >= ?", [since]
        if tier:
            sql, args = sql + " AND tier = ?", args + [tier]
        return self.db.execute(sql + " ORDER BY ts, id", args).fetchall()

    # --- track record ---
    def tier_entries(self, since):
        return self.db.execute("SELECT address, tier, symbol, entered_at FROM tier_members WHERE entered_at >= ?",
                               (since,)).fetchall()

    def recorded_checkpoints(self):
        return {(r[0], r[1], r[2], r[3]) for r in self.db.execute(
            "SELECT address, tier, entered_at, checkpoint FROM outcomes")}

    def add_outcome(self, row):
        self.db.execute("INSERT OR IGNORE INTO outcomes (address, tier, entered_at, checkpoint, ts, found, price_usd, "
                        "mcap_usd, liquidity_usd, holders, tiers_now) VALUES (:address, :tier, :entered_at, "
                        ":checkpoint, :ts, :found, :price_usd, :mcap_usd, :liquidity_usd, :holders, :tiers_now)", row)

    def outcomes(self, tier):
        return [dict(r) for r in self.db.execute("SELECT * FROM outcomes WHERE tier = ?", (tier,))]

    def tiers_of(self, address):
        return sorted(r[0] for r in self.db.execute(
            "SELECT tier FROM tier_members WHERE address = ? AND left_at IS NULL", (address,)))

    def tier_first_event_ts(self, tier):
        """When a tier was first populated; events right after it are the initial bootstrap."""
        return self.db.execute("SELECT MIN(ts) FROM tier_events WHERE tier = ?", (tier,)).fetchone()[0]

    # --- chart cache ---
    def chart_rows(self, addresses=None):
        """{address: row dict} from chart_series (all rows, or only the given addresses)."""
        if addresses is None:
            rows = self.db.execute("SELECT * FROM chart_series")
        else:
            addresses = list(addresses)
            if not addresses:
                return {}
            rows = self.db.execute(f"SELECT * FROM chart_series WHERE address IN ({','.join('?' * len(addresses))})",
                                   addresses)
        return {r["address"]: dict(r) for r in rows}

    def set_chart_pool(self, address, pool, now):
        self.db.execute("INSERT INTO chart_series (address, pool, pool_resolved_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(address) DO UPDATE SET pool = excluded.pool, pool_resolved_at = excluded.pool_resolved_at",
                        (address, pool, now))

    def save_chart_points(self, address, hours, points, now):
        self.db.execute("INSERT INTO chart_series (address, hours, points, fetched_at, attempted_at, error) "
                        "VALUES (?, ?, ?, ?, ?, NULL) ON CONFLICT(address) DO UPDATE SET hours = excluded.hours, "
                        "points = excluded.points, fetched_at = excluded.fetched_at, "
                        "attempted_at = excluded.attempted_at, error = NULL",
                        (address, hours, json.dumps(points), now, now))

    def chart_failed(self, address, error, now):
        """Record a failed attempt; the last good series is kept."""
        self.db.execute("INSERT INTO chart_series (address, attempted_at, error) VALUES (?, ?, ?) "
                        "ON CONFLICT(address) DO UPDATE SET attempted_at = excluded.attempted_at, error = excluded.error",
                        (address, now, str(error)[:300]))

    def add_chart_demand(self, addresses, now):
        self.db.executemany("INSERT OR REPLACE INTO chart_demand (address, ts) VALUES (?, ?)",
                            [(a, now) for a in addresses])

    def chart_demand_since(self, since):
        return {r[0] for r in self.db.execute("SELECT address FROM chart_demand WHERE ts >= ?", (since,))}

    # --- market strip ---
    def add_market_snapshot(self, data, now, keep_days):
        self.db.execute("INSERT OR REPLACE INTO market_snapshots (ts, data) VALUES (?, ?)", (now, json.dumps(data)))
        self.db.execute("DELETE FROM market_snapshots WHERE ts < ?", (now - keep_days * 86400,))

    def market_snapshots(self, since=0):
        """[{ts, data}] oldest first."""
        return [{"ts": r[0], "data": json.loads(r[1])}
                for r in self.db.execute("SELECT ts, data FROM market_snapshots WHERE ts >= ? ORDER BY ts", (since,))]

    def last_snapshot_ts(self, address, source):
        return self.db.execute("SELECT MAX(ts) FROM snapshots WHERE address = ? AND source = ?",
                               (address, source)).fetchone()[0]

    # --- v3 profiles / holders (keyed by chain + address) ---
    def _by_address(self, table, chain, addresses):
        addresses = list(addresses)
        if not addresses:
            return {}
        q = ",".join("?" * len(addresses))
        rows = self.db.execute(f"SELECT * FROM {table} WHERE chain = ? AND address IN ({q})", [chain, *addresses])
        return {r["address"]: {**dict(r), "data": json.loads(r["data"])} for r in rows}

    def profiles(self, chain, addresses):
        """{address: {data, updated_at, ...}}"""
        return self._by_address("token_profiles", chain, addresses)

    def save_profile(self, chain, address, data, now):
        self.db.execute("INSERT OR REPLACE INTO token_profiles (chain, address, data, updated_at) VALUES (?, ?, ?, ?)",
                        (chain, address, json.dumps(data), now))

    def holder_reports(self, chain, addresses):
        return self._by_address("holder_reports", chain, addresses)

    def save_holder_report(self, chain, address, data, now):
        self.db.execute("INSERT OR REPLACE INTO holder_reports (chain, address, ts, data, top10_all_pct, top10_excl_pct) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (chain, address, now, json.dumps(data), data.get("top10_all_pct"), data.get("top10_excl_pct")))

    def max_ts(self, table, column):
        """Latest fetch time recorded in a table (for 'last updated' labels)."""
        return self.db.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()
