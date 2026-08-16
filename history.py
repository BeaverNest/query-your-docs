#!/usr/bin/env python3
"""history.py — SQLite conversation store for the query-your-docs web UI.

Persists conversations and messages so the frontend can list history,
load a conversation, and keep answers (with their citation sources)
across reloads. Uses short-lived connections under a module lock, so it
is safe to call from threaded HTTP servers (FastAPI/uvicorn).

Schema (data/history.db unless QYD_HISTORY_DB is set):
  conversations(id TEXT PK, title, created_at, updated_at)
  messages(id INTEGER PK AUTOINCREMENT, conversation_id, role, content,
           sources TEXT /* JSON array or NULL */, created_at)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "history.db"

CONVERSATION_ID_RE = r"^c_[A-Za-z0-9]{8,64}$"


def _db_path() -> Path:
    return Path(os.environ.get("QYD_HISTORY_DB", DEFAULT_DB))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HistoryStore:
    """Thread-safe SQLite store for conversations and messages."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else _db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------- schema
    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_schema(self) -> None:
        with self._lock, self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sources TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conv
                    ON messages(conversation_id, id);
                """
            )

    # ------------------------------------------------------------- writes
    def create_conversation(self, title: str, conversation_id: str | None = None) -> dict:
        """Create a conversation; returns the row dict."""
        cid = conversation_id or ("c_" + os.urandom(16).hex())
        now = _now()
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (cid, title, now, now),
            )
        return self.get_conversation(cid)

    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        sources: list | None = None,
    ) -> None:
        """Append a message and bump the conversation's updated_at."""
        sources_json = json.dumps(sources, ensure_ascii=False) if sources else None
        now = _now()
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO messages (conversation_id, role, content, sources, created_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, role, content, sources_json, now),
            )
            con.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )

    # -------------------------------------------------------------- reads
    def list_conversations(self) -> list[dict]:
        with self._lock, self._connect() as con:
            rows = con.execute(
                """
                SELECT c.id, c.title, c.created_at, c.updated_at,
                       COUNT(m.id) AS message_count
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                GROUP BY c.id
                ORDER BY c.updated_at DESC
                """
            ).fetchall()
        return [
            {
                "id": r[0],
                "title": r[1],
                "created_at": r[2],
                "updated_at": r[3],
                "message_count": r[4],
            }
            for r in rows
        ]

    def get_conversation(self, conversation_id: str) -> dict | None:
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return None
            mrows = con.execute(
                "SELECT role, content, sources, created_at FROM messages "
                "WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        messages = []
        for role, content, sources_json, created_at in mrows:
            msg: dict = {"role": role, "content": content, "created_at": created_at}
            msg["sources"] = json.loads(sources_json) if sources_json else None
            messages.append(msg)
        return {
            "id": row[0],
            "title": row[1],
            "created_at": row[2],
            "updated_at": row[3],
            "messages": messages,
        }

    def exists(self, conversation_id: str) -> bool:
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------ metrics
    def count_conversations(self) -> int:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT COUNT(*) FROM conversations").fetchone()
        return row[0] if row else 0
