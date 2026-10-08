"""SQLite store: discovered tokens, holder snapshots, post history."""
import sqlite3
from datetime import datetime, timezone
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
-- One row per RugCheck call; used for holder growth.
CREATE TABLE IF NOT EXISTS snapshots (
    address        TEXT NOT NULL,
    ts             REAL NOT NULL,
    holders        INTEGER,
    liquidity_usd  REAL,
    volume_h24_usd REAL,
    PRIMARY KEY (address, ts)
);
-- Dedupe + daily cap are per chat, so TEST-channel posts never block the main channel.
CREATE TABLE IF NOT EXISTS posts (
    address    TEXT NOT NULL,                -- never post the same token twice to a chat
    chat       TEXT NOT NULL,
    posted_at  REAL NOT NULL,
    message_id INTEGER,
    PRIMARY KEY (address, chat)
);
CREATE TABLE IF NOT EXISTS zero_notices (
    day       TEXT NOT NULL,                 -- UTC date; at most one "0 passed" per day per chat
    chat      TEXT NOT NULL,
    posted_at REAL NOT NULL,
    PRIMARY KEY (day, chat)
);
"""

# Stage 3 first draft keyed these tables without `chat`. Recreate only if empty.
_PER_CHAT_TABLES = ("posts", "zero_notices")


def utc_day_start(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._migrate_per_chat()
        self.db.executescript(SCHEMA)

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
    def add_snapshot(self, address, now, holders, liquidity_usd, volume_h24_usd):
        self.db.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?)",
            (address, now, holders, liquidity_usd, volume_h24_usd),
        )

    def snapshots_since(self, address, since):
        return self.db.execute(
            "SELECT ts, holders FROM snapshots WHERE address = ? AND ts >= ? AND holders IS NOT NULL "
            "ORDER BY ts", (address, since),
        ).fetchall()

    # --- posts ---
    def posts_today(self, now, chat):
        return self.db.execute(
            "SELECT COUNT(*) FROM posts WHERE chat = ? AND posted_at >= ?", (chat, utc_day_start(now))
        ).fetchone()[0]

    def posted_to(self, chat):
        return {r[0] for r in self.db.execute("SELECT address FROM posts WHERE chat = ?", (chat,))}

    def add_post(self, address, now, chat, message_id):
        # Token stays 'active' so it can still be evaluated/posted for other chats.
        self.db.execute("INSERT INTO posts (address, chat, posted_at, message_id) VALUES (?, ?, ?, ?)",
                        (address, chat, now, message_id))

    def zero_notice_sent(self, day, chat):
        return self.db.execute("SELECT 1 FROM zero_notices WHERE day = ? AND chat = ?",
                               (day, chat)).fetchone() is not None

    def add_zero_notice(self, day, chat, now):
        self.db.execute("INSERT INTO zero_notices (day, chat, posted_at) VALUES (?, ?, ?)", (day, chat, now))

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()
