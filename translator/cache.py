"""SQLite-backed paragraph cache; successful calls survive interrupted runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from translator.epub_processor import ParagraphRef


class LearningCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paragraph_cache (
                cache_key TEXT PRIMARY KEY,
                book_key TEXT NOT NULL,
                profile_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS syntax_cache (
                cache_key TEXT PRIMARY KEY,
                book_key TEXT NOT NULL,
                syntax_profile_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.commit()

    def __enter__(self) -> "LearningCache":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def book_key(source: Path) -> str:
        digest = hashlib.sha256()
        with source.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def key(
        book_key: str,
        profile_key: str,
        ref: ParagraphRef,
        *,
        context_identity: str = "",
    ) -> str:
        parts = (book_key, profile_key, ref.href, str(ref.paragraph), ref.text)
        payload = "\0".join(parts)
        if context_identity:
            payload = "\0".join(
                (book_key, profile_key, context_identity, ref.href, str(ref.paragraph), ref.text)
            )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def syntax_key(
        book_key: str, syntax_profile_key: str, ref: ParagraphRef
    ) -> str:
        payload = "\0".join(
            (book_key, syntax_profile_key, ref.href, str(ref.paragraph), ref.text)
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, cache_key: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM paragraph_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(
        self,
        cache_key: str,
        book_key: str,
        profile_key: str,
        payload: dict,
    ) -> None:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO paragraph_cache
                (cache_key, book_key, profile_key, payload)
                VALUES (?, ?, ?, ?)
                """,
                (cache_key, book_key, profile_key, serialized),
            )
            self._connection.commit()

    def delete(self, cache_key: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM paragraph_cache WHERE cache_key = ?", (cache_key,)
            )
            self._connection.commit()

    def get_syntax(self, cache_key: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM syntax_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put_syntax(
        self,
        cache_key: str,
        book_key: str,
        syntax_profile_key: str,
        payload: dict,
    ) -> None:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO syntax_cache
                (cache_key, book_key, syntax_profile_key, payload)
                VALUES (?, ?, ?, ?)
                """,
                (cache_key, book_key, syntax_profile_key, serialized),
            )
            self._connection.commit()

    def delete_syntax(self, cache_key: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM syntax_cache WHERE cache_key = ?", (cache_key,)
            )
            self._connection.commit()

    def delete_book(self, book_key: str) -> int:
        with self._lock:
            paragraph_cursor = self._connection.execute(
                "DELETE FROM paragraph_cache WHERE book_key = ?", (book_key,)
            )
            syntax_cursor = self._connection.execute(
                "DELETE FROM syntax_cache WHERE book_key = ?", (book_key,)
            )
            self._connection.commit()
            return paragraph_cursor.rowcount + syntax_cursor.rowcount
