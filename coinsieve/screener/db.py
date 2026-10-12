"""SQLite store for the v4 screener (`data/screener.db`). History is a core asset: nothing here deletes rows except
replacing the same (date, coin, metric) observation when a source is fetched twice on one day."""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
    cg_id TEXT PRIMARY KEY, symbol TEXT, name TEXT, first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS universe_daily (
    date TEXT, cg_id TEXT, included INTEGER, reason TEXT, rank INTEGER, mcap REAL,
    PRIMARY KEY (date, cg_id));
CREATE TABLE IF NOT EXISTS price_daily (
    cg_id TEXT, date TEXT, price REAL, mcap REAL, volume REAL, source TEXT,
    PRIMARY KEY (cg_id, date));
CREATE TABLE IF NOT EXISTS metric_values (
    date TEXT, cg_id TEXT, metric TEXT, value REAL, text TEXT, source_url TEXT NOT NULL, fetched_at REAL,
    PRIMARY KEY (date, cg_id, metric));
CREATE INDEX IF NOT EXISTS metric_values_coin ON metric_values (cg_id, metric, date);
CREATE TABLE IF NOT EXISTS links (
    cg_id TEXT, kind TEXT, url TEXT, updated_at REAL, PRIMARY KEY (cg_id, kind, url));
CREATE TABLE IF NOT EXISTS fetch_log (
    source TEXT, key TEXT, fetched_at REAL, status TEXT, PRIMARY KEY (source, key));
CREATE TABLE IF NOT EXISTS api_calls (
    ts REAL, provider TEXT, path TEXT, status INTEGER, ms INTEGER);
CREATE INDEX IF NOT EXISTS api_calls_month ON api_calls (provider, ts);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS scores_daily (
    date TEXT, cg_id TEXT, total REAL, rank INTEGER, rank_all INTEGER, passes INTEGER, fail_reasons TEXT,
    categories TEXT, config_hash TEXT, PRIMARY KEY (date, cg_id));
CREATE TABLE IF NOT EXISTS metric_scores (
    date TEXT, cg_id TEXT, metric TEXT, value REAL, percentile REAL, source_url TEXT,
    PRIMARY KEY (date, cg_id, metric));
"""


class ScreenerDB:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- API call log (budgets) ----
    def log_call(self, ts, provider, path, status, ms):
        self.conn.execute("INSERT INTO api_calls VALUES (?,?,?,?,?)", (ts, provider, path, status, ms))
        self.conn.commit()   # committed at once: a crash must not lose the count

    def calls_since(self, provider, since):
        return self.conn.execute("SELECT COUNT(*) FROM api_calls WHERE provider = ? AND ts >= ?",
                                 (provider, since)).fetchone()[0]

    def calls_by_provider(self, since):
        return dict(self.conn.execute("SELECT provider, COUNT(*) FROM api_calls WHERE ts >= ? GROUP BY provider",
                                      (since,)).fetchall())

    # ---- coins / universe / prices ----
    def upsert_coin(self, cg_id, symbol, name, date):
        self.conn.execute(
            "INSERT INTO coins VALUES (?,?,?,?,?) ON CONFLICT (cg_id) DO UPDATE SET symbol = excluded.symbol, "
            "name = excluded.name, last_seen = excluded.last_seen", (cg_id, symbol, name, date, date))

    def save_universe(self, date, rows):
        self.conn.executemany(
            "INSERT OR REPLACE INTO universe_daily VALUES (?,?,?,?,?,?)",
            [(date, r["cg_id"], int(r["included"]), r["reason"], r.get("rank"), r.get("mcap")) for r in rows])

    def included_on(self, date):
        return {r[0] for r in self.conn.execute(
            "SELECT cg_id FROM universe_daily WHERE date = ? AND included = 1", (date,))}

    def last_universe_date(self, before):
        row = self.conn.execute("SELECT MAX(date) FROM universe_daily WHERE date < ?", (before,)).fetchone()
        return row[0]

    def save_prices(self, rows):
        """rows: (cg_id, date, price, mcap, volume, source). A backfill never overwrites a daily 'markets' row."""
        for r in rows:
            if r[5] == "backfill":
                self.conn.execute("INSERT OR IGNORE INTO price_daily VALUES (?,?,?,?,?,?)", r)
            else:
                self.conn.execute("INSERT OR REPLACE INTO price_daily VALUES (?,?,?,?,?,?)", r)

    def price_days(self, cg_id):
        return self.conn.execute("SELECT COUNT(*) FROM price_daily WHERE cg_id = ?", (cg_id,)).fetchone()[0]

    # ---- metric observations ----
    def save_metrics(self, date, cg_id, values, source_url, fetched_at):
        """values: {metric: number | str | list | dict | None}. Numbers go to `value`, everything else to `text`
        (JSON). None is stored too: "fetched, source had no value" differs from "never fetched"."""
        if not source_url:
            raise ValueError(f"source_url required ({cg_id})")
        rows = []
        for metric, v in values.items():
            if isinstance(v, bool):
                num, txt = float(v), None
            elif isinstance(v, (int, float)):
                num, txt = float(v), None
            elif v is None:
                num, txt = None, None
            else:
                num, txt = None, v if isinstance(v, str) else json.dumps(v, sort_keys=True)
            rows.append((date, cg_id, metric, num, txt, source_url, fetched_at))
        self.conn.executemany("INSERT OR REPLACE INTO metric_values VALUES (?,?,?,?,?,?,?)", rows)

    def replace_source_day(self, date, cg_id, url_prefix):
        """Drop one coin's same-day rows from one source before it is saved again (a re-fetch replaces today's
        observation; earlier days are never touched)."""
        self.conn.execute("DELETE FROM metric_values WHERE date = ? AND cg_id = ? AND source_url LIKE ?",
                          (date, cg_id, url_prefix + "%"))

    def latest_metrics(self, cg_id, as_of_date):
        """{metric: row} with the newest observation on or before as_of_date."""
        rows = self.conn.execute(
            "SELECT m.* FROM metric_values m JOIN (SELECT metric, MAX(date) d FROM metric_values "
            "WHERE cg_id = ? AND date <= ? GROUP BY metric) x ON m.metric = x.metric AND m.date = x.d "
            "WHERE m.cg_id = ?", (cg_id, as_of_date, cg_id)).fetchall()
        return {r["metric"]: r for r in rows}

    # ---- links ----
    def set_links(self, cg_id, kind, urls, now):
        self.conn.execute("DELETE FROM links WHERE cg_id = ? AND kind = ?", (cg_id, kind))
        self.conn.executemany("INSERT OR IGNORE INTO links VALUES (?,?,?,?)",
                              [(cg_id, kind, u, now) for u in urls if u])

    def links(self, cg_id, kind):
        return [r[0] for r in self.conn.execute(
            "SELECT url FROM links WHERE cg_id = ? AND kind = ? ORDER BY url", (cg_id, kind))]

    # ---- per-source fetch rotation ----
    def last_fetch(self, source, key):
        row = self.conn.execute("SELECT fetched_at FROM fetch_log WHERE source = ? AND key = ?",
                                (source, key)).fetchone()
        return row[0] if row else None

    def mark_fetched(self, source, key, now, status="ok"):
        self.conn.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?)", (source, key, now, status))

    def get_kv(self, key):
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_kv(self, key, value, now):
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, json.dumps(value), now))

    def prices(self, cg_id, since_date):
        return [tuple(r) for r in self.conn.execute(
            "SELECT date, price, mcap FROM price_daily WHERE cg_id = ? AND date >= ? ORDER BY date",
            (cg_id, since_date))]

    def save_scores(self, date, rows, metric_rows):
        """Replaces the scores of `date` (re-scoring a day after a config change overwrites that day only)."""
        self.conn.execute("DELETE FROM scores_daily WHERE date = ?", (date,))
        self.conn.execute("DELETE FROM metric_scores WHERE date = ?", (date,))
        self.conn.executemany("INSERT INTO scores_daily VALUES (?,?,?,?,?,?,?,?,?)", rows)
        self.conn.executemany("INSERT INTO metric_scores VALUES (?,?,?,?,?,?)", metric_rows)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()
