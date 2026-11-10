import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


class SocialStore:
    def __init__(self, path, now):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS actions (
                key TEXT PRIMARY KEY,kind TEXT NOT NULL,day TEXT NOT NULL,
                created REAL NOT NULL,status TEXT NOT NULL,content TEXT,
                remote_id TEXT,error TEXT
            );
            CREATE INDEX IF NOT EXISTS actions_daily ON actions(kind,day,status);
        """)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO meta VALUES('enabled_since',?)", (str(now),)
            )
        path.chmod(0o600)
        self.enabled_since = float(
            self.db.execute(
                "SELECT value FROM meta WHERE key='enabled_since'"
            ).fetchone()[0]
        )

    def eligible(self, kind, limit, interval, now):
        """Check the local-day write budget, request budget, and spacing.

        Args:
            kind: Public comment or own-post reply.
            limit: Maximum daily write attempts, including uncertain outcomes.
            interval: Minimum seconds since the previous write attempt.
            now: Current Unix timestamp.

        Returns:
            Whether another candidate may be prepared.
        """
        day = datetime.fromtimestamp(now, TZ).date().isoformat()
        count = self.db.execute(
            "SELECT count(*) FROM actions WHERE kind=? AND day=? AND status IN ('attempting','published','uncertain')",
            (kind, day),
        ).fetchone()[0]
        preparations = self.db.execute(
            "SELECT count(*) FROM actions WHERE kind=? AND day=?", (kind, day)
        ).fetchone()[0]
        last = self.db.execute(
            "SELECT max(created) FROM actions WHERE kind=? AND status IN ('attempting','published','uncertain')",
            (kind,),
        ).fetchone()[0]
        return (
            count < limit
            and preparations < max(10, limit * 6)
            and (last is None or now - last >= interval)
        )

    def reserve(self, key, kind, limit, interval, now):
        """Persist a candidate before generation so restart cannot duplicate it.

        Args:
            key: Stable upstream post/comment identity.
            kind: Public comment or own-post reply.
            limit: Daily write-attempt ceiling.
            interval: Minimum interval between write attempts.
            now: Current Unix timestamp.

        Returns:
            True only for a new candidate within its budgets.
        """
        if not self.eligible(kind, limit, interval, now):
            return False
        day = datetime.fromtimestamp(now, TZ).date().isoformat()
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO actions(key,kind,day,created,status) VALUES(?,?,?,?,'preparing')",
                (key, kind, day, now),
            )
        return cursor.rowcount == 1

    def mark(self, key, status, content=None, remote_id=None, error=None):
        """Commit a transition, particularly before a potentially ambiguous write.

        Args:
            key: Reserved candidate identity.
            status: Preparing, skipped, attempting, published, uncertain, or failed.
            content: Generated public comment, when available.
            remote_id: Actual API-returned comment identifier, when available.
            error: Sanitized error category without credentials or raw responses.
        """
        with self.db:
            self.db.execute(
                "UPDATE actions SET status=?,content=coalesce(?,content),remote_id=?,error=? WHERE key=?",
                (status, content, remote_id, error, key),
            )

    def close(self):
        self.db.close()
