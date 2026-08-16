#!/usr/bin/env python3
"""settings.py — SQLite settings store for the query-your-docs web UI.

Persists user-editable settings (LLM model config, persona, retrieval,
appearance) so the Settings drawer can read/save them across reloads.
Uses short-lived connections under a module lock, so it is safe to call
from threaded HTTP servers (FastAPI/uvicorn) — same pattern as history.py.

Schema (data/settings.db unless QYD_SETTINGS_DB is set):
  settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)

Key-value rows for forward compatibility. Defaults mirror the current
env-based config so behaviour is unchanged until the user saves:

  model.name          RAG_LLM_MODEL env or deepseek-v4-flash
  model.base_url      OPENAI_BASE_URL env or ""   (http(s) required if set)
  model.api_key       OPENAI_API_KEY env or ""    (never returned by the API)
  persona.preset      "concise"   (byte-identical to the historical rag.py prompt)
  persona.custom      ""          (additive instructions, <=2000 chars)
  retrieval.top_k     4           (1-10)
  retrieval.chunk_size 600        (100-2000; applies on next index)
  appearance.theme    "light"     (dark/light/system; current app base = light)
  appearance.language "en"        (en/id)

Persona preset strings are the backend contract from the approved design
(t_df177455, section 7): `preset` is stored as a name and `custom` is
appended to the active preset as "\\n\\nAdditional instructions:\\n{custom}".
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "settings.db"

DEFAULT_MODEL = "deepseek-v4-flash"

PRESET_CONCISE = (
    "You are a document-intelligence assistant. Answer the user's question using ONLY "
    "the provided context. Cite the source of every factual claim with its bracketed "
    "reference number, e.g. [1] or [1,2]. If the context does not contain the answer, "
    "say so clearly and do not invent facts. Answer in the language of the question. "
    "Keep the answer concise (max ~120 words)."
)
PRESET_DETAILED = (
    "You are a document-intelligence assistant. Answer the user's question using ONLY "
    "the provided context. Cite the source of every factual claim with its bracketed "
    "reference number, e.g. [1] or [1,2]. If the context does not contain the answer, "
    "say so clearly and do not invent facts. Answer in the language of the question. "
    "Give a thorough, well-structured answer (up to ~350 words): cover the key points, "
    "supporting numbers, and any caveats found in the context. Use short paragraphs or "
    "bullet lists when it improves readability."
)
PRESET_BEGINNER = (
    "You are a document-intelligence assistant. Answer the user's question using ONLY "
    "the provided context, in simple plain language. Cite the source of every factual "
    "claim with its bracketed reference number, e.g. [1] or [1,2]. If the context does "
    "not contain the answer, say so clearly and do not invent facts. Explain technical "
    "terms briefly before using them. Keep sentences short and the answer under ~200 words."
)
PRESET_INDONESIAN = (
    "Anda adalah asisten intelijen dokumen. Jawab pertanyaan pengguna HANYA menggunakan "
    "konteks yang diberikan. Kutip sumber setiap klaim faktual dengan nomor referensi "
    "dalam kurung, mis. [1] atau [1,2]. Jika konteks tidak memuat jawabannya, katakan "
    "dengan jelas dan jangan mengarang fakta. Jawablah SELALU dalam Bahasa Indonesia "
    "yang natural dan ringkas (maks ~120 kata), apa pun bahasa pertanyaannya."
)

PRESETS: dict[str, str] = {
    "concise": PRESET_CONCISE,
    "detailed": PRESET_DETAILED,
    "beginner": PRESET_BEGINNER,
    "indonesian": PRESET_INDONESIAN,
}
PRESET_IDS = tuple(PRESETS)
THEMES = ("dark", "light", "system")
LANGUAGES = ("en", "id")

CUSTOM_SUFFIX_TEMPLATE = "\n\nAdditional instructions:\n{custom}"

# Validation limits (design section 6.4 + DoD).
MAX_MODEL_NAME_CHARS = 120
MAX_BASE_URL_CHARS = 2000
MAX_API_KEY_CHARS = 4096
MAX_CUSTOM_CHARS = 2000
TOP_K_MIN, TOP_K_MAX = 1, 10
CHUNK_MIN, CHUNK_MAX = 100, 2000


def _db_path() -> Path:
    return Path(os.environ.get("QYD_SETTINGS_DB", DEFAULT_DB))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_int(v) -> bool:
    """True for real ints (rejects bool, which subclasses int)."""
    return isinstance(v, int) and not isinstance(v, bool)


def validate_payload(body: dict) -> tuple[dict, list[str]]:
    """Validate a PUT /api/settings body.

    Returns (normalized, errors). `normalized` keeps the same section keys
    as the input; each present section maps to a dict of validated values
    (absent sections are omitted so the caller can keep existing values).
    Unknown keys are ignored for forward compatibility. Errors is a list of
    human-readable messages; empty means valid.
    """
    if not isinstance(body, dict):
        return {}, ["Request body must be a JSON object."]

    errors: list[str] = []
    out: dict = {}

    # ------------------------------------------------------------ model
    if "model" in body:
        m = body["model"]
        if not isinstance(m, dict):
            errors.append("model must be an object.")
        else:
            mm: dict = {}
            name = m.get("name")
            if name is not None:
                if not isinstance(name, str) or not name.strip():
                    errors.append("model.name is required and must be non-empty.")
                elif len(name.strip()) > MAX_MODEL_NAME_CHARS:
                    errors.append(f"model.name must be at most {MAX_MODEL_NAME_CHARS} characters.")
                else:
                    mm["name"] = name.strip()
            else:
                errors.append("model.name is required.")
            base_url = m.get("base_url")
            if base_url is not None:
                if not isinstance(base_url, str):
                    errors.append("model.base_url must be a string.")
                elif len(base_url) > MAX_BASE_URL_CHARS:
                    errors.append(f"model.base_url must be at most {MAX_BASE_URL_CHARS} characters.")
                elif base_url.strip() and not _is_http_url(base_url.strip()):
                    errors.append("model.base_url must be an http(s) URL.")
                else:
                    mm["base_url"] = base_url.strip()
            api_key = m.get("api_key")
            if api_key is not None:
                if not isinstance(api_key, str):
                    errors.append("model.api_key must be a string.")
                elif len(api_key) > MAX_API_KEY_CHARS:
                    errors.append(f"model.api_key must be at most {MAX_API_KEY_CHARS} characters.")
                else:
                    mm["api_key"] = api_key  # empty keeps the existing key
            if mm:
                out["model"] = mm

    # ----------------------------------------------------------- persona
    if "persona" in body:
        p = body["persona"]
        if not isinstance(p, dict):
            errors.append("persona must be an object.")
        else:
            pp: dict = {}
            preset = p.get("preset")
            if preset is not None:
                if not isinstance(preset, str) or preset not in PRESET_IDS:
                    errors.append(f"persona.preset must be one of: {', '.join(PRESET_IDS)}.")
                else:
                    pp["preset"] = preset
            custom = p.get("custom")
            if custom is not None:
                if not isinstance(custom, str):
                    errors.append("persona.custom must be a string.")
                elif len(custom) > MAX_CUSTOM_CHARS:
                    errors.append(f"persona.custom must be at most {MAX_CUSTOM_CHARS} characters.")
                else:
                    pp["custom"] = custom
            if pp:
                out["persona"] = pp

    # --------------------------------------------------------- retrieval
    if "retrieval" in body:
        r = body["retrieval"]
        if not isinstance(r, dict):
            errors.append("retrieval must be an object.")
        else:
            rr: dict = {}
            top_k = r.get("top_k")
            if top_k is not None:
                if not _is_int(top_k) or not (TOP_K_MIN <= top_k <= TOP_K_MAX):
                    errors.append(f"retrieval.top_k must be an integer between {TOP_K_MIN} and {TOP_K_MAX}.")
                else:
                    rr["top_k"] = top_k
            chunk_size = r.get("chunk_size")
            if chunk_size is not None:
                if not _is_int(chunk_size) or not (CHUNK_MIN <= chunk_size <= CHUNK_MAX):
                    errors.append(f"retrieval.chunk_size must be an integer between {CHUNK_MIN} and {CHUNK_MAX}.")
                else:
                    rr["chunk_size"] = chunk_size
            if rr:
                out["retrieval"] = rr

    # -------------------------------------------------------- appearance
    if "appearance" in body:
        a = body["appearance"]
        if not isinstance(a, dict):
            errors.append("appearance must be an object.")
        else:
            aa: dict = {}
            theme = a.get("theme")
            if theme is not None:
                if not isinstance(theme, str) or theme not in THEMES:
                    errors.append(f"appearance.theme must be one of: {', '.join(THEMES)}.")
                else:
                    aa["theme"] = theme
            language = a.get("language")
            if language is not None:
                if not isinstance(language, str) or language not in LANGUAGES:
                    errors.append(f"appearance.language must be one of: {', '.join(LANGUAGES)}.")
                else:
                    aa["language"] = language
            if aa:
                out["appearance"] = aa

    return out, errors


def _is_http_url(value: str) -> bool:
    try:
        parts = urlparse(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


class SettingsStore:
    """Thread-safe SQLite settings store (key-value rows)."""

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
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    # ------------------------------------------------------------- reads
    def _defaults(self) -> dict[str, str]:
        return {
            "model.name": os.environ.get("RAG_LLM_MODEL", DEFAULT_MODEL),
            "model.base_url": os.environ.get("OPENAI_BASE_URL", ""),
            "model.api_key": os.environ.get("OPENAI_API_KEY", ""),
            "persona.preset": "concise",
            "persona.custom": "",
            "retrieval.top_k": "4",
            "retrieval.chunk_size": "600",
            "appearance.theme": "light",
            "appearance.language": "en",
        }

    def get_all(self) -> dict[str, str]:
        """Stored values merged over defaults (stored wins)."""
        raw = self._defaults()
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT key, value FROM settings").fetchall()
        for key, value in rows:
            raw[key] = value
        return raw

    def get(self, key: str) -> str:
        return self.get_all().get(key, "")

    def has_key(self) -> bool:
        return bool(self.get("model.api_key"))

    def view(self) -> dict:
        """Public settings shape — the API key is only exposed as has_key."""
        raw = self.get_all()
        try:
            top_k = int(raw["retrieval.top_k"])
        except (TypeError, ValueError):
            top_k = 4
        try:
            chunk_size = int(raw["retrieval.chunk_size"])
        except (TypeError, ValueError):
            chunk_size = 600
        return {
            "model": {
                "name": raw["model.name"],
                "base_url": raw["model.base_url"],
                "api_key": {"has_key": bool(raw["model.api_key"])},
            },
            "persona": {
                "preset": raw["persona.preset"],
                "custom": raw["persona.custom"],
            },
            "retrieval": {
                "top_k": top_k,
                "chunk_size": chunk_size,
            },
            "appearance": {
                "theme": raw["appearance.theme"],
                "language": raw["appearance.language"],
            },
        }

    # ------------------------------------------------------------- writes
    def save(self, payload: dict) -> None:
        """Persist validated settings (normalized shape from validate_payload).

        Only keys present in `payload` are written. api_key is written only
        when non-empty — an empty/absent key keeps the existing one (design
        section 6.2: "leaving it empty on Save keeps the existing key").
        """
        flat: dict[str, str] = {}
        if "model" in payload:
            m = payload["model"]
            if m.get("name") is not None:
                flat["model.name"] = m["name"]
            if m.get("base_url") is not None:
                flat["model.base_url"] = m["base_url"]
            if m.get("api_key"):
                flat["model.api_key"] = m["api_key"]
        if "persona" in payload:
            p = payload["persona"]
            if p.get("preset") is not None:
                flat["persona.preset"] = p["preset"]
            if p.get("custom") is not None:
                flat["persona.custom"] = p["custom"]
        if "retrieval" in payload:
            r = payload["retrieval"]
            if r.get("top_k") is not None:
                flat["retrieval.top_k"] = str(r["top_k"])
            if r.get("chunk_size") is not None:
                flat["retrieval.chunk_size"] = str(r["chunk_size"])
        if "appearance" in payload:
            a = payload["appearance"]
            if a.get("theme") is not None:
                flat["appearance.theme"] = a["theme"]
            if a.get("language") is not None:
                flat["appearance.language"] = a["language"]
        if not flat:
            return
        now = _now()
        with self._lock, self._connect() as con:
            con.executemany(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                [(k, v, now) for k, v in flat.items()],
            )
