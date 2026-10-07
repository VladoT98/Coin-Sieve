"""SQLite candidate store: accumulates discovered tokens across runs."""
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
"""


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

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

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()
