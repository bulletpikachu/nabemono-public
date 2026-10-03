import asyncio
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


@dataclass
class MemoryRecord:
    id: int
    user_id: int
    username: str
    channel_id: int
    content: str
    source_message_id: int | None
    importance: int
    score: float | None = None


class LongTermMemory:
    def __init__(
        self,
        db_path,
        embedding_model,
        top_k=5,
        min_score=0.55,
        # With bge-small, distinct same-template facts ("likes dogs"/"likes
        # cats", two different birthdays) score up to ~0.95, so only treat
        # near-identical text as a duplicate; overwriting is destructive.
        dedup_min_score=0.98,
        embedder=None,
    ):
        self.db_path = Path(db_path)
        self.embedding_model = embedding_model
        self.top_k = top_k
        self.min_score = min_score
        self.dedup_min_score = dedup_min_score
        self._embedder = embedder
        self._embedder_lock = threading.Lock()
        # remember() does a check-then-insert; concurrent to_thread calls each
        # get their own connection, so the dedup check must be serialized.
        self._write_lock = threading.Lock()
        self._init_db()

    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    source_message_id INTEGER,
                    importance INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    embedding BLOB NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_user_channel
                ON memories(user_id, channel_id)
                """
            )

    @property
    def embedder(self):
        with self._embedder_lock:
            if self._embedder is None:
                from fastembed import TextEmbedding

                self._embedder = TextEmbedding(model_name=self.embedding_model)
            return self._embedder

    def _embed(self, text):
        vector = next(iter(self.embedder.embed([text])))
        return np.asarray(vector, dtype=np.float32)

    def _embed_query(self, text):
        # bge models expect this prefix on queries (but not on stored passages)
        # for short-query-to-passage retrieval.
        if "bge" in str(self.embedding_model).lower():
            text = f"Represent this sentence for searching relevant passages: {text}"
        return self._embed(text)

    @staticmethod
    def _serialize_embedding(vector):
        return np.asarray(vector, dtype=np.float32).tobytes()

    @staticmethod
    def _deserialize_embedding(blob):
        return np.frombuffer(blob, dtype=np.float32)

    @staticmethod
    def _cosine_similarity(left, right):
        denominator = np.linalg.norm(left) * np.linalg.norm(right)
        if denominator == 0:
            return 0.0
        return float(np.dot(left, right) / denominator)

    def remember(self, user_id, username, channel_id, content, source_message_id=None, importance=1):
        content = content.strip()
        if not content:
            return None

        now = datetime.now(timezone.utc).isoformat()
        embedding = self._embed(content)
        importance = int(importance)

        with self._write_lock, closing(self._connect()) as conn, conn:
            duplicate = self._find_duplicate(conn, user_id, embedding)
            if duplicate is not None:
                duplicate_id, duplicate_importance = duplicate
                conn.execute(
                    """
                    UPDATE memories
                    SET username = ?, content = ?, source_message_id = ?,
                        importance = ?, updated_at = ?, embedding = ?
                    WHERE id = ?
                    """,
                    (
                        username,
                        content,
                        source_message_id,
                        max(importance, duplicate_importance),
                        now,
                        self._serialize_embedding(embedding),
                        duplicate_id,
                    ),
                )
                return duplicate_id

            cursor = conn.execute(
                """
                INSERT INTO memories (
                    user_id, username, channel_id, content, source_message_id,
                    importance, created_at, updated_at, embedding
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    channel_id,
                    content,
                    source_message_id,
                    importance,
                    now,
                    now,
                    self._serialize_embedding(embedding),
                ),
            )
            return cursor.lastrowid

    def _find_duplicate(self, conn, user_id, embedding):
        """Return (id, importance) of an existing near-identical memory, if any."""
        rows = conn.execute(
            "SELECT id, importance, embedding FROM memories WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        best = None
        best_score = self.dedup_min_score
        for row_id, row_importance, blob in rows:
            existing = self._deserialize_embedding(blob)
            if existing.shape != embedding.shape:
                continue
            score = self._cosine_similarity(embedding, existing)
            if score >= best_score:
                best = (row_id, row_importance)
                best_score = score
        return best

    async def remember_async(self, *args, **kwargs):
        return await asyncio.to_thread(self.remember, *args, **kwargs)

    def search(self, query, top_k=None, min_score=None, user_id=None, channel_id=None):
        if not isinstance(query, str):
            return []
        query = query.strip()
        if not query:
            return []

        top_k = top_k if top_k is not None else self.top_k
        min_score = min_score if min_score is not None else self.min_score
        query_embedding = self._embed_query(query)

        clauses = []
        params = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if channel_id is not None:
            clauses.append("channel_id = ?")
            params.append(channel_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT id, user_id, username, channel_id, content, source_message_id,
                   importance, embedding
            FROM memories
            {where}
        """

        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()

        results = []
        for row in rows:
            memory_embedding = self._deserialize_embedding(row[7])
            if memory_embedding.shape != query_embedding.shape:
                continue
            score = self._cosine_similarity(query_embedding, memory_embedding)
            if score >= min_score:
                results.append(
                    MemoryRecord(
                        id=row[0],
                        user_id=row[1],
                        username=row[2],
                        channel_id=row[3],
                        content=row[4],
                        source_message_id=row[5],
                        importance=row[6],
                        score=score,
                    )
                )

        results.sort(key=lambda memory: (memory.score, memory.importance), reverse=True)
        return results[:top_k]

    async def search_async(self, *args, **kwargs):
        return await asyncio.to_thread(self.search, *args, **kwargs)

    def forget(self, memory_id):
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cursor.rowcount > 0

    def clear_user(self, username):
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE lower(username) = lower(?)",
                (username,),
            )
            return cursor.rowcount

    def list_for_user(self, username, limit=10):
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, username, channel_id, content, source_message_id,
                       importance
                FROM memories
                WHERE lower(username) = lower(?)
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (username, limit),
            ).fetchall()

        return [
            MemoryRecord(
                id=row[0],
                user_id=row[1],
                username=row[2],
                channel_id=row[3],
                content=row[4],
                source_message_id=row[5],
                importance=row[6],
            )
            for row in rows
        ]
