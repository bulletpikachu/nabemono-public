from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


MAX_KEY_LENGTH = 100
MAX_VALUE_LENGTH = 4_000
MAX_ALIAS_LENGTH = 100
MAX_ALIASES = 20
_NON_WORD = re.compile(r"[^a-z0-9]+")


class PersonaValidationError(ValueError):
    pass


class PersonaConflictError(ValueError):
    pass


@dataclass(frozen=True)
class PersonaFact:
    id: int
    key: str
    value: str
    aliases: tuple[str, ...]
    created_at: str
    updated_at: str
    match: str | None = None

    def public_payload(self, *, include_aliases: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "key": self.key,
            "value": self.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_aliases:
            payload["aliases"] = list(self.aliases)
        if self.match is not None:
            payload["match"] = self.match
        return payload


def normalize_text(value: str) -> str:
    return " ".join(_NON_WORD.sub(" ", value.casefold()).split())


def normalize_key(value: str) -> str:
    return normalize_text(value).replace(" ", "_")


class PersonaStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._write_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _ensure_db(self) -> None:
        if self._initialized:
            return
        with self._write_lock:
            if self._initialized:
                return
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS persona_facts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        key TEXT NOT NULL UNIQUE,
                        value TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS persona_aliases (
                        alias TEXT PRIMARY KEY,
                        fact_id INTEGER NOT NULL
                            REFERENCES persona_facts(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_persona_aliases_fact
                    ON persona_aliases(fact_id)
                    """
                )
            self._initialized = True

    @staticmethod
    def _validated_key(value: object) -> str:
        key = normalize_key(str(value or ""))
        if not key:
            raise PersonaValidationError("key must not be empty")
        if len(key) > MAX_KEY_LENGTH:
            raise PersonaValidationError(
                f"key must be at most {MAX_KEY_LENGTH} characters"
            )
        return key

    @staticmethod
    def _validated_value(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            raise PersonaValidationError("value must not be empty")
        if len(text) > MAX_VALUE_LENGTH:
            raise PersonaValidationError(
                f"value must be at most {MAX_VALUE_LENGTH} characters"
            )
        return text

    @staticmethod
    def _validated_aliases(values: object) -> tuple[str, ...]:
        if values is None:
            return ()
        if not isinstance(values, (list, tuple)):
            raise PersonaValidationError("aliases must be a list")
        if len(values) > MAX_ALIASES:
            raise PersonaValidationError(
                f"a fact can have at most {MAX_ALIASES} aliases"
            )
        aliases: list[str] = []
        for value in values:
            alias = normalize_text(str(value or ""))
            if not alias:
                raise PersonaValidationError("aliases must not be empty")
            if len(alias) > MAX_ALIAS_LENGTH:
                raise PersonaValidationError(
                    f"aliases must be at most {MAX_ALIAS_LENGTH} characters"
                )
            if alias not in aliases:
                aliases.append(alias)
        return tuple(aliases)

    @staticmethod
    def _fact_from_row(
        row: sqlite3.Row,
        aliases: tuple[str, ...],
        *,
        match: str | None = None,
    ) -> PersonaFact:
        return PersonaFact(
            id=row["id"],
            key=row["key"],
            value=row["value"],
            aliases=aliases,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            match=match,
        )

    @staticmethod
    def _aliases_by_fact(
        connection: sqlite3.Connection,
        fact_ids: list[int],
    ) -> dict[int, tuple[str, ...]]:
        if not fact_ids:
            return {}
        placeholders = ",".join("?" for _ in fact_ids)
        rows = connection.execute(
            f"""
            SELECT fact_id, alias
            FROM persona_aliases
            WHERE fact_id IN ({placeholders})
            ORDER BY alias
            """,
            fact_ids,
        ).fetchall()
        aliases: dict[int, list[str]] = {fact_id: [] for fact_id in fact_ids}
        for row in rows:
            aliases[row["fact_id"]].append(row["alias"])
        return {
            fact_id: tuple(values)
            for fact_id, values in aliases.items()
        }

    def list_facts(self) -> list[PersonaFact]:
        self._ensure_db()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM persona_facts ORDER BY key"
            ).fetchall()
            aliases = self._aliases_by_fact(
                connection,
                [row["id"] for row in rows],
            )
        return [
            self._fact_from_row(row, aliases.get(row["id"], ()))
            for row in rows
        ]

    def get(self, fact_id: int) -> PersonaFact | None:
        self._ensure_db()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM persona_facts WHERE id = ?",
                (fact_id,),
            ).fetchone()
            if row is None:
                return None
            aliases = self._aliases_by_fact(connection, [row["id"]])
        return self._fact_from_row(row, aliases.get(row["id"], ()))

    def _check_conflicts(
        self,
        connection: sqlite3.Connection,
        key: str,
        aliases: tuple[str, ...],
        *,
        exclude_id: int | None = None,
    ) -> None:
        values = {normalize_text(key), *aliases}
        for value in values:
            key_row = connection.execute(
                """
                SELECT id FROM persona_facts
                WHERE replace(key, '_', ' ') = ?
                """,
                (value,),
            ).fetchone()
            if key_row is not None and key_row["id"] != exclude_id:
                raise PersonaConflictError(
                    f"'{value}' is already used by another fact"
                )
            alias_row = connection.execute(
                "SELECT fact_id FROM persona_aliases WHERE alias = ?",
                (value,),
            ).fetchone()
            if alias_row is not None and alias_row["fact_id"] != exclude_id:
                raise PersonaConflictError(
                    f"'{value}' is already used by another fact"
                )

    def create(
        self,
        *,
        key: object,
        value: object,
        aliases: object = None,
    ) -> PersonaFact:
        self._ensure_db()
        clean_key = self._validated_key(key)
        clean_value = self._validated_value(value)
        clean_aliases = self._validated_aliases(aliases)
        if normalize_text(clean_key) in clean_aliases:
            clean_aliases = tuple(
                alias
                for alias in clean_aliases
                if alias != normalize_text(clean_key)
            )

        with self._write_lock, closing(self._connect()) as connection, connection:
            self._check_conflicts(connection, clean_key, clean_aliases)
            cursor = connection.execute(
                "INSERT INTO persona_facts (key, value) VALUES (?, ?)",
                (clean_key, clean_value),
            )
            fact_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO persona_aliases (alias, fact_id) VALUES (?, ?)",
                [(alias, fact_id) for alias in clean_aliases],
            )
        fact = self.get(fact_id)
        assert fact is not None
        return fact

    def update(
        self,
        fact_id: int,
        *,
        key: object,
        value: object,
        aliases: object = None,
    ) -> PersonaFact | None:
        self._ensure_db()
        clean_key = self._validated_key(key)
        clean_value = self._validated_value(value)
        clean_aliases = self._validated_aliases(aliases)
        if normalize_text(clean_key) in clean_aliases:
            clean_aliases = tuple(
                alias
                for alias in clean_aliases
                if alias != normalize_text(clean_key)
            )

        with self._write_lock, closing(self._connect()) as connection, connection:
            exists = connection.execute(
                "SELECT 1 FROM persona_facts WHERE id = ?",
                (fact_id,),
            ).fetchone()
            if exists is None:
                return None
            self._check_conflicts(
                connection,
                clean_key,
                clean_aliases,
                exclude_id=fact_id,
            )
            connection.execute(
                """
                UPDATE persona_facts
                SET key = ?, value = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (clean_key, clean_value, fact_id),
            )
            connection.execute(
                "DELETE FROM persona_aliases WHERE fact_id = ?",
                (fact_id,),
            )
            connection.executemany(
                "INSERT INTO persona_aliases (alias, fact_id) VALUES (?, ?)",
                [(alias, fact_id) for alias in clean_aliases],
            )
        return self.get(fact_id)

    def delete(self, fact_id: int) -> bool:
        self._ensure_db()
        with self._write_lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM persona_facts WHERE id = ?",
                (fact_id,),
            )
            return cursor.rowcount > 0

    def search(self, query: object, limit: int = 5) -> list[PersonaFact]:
        text = normalize_text(str(query or ""))
        if not text:
            return []
        limit = max(1, min(20, int(limit)))
        facts = self.list_facts()
        query_tokens = set(text.split())
        ranked: list[tuple[float, PersonaFact, str]] = []

        for fact in facts:
            key_text = normalize_text(fact.key)
            candidates = (key_text, *fact.aliases)
            if text == key_text:
                ranked.append((4.0, fact, "key"))
                continue
            if text in fact.aliases:
                ranked.append((3.5, fact, "alias"))
                continue

            best = 0.0
            for candidate in candidates:
                candidate_tokens = set(candidate.split())
                if not candidate_tokens:
                    continue
                if candidate in text:
                    score = 2.0 + len(candidate_tokens) / max(
                        len(query_tokens),
                        1,
                    )
                else:
                    overlap = len(query_tokens & candidate_tokens)
                    coverage = overlap / len(candidate_tokens)
                    score = coverage if coverage >= 0.6 else 0.0
                best = max(best, score)
            if best:
                ranked.append((best, fact, "related"))

        ranked.sort(key=lambda item: (-item[0], item[1].key))
        return [
            PersonaFact(
                **{
                    **fact.__dict__,
                    "match": match,
                }
            )
            for _, fact, match in ranked[:limit]
        ]
