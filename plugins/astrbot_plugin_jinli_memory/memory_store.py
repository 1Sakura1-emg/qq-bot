"""Persistent, scoped conversational memories and a durable extraction queue."""

import hashlib
import math
import re
import sqlite3
import time
from pathlib import Path

SENSITIVE = re.compile(
    r"sk-[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{12,}|"
    r"(?:api[_ -]?key|secret[_ -]?key|access[_ -]?token|密码|密钥)\s*[:：=]\s*\S+|"
    r"(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)\d{17}[0-9Xx](?!\d)",
    re.IGNORECASE,
)


class MemoryStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL,
                speaker_id TEXT NOT NULL, speaker_name TEXT NOT NULL,
                source_group TEXT NOT NULL, topic TEXT NOT NULL, kind TEXT NOT NULL,
                content TEXT NOT NULL, evidence TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, forgotten_at REAL NOT NULL DEFAULT 0,
                UNIQUE(scope, speaker_id, topic)
            );
            CREATE TABLE IF NOT EXISTS pending (
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL, speaker_id TEXT NOT NULL,
                speaker_name TEXT NOT NULL, source_group TEXT NOT NULL,
                message_id TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL,
                UNIQUE(scope, source_group, message_id)
            );
            CREATE TABLE IF NOT EXISTS extraction_runs (created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS seen_messages (
                scope TEXT NOT NULL, source_group TEXT NOT NULL,
                message_id TEXT NOT NULL, received_at REAL NOT NULL,
                PRIMARY KEY(scope,source_group,message_id)
            );
            INSERT OR IGNORE INTO seen_messages
                SELECT scope,source_group,message_id,created_at FROM pending;
        """)
        self.db.commit()

    def enqueue(
        self, scope, speaker_id, speaker_name, group, message_id, text, created_at=None
    ):
        """Persist text for extraction without combining different private scopes.

        Args:
            scope: Public pool or the exact private sender scope.
            speaker_id: Trusted sender identifier from the platform event.
            speaker_name: Current platform nickname.
            group: Original group identifier, empty for private messages.
            message_id: Platform message identifier for deduplication.
            text: Original text eligible for memory extraction.
            created_at: Original event time when importing an existing public record.
        """
        if not text.strip() or SENSITIVE.search(text):
            return
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO seen_messages VALUES (?,?,?,?)",
                (scope, group, message_id, time.time()),
            )
            if not inserted.rowcount:
                return
            self.db.execute(
                "INSERT OR IGNORE INTO pending "
                "(scope,speaker_id,speaker_name,source_group,message_id,text,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    scope,
                    speaker_id,
                    speaker_name[:80],
                    group,
                    message_id,
                    text[:1500],
                    created_at if created_at is not None else time.time(),
                ),
            )
            self.db.execute(
                "DELETE FROM pending WHERE scope=? AND id NOT IN "
                "(SELECT id FROM pending WHERE scope=? ORDER BY id DESC LIMIT 160)",
                (scope, scope),
            )
            self.db.execute(
                "DELETE FROM seen_messages WHERE received_at<?",
                (time.time() - 30 * 86400,),
            )

    def next_batch(self, force=False):
        """Return one bounded batch, subject to a persistent global call budget.

        Args:
            force: Allow a manual verification to process a small pending batch.

        Returns:
            Up to sixteen records from one privacy scope, or an empty list.
        """
        now = time.time()
        if (
            self.db.execute(
                "SELECT count(*) FROM extraction_runs WHERE created_at>?", (now - 3600,)
            ).fetchone()[0]
            >= 12
        ):
            return []
        scope = self.db.execute(
            "SELECT scope FROM pending GROUP BY scope "
            "HAVING count(*)>=8 OR min(created_at)<? ORDER BY min(created_at) LIMIT 1",
            (now + 1 if force else now - 90,),
        ).fetchone()
        if not scope:
            return []
        records = self.db.execute(
            "SELECT * FROM pending WHERE scope=? ORDER BY id LIMIT 16", (scope[0],)
        ).fetchall()
        result, size = [], 0
        for row in records:
            if result and size + len(row["text"]) > 6000:
                break
            result.append(dict(row))
            size += len(row["text"])
        return result

    def commit_batch(self, batch, facts):
        """Validate attributed evidence and atomically save facts and finish a batch.

        Args:
            batch: Original records from a single privacy scope.
            facts: Structured extraction output, treated as untrusted data.

        Returns:
            Number of accepted memories.
        """
        if not batch or len({r["scope"] for r in batch}) != 1:
            raise ValueError("Memory batches must have one privacy scope.")
        added = 0
        with self.db:
            for fact in facts[:8] if isinstance(facts, list) else []:
                if not isinstance(fact, dict):
                    continue
                text, evidence = fact.get("text"), fact.get("evidence")
                if not isinstance(text, str) or not isinstance(evidence, str):
                    continue
                if (
                    not 4 <= len(text) <= 300
                    or len(evidence) < 3
                    or SENSITIVE.search(text)
                ):
                    continue
                source = next(
                    (
                        r
                        for r in batch
                        if r["speaker_id"] == str(fact.get("speaker_id"))
                        and evidence in r["text"]
                    ),
                    None,
                )
                if source is None:
                    continue
                topic = str(
                    fact.get("topic") or hashlib.sha256(text.encode()).hexdigest()
                )[:100]
                previous = self.db.execute(
                    "SELECT forgotten_at,updated_at FROM memories WHERE scope=? AND speaker_id=? AND topic=?",
                    (source["scope"], source["speaker_id"], topic),
                ).fetchone()
                if previous and (
                    source["created_at"] <= previous[0]
                    or source["created_at"] < previous[1]
                ):
                    continue
                kind = fact.get("kind", "claim")
                if kind not in {
                    "identity",
                    "preference",
                    "relationship",
                    "event",
                    "claim",
                }:
                    kind = "claim"
                self.db.execute(
                    "INSERT INTO memories (scope,speaker_id,speaker_name,source_group,topic,kind,"
                    "content,evidence,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(scope,speaker_id,topic) DO UPDATE SET "
                    "speaker_name=excluded.speaker_name,source_group=excluded.source_group,"
                    "kind=excluded.kind,content=excluded.content,evidence=excluded.evidence,"
                    "updated_at=excluded.updated_at,active=1",
                    (
                        source["scope"],
                        source["speaker_id"],
                        source["speaker_name"],
                        source["source_group"],
                        topic,
                        kind,
                        text,
                        evidence[:500],
                        source["created_at"],
                        source["created_at"],
                    ),
                )
                added += 1
            self.db.executemany(
                "DELETE FROM pending WHERE id=?", [(r["id"],) for r in batch]
            )
        return added

    def retrieve(self, scope, query, sender_id, limit=12):
        """Retrieve public facts and, only in private, the current sender's facts.

        Args:
            scope: Current audience, derived from the platform event.
            query: Current message for lexical relevance.
            sender_id: Real sender identifier for identity matching.
            limit: Maximum number of returned memories.

        Returns:
            Relevant records that are visible to the current audience.
        """
        allowed = ["public"] if scope == "public" else ["public", scope]
        rows = self.db.execute(
            "SELECT * FROM memories WHERE active=1 AND scope IN ("
            + ",".join("?" for _ in allowed)
            + ") ORDER BY updated_at DESC LIMIT 5000",
            allowed,
        ).fetchall()
        tokens = set(re.findall(r"[a-zA-Z]{2,}|[\u4e00-\u9fff]{2,}", query.lower()))
        grams = {t[i : i + 2] for t in tokens for i in range(len(t) - 1)}
        ranked = []
        for row in rows:
            body = (row["speaker_name"] + " " + row["content"]).lower()
            score = sum(2 for token in tokens if token in body)
            score += sum(0.4 for gram in grams if gram in body)
            if row["speaker_id"] == sender_id:
                score += 3 if row["kind"] == "identity" else 0.6
            score += 0.3 / (
                1 + math.log1p(max(0, time.time() - row["updated_at"]) / 3600)
            )
            ranked.append((score, dict(row)))
        ranked.sort(key=lambda item: (item[0], item[1]["updated_at"]), reverse=True)
        return [row for _, row in ranked[:limit]]

    def forget(self, memory_id, scope, sender_id, owner_id):
        """Stop retrieving a visible memory when its author or owner requests it.

        Args:
            memory_id: Memory identifier shown by the memory view command.
            scope: Current audience scope.
            sender_id: Authenticated command sender.
            owner_id: Configured owner, who may manage public memories.

        Returns:
            Whether the memory was visible and the sender was authorized.
        """
        row = self.db.execute(
            "SELECT * FROM memories WHERE id=? AND active=1", (memory_id,)
        ).fetchone()
        allowed = {"public"} if scope == "public" else {"public", scope}
        if not row or row["scope"] not in allowed:
            return False
        if sender_id != row["speaker_id"] and not (
            row["scope"] == "public" and sender_id == owner_id
        ):
            return False
        with self.db:
            self.db.execute(
                "UPDATE memories SET active=0,forgotten_at=? WHERE id=?",
                (time.time(), memory_id),
            )
        return True

    def close(self):
        """Close the local database connection."""
        self.db.close()
