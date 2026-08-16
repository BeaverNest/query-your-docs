#!/usr/bin/env python3
"""server.py — FastAPI web server for query-your-docs.

NotebookLM-style web backend: upload multiple documents, index them into the
local knowledge base, ask questions (answers carry [n] citations and a sources
list), and persist conversation history in SQLite.

Run:
  .venv/bin/python server.py                 # uvicorn on 127.0.0.1:8000
  QYD_PORT=9000 .venv/bin/python server.py   # custom port

Endpoints (see API.md for the full contract):
  GET    /api/health
  GET    /api/config
  GET    /api/settings
  PUT    /api/settings
  POST   /api/settings/test
  GET    /api/sources
  POST   /api/upload               multipart, multiple files
  POST   /api/index
  GET    /api/index/status
  DELETE /api/sources/{doc_id}
  POST   /api/ask
  GET    /api/history
  GET    /api/history/{conversation_id}

All responses use the envelope {ok: bool, data?: ...} on success and
{ok: false, error: {code, message}} on failure. Secrets are never returned:
GET /api/settings exposes the API key only as api_key.has_key.
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import rag
from history import CONVERSATION_ID_RE, HistoryStore
from rag import RagError
from settings import (
    MAX_API_KEY_CHARS,
    MAX_BASE_URL_CHARS,
    MAX_MODEL_NAME_CHARS,
    SettingsStore,
    validate_payload,
)

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv = getattr(rag, "load_dotenv", None)
if load_dotenv:
    load_dotenv(SCRIPT_DIR / ".env")

# ----------------------------------------------------------------- config
APP_VERSION = "0.1.0"
DEFAULT_PORT = int(os.environ.get("QYD_PORT", "8000"))

DOCS_DIR = Path(os.environ.get("QYD_DOCS_DIR", SCRIPT_DIR / "data" / "docs"))
STATIC_DIR = SCRIPT_DIR / "static"

ALLOWED_SUFFIXES = {".pdf", ".txt"}
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB per file
MAX_FILES_PER_BATCH = 20
MAX_QUESTION_CHARS = 4000
DEFAULT_LLM_MODEL = "deepseek-v4-flash"

# ----------------------------------------------------------------- state
app = FastAPI(title="query-your-docs API", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local portfolio demo; no credentials/cookies used
    allow_methods=["*"],
    allow_headers=["*"],
)

history_store = HistoryStore()
settings_store = SettingsStore()


def _sync_env_from_settings() -> None:
    """Apply stored model settings to the process env.

    rag.py reads OPENAI_API_KEY / OPENAI_BASE_URL / RAG_LLM_MODEL from the
    environment; keeping the env in sync here means a key/model saved via
    PUT /api/settings is used by /api/ask immediately and after restart.
    Called once at startup and after every settings save.
    """
    raw = settings_store.get_all()
    if raw.get("model.name"):
        os.environ["RAG_LLM_MODEL"] = raw["model.name"]
    if raw.get("model.base_url"):
        os.environ["OPENAI_BASE_URL"] = raw["model.base_url"]
    if raw.get("model.api_key"):
        os.environ["OPENAI_API_KEY"] = raw["model.api_key"]


_sync_env_from_settings()

_INDEX_LOCK = threading.Lock()
_index_state = {
    "status": "idle",          # idle | indexing
    "started_at": None,
    "finished_at": None,
    "result": None,            # last index summary
    "error": None,             # last index error message (or None)
}


# ------------------------------------------------------------- helpers
def _err(code: str, message: str, status: int) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _docs_dir() -> Path:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    return DOCS_DIR


def _collect_docs() -> list[Path]:
    """All indexed-ready documents (.pdf/.txt) in the docs dir, sorted."""
    d = _docs_dir()
    pdfs = sorted(p for p in d.iterdir() if p.suffix.lower() == ".pdf")
    txts = sorted(p for p in d.iterdir() if p.suffix.lower() == ".txt")
    return pdfs + txts


def _safe_name(filename: str | None) -> str:
    return Path(filename or "").name


def _llm_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _run_index_locked() -> dict:
    """Rebuild the whole KB from the docs dir. Caller holds _INDEX_LOCK.

    Uses the saved retrieval.chunk_size (applies on this next index; the
    chunker runs at index time, so a saved change takes effect on the next
    rebuild, matching design section 6.8).
    """
    _index_state.update({"status": "indexing", "started_at": _now_utc(), "result": None, "error": None})
    try:
        chunk_size = _settings_int("retrieval.chunk_size", rag.TARGET_CHUNK_TOKENS)
        result = rag.index_docs(_collect_docs(), chunk_size=chunk_size)
        _index_state.update({"status": "idle", "finished_at": _now_utc(), "result": result, "error": None})
        return result
    except Exception as exc:  # noqa: BLE001 - surfaced as JSON error
        traceback.print_exc()
        _index_state.update({"status": "idle", "finished_at": _now_utc(), "error": str(exc)})
        raise


def _index_in_progress() -> bool:
    return _index_state["status"] == "indexing"


def _sources_view() -> list[dict]:
    """KB sources (status ready) merged with uploaded-but-unindexed files
    (status pending) and last-index errors (status error)."""
    kb = {s["id"]: s for s in rag.kb_sources()}
    indexed_ids = set(kb)
    per_doc_errors = {}
    if _index_state.get("result"):
        for d in _index_state["result"].get("per_doc", []):
            if d.get("status") == "error":
                per_doc_errors[Path(d["name"]).stem] = d.get("error", "no-text-extracted")

    sources = []
    for doc_id, s in kb.items():
        sources.append({
            "id": doc_id,
            "title": s["title"],
            "pages": s["pages"],
            "chunks": s["chunks"],
            "status": "error" if doc_id in per_doc_errors else "ready",
            "error": per_doc_errors.get(doc_id),
        })

    for path in _collect_docs():
        doc_id = path.stem
        if doc_id in indexed_ids:
            continue
        sources.append({
            "id": doc_id,
            "title": rag.doc_title(path),
            "pages": rag.doc_page_count(path),
            "chunks": 0,
            "status": "error" if doc_id in per_doc_errors else "pending",
            "error": per_doc_errors.get(doc_id),
        })
    sources.sort(key=lambda s: s["id"])
    return sources


def _envelope_ok(data) -> dict:
    return {"ok": True, "data": data}


# --------------------------------------------------------- error handling
@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={"ok": False, "error": {"code": "bad-request", "message": "Invalid request body."}},
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": detail})
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": {"code": "bad-request", "message": str(detail)}},
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": {"code": "transient", "message": "Internal server error."}},
    )


# -------------------------------------------------------------- endpoints
@app.get("/api/health")
def api_health():
    return _envelope_ok({
        "status": "ok",
        "version": APP_VERSION,
        "kb_chunks": rag.kb_count(),
        "conversations": history_store.count_conversations(),
        "indexing": _index_in_progress(),
    })


@app.get("/api/config")
def api_config():
    """Runtime config: model bridge + the knobs the ask path will use.

    Extended per design section 10.5 to expose persona.preset and
    retrieval.top_k so the chat path and future clients use saved settings.
    """
    sv = settings_store.view()
    return _envelope_ok({
        "llm_configured": _llm_configured(),
        "model": os.environ.get("RAG_LLM_MODEL", DEFAULT_LLM_MODEL),
        "persona": {"preset": sv["persona"]["preset"]},
        "retrieval": {"top_k": sv["retrieval"]["top_k"]},
    })


def _settings_int(key: str, fallback: int) -> int:
    """Read an integer setting defensively (corrupt rows fall back)."""
    try:
        return int(settings_store.get(key))
    except (TypeError, ValueError):
        return fallback


def _effective_ask_knobs() -> tuple[int, str, str]:
    """Saved ask knobs: (top_k, persona preset, custom instructions).

    Falls back to rag defaults when the settings row is missing/corrupt.
    """
    sv = settings_store.view()
    return sv["retrieval"]["top_k"], sv["persona"]["preset"], sv["persona"]["custom"]


@app.get("/api/settings")
def api_settings_get():
    """Return saved settings. The API key is only exposed as has_key.

    Data shape (design contract section 10):
      {model: {name, base_url, api_key: {has_key}},
       persona: {preset, custom},
       retrieval: {top_k, chunk_size},
       appearance: {theme, language},
       about: {version, docs, chunks, conversations, server_ok}}
    """
    data = settings_store.view()
    data["about"] = {
        "version": APP_VERSION,
        "docs": len(rag.kb_sources()),
        "chunks": rag.kb_count(),
        "conversations": history_store.count_conversations(),
        "server_ok": True,
    }
    return _envelope_ok(data)


@app.put("/api/settings")
def api_settings_put(body: dict):
    """Validate and persist settings (same shape as GET minus `about`).

    Partial saves are allowed: sections omitted from the body keep their
    current value. An empty/absent api_key keeps the existing key. The raw
    key is never returned — the response carries api_key.has_key only.
    """
    if not isinstance(body, dict):
        raise _err("bad-request", "Request body must be a JSON object.", 400)
    normalized, errors = validate_payload(body)
    if errors:
        raise _err("bad-request", "; ".join(errors), 400)
    settings_store.save(normalized)
    _sync_env_from_settings()
    return _envelope_ok(settings_store.view())


TEST_CONNECT_TIMEOUT_SECONDS = 10.0


def _resolve_test_config(body: dict) -> tuple[str, str, str]:
    """Merge staged test values with saved settings.

    Body is the flat staged shape from the Model form: {name, base_url?,
    api_key?}. Missing fields fall back to the saved settings (a user who
    only edits the model name can still test with the existing key/base).
    Returns (name, base_url, api_key). Raises HTTPException on validation
    errors — the raw key never appears in any message.
    """
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _err("bad-request", "model.name is required and must be non-empty.", 400)
    name = name.strip()
    if len(name) > MAX_MODEL_NAME_CHARS:
        raise _err("bad-request", f"model.name must be at most {MAX_MODEL_NAME_CHARS} characters.", 400)

    base_url = body.get("base_url")
    if base_url is None:
        base_url = settings_store.get("model.base_url")
    if not isinstance(base_url, str):
        raise _err("bad-request", "model.base_url must be a string.", 400)
    base_url = base_url.strip()
    if len(base_url) > MAX_BASE_URL_CHARS:
        raise _err("bad-request", f"model.base_url must be at most {MAX_BASE_URL_CHARS} characters.", 400)
    if base_url:
        from settings import _is_http_url

        if not _is_http_url(base_url):
            raise _err("bad-request", "model.base_url must be an http(s) URL.", 400)

    api_key = body.get("api_key")
    if api_key is None:
        api_key = settings_store.get("model.api_key")
    if not isinstance(api_key, str):
        raise _err("bad-request", "model.api_key must be a string.", 400)
    api_key = api_key.strip()
    if len(api_key) > MAX_API_KEY_CHARS:
        raise _err("bad-request", f"model.api_key must be at most {MAX_API_KEY_CHARS} characters.", 400)
    if not api_key:
        raise _err("llm-not-configured", "No API key set — add one or save a key first.", 503)

    return name, base_url, api_key


def _test_error_message(exc: Exception) -> str:
    """Map an LLM/connection error to a sanitized, key-free message."""
    try:
        status = getattr(exc, "status_code", None)
        if status == 401:
            return "Authentication failed (401) — check the API key."
        if status == 404:
            return "Model or endpoint not found (404) — check the model name and base URL."
        if status == 429:
            return "Rate limited (429) — try again shortly."
        if status is not None:
            return f"Provider error ({status})."
    except Exception:  # noqa: BLE001 - never crash while sanitizing
        pass
    return "Connection failed — check the base URL and network access."


@app.post("/api/settings/test")
def api_settings_test(body: dict):
    """Test a staged LLM connection WITHOUT saving anything.

    Body (flat staged shape from the Model form): {name, base_url?,
    api_key?}. Missing fields fall back to saved settings. Performs one tiny
    chat completion against the provider with a 10s timeout and returns the
    latency. Never persists; never returns or logs the API key.

    Response: {ok: true, data: {latency_ms, model}} on success, or
    {ok: false, error: {code: "connection-failed"|"bad-request"|...}}.
    """
    if not isinstance(body, dict):
        raise _err("bad-request", "Request body must be a JSON object.", 400)

    name, base_url, api_key = _resolve_test_config(body)

    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=TEST_CONNECT_TIMEOUT_SECONDS)
    started = time.monotonic()
    try:
        client.chat.completions.create(
            model=name,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
    except Exception as exc:  # noqa: BLE001 - connection errors map to connection-failed
        traceback.print_exc()
        raise _err("connection-failed", _test_error_message(exc), 502)
    latency_ms = int((time.monotonic() - started) * 1000)

    return _envelope_ok({"latency_ms": latency_ms, "model": name})


@app.get("/api/sources")
def api_sources():
    return _envelope_ok({"sources": _sources_view(), "indexing": _index_in_progress()})


@app.get("/api/index/status")
def api_index_status():
    return _envelope_ok({
        "status": _index_state["status"],
        "started_at": _index_state["started_at"],
        "finished_at": _index_state["finished_at"],
        "result": _index_state["result"],
        "error": _index_state["error"],
    })


@app.post("/api/upload")
async def api_upload(files: list[UploadFile] = File(...)):
    """Store uploaded PDF/TXT files into the docs dir.

    Per-file results are returned inline (a rejected file never fails the
    whole batch): {name, size, status: "ready"|"rejected", error?}.
    """
    if not files:
        raise _err("bad-request", "No files provided.", 400)
    if len(files) > MAX_FILES_PER_BATCH:
        raise _err(
            "batch-too-large",
            f"Batch limit is {MAX_FILES_PER_BATCH} files per request.",
            400,
        )

    results: list[dict] = []
    seen: set[str] = set()
    docs_dir = _docs_dir()
    for f in files:
        name = _safe_name(f.filename)
        if not name:
            results.append({"name": "", "size": 0, "status": "rejected", "error": "bad-request"})
            continue
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            results.append({
                "name": name, "size": 0, "status": "rejected",
                "error": "unsupported-type",
            })
            continue
        if name in seen:
            results.append({
                "name": name, "size": 0, "status": "rejected",
                "error": "dup-name",
            })
            continue
        seen.add(name)

        size = 0
        over = False
        dest = docs_dir / name
        tmp = dest.with_suffix(dest.suffix + ".uploading")
        try:
            with tmp.open("wb") as out:
                while True:
                    chunk = await f.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        over = True
                        break
                    out.write(chunk)
            if over:
                tmp.unlink(missing_ok=True)
                results.append({
                    "name": name, "size": size, "status": "rejected",
                    "error": "size-cap",
                })
                continue
            tmp.replace(dest)  # atomic: no partial files survive a failure
            results.append({"name": name, "size": size, "status": "ready", "error": None})
        except Exception:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            results.append({
                "name": name, "size": size, "status": "rejected",
                "error": "transient",
            })

    return _envelope_ok({"results": results, "indexed": False})


@app.post("/api/index")
def api_index():
    """Rebuild the whole knowledge base from the docs dir (synchronous)."""
    if not _INDEX_LOCK.acquire(blocking=False):
        raise _err("indexing-in-progress", "An index rebuild is already running.", 409)
    try:
        result = _run_index_locked()
    finally:
        _INDEX_LOCK.release()
    return _envelope_ok({"indexed": True, "sources": _sources_view(), "result": result})


@app.delete("/api/sources/{doc_id}")
def api_remove_source(doc_id: str):
    """Remove a source file and reindex the remaining documents."""
    if not doc_id or not re.match(r"^[A-Za-z0-9._ -]{1,120}$", doc_id):
        raise _err("not-found", "Source not found.", 404)

    docs_dir = _docs_dir()
    file_to_remove = None
    for suffix in (".pdf", ".txt"):
        candidate = docs_dir / f"{doc_id}{suffix}"
        if candidate.exists():
            file_to_remove = candidate
            break

    kb_ids = {s["id"] for s in rag.kb_sources()}
    if file_to_remove is None and doc_id not in kb_ids:
        raise _err("not-found", "Source not found.", 404)

    if file_to_remove is not None:
        file_to_remove.unlink(missing_ok=True)

    if not _INDEX_LOCK.acquire(blocking=False):
        raise _err("indexing-in-progress", "An index rebuild is already running.", 409)
    try:
        result = _run_index_locked()
    finally:
        _INDEX_LOCK.release()

    return _envelope_ok({"removed": doc_id, "sources": _sources_view(), "result": result})


@app.post("/api/ask")
def api_ask(body: dict):
    """Answer a question with citations; persist it to conversation history.

    Body: {conversation_id?: string, question: string}
    Response data: {conversation_id, answer, sources: [{n, title, page,
    score, snippet, doc_id}], created_at}
    """
    question = (body or {}).get("question")
    if not isinstance(question, str) or not question.strip():
        raise _err("bad-request", "`question` is required and must be non-empty.", 400)
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        raise _err("bad-request", f"`question` must be at most {MAX_QUESTION_CHARS} characters.", 400)

    if _index_in_progress():
        raise _err("indexing-in-progress", "Knowledge base is being rebuilt. Try again shortly.", 409)

    conversation_id = body.get("conversation_id")
    if conversation_id is not None:
        if not isinstance(conversation_id, str) or not re.match(CONVERSATION_ID_RE, conversation_id):
            raise _err("bad-request", "`conversation_id` must match c_[A-Za-z0-9]{8,64}.", 400)
        if not history_store.exists(conversation_id):
            raise _err("not-found", "Conversation not found.", 404)

    if rag.kb_count() == 0:
        raise _err("kb-empty", "Knowledge base is empty. Upload and index documents first.", 409)

    if not _llm_configured():
        raise _err("llm-not-configured", "OPENAI_API_KEY is not configured.", 503)

    try:
        top_k, preset, custom = _effective_ask_knobs()
        result = rag.answer_question(question, k=top_k, preset=preset, custom=custom)
    except RagError as exc:
        raise _err(exc.code, str(exc), exc.status)
    except Exception as exc:  # noqa: BLE001 - LLM/network failures are transient
        traceback.print_exc()
        raise _err("transient", "Answer generation failed. Please try again.", 502)

    answer = result["answer"]
    sources = result["sources"]

    if conversation_id is None:
        title = question[:80]
        conversation = history_store.create_conversation(title)
        conversation_id = conversation["id"]
    history_store.append_message(conversation_id, "user", question)
    history_store.append_message(conversation_id, "assistant", answer, sources=sources)

    return _envelope_ok({
        "conversation_id": conversation_id,
        "answer": answer,
        "sources": sources,
        "created_at": _now_utc(),
    })


@app.get("/api/history")
def api_history():
    return _envelope_ok({"conversations": history_store.list_conversations()})


@app.get("/api/history/{conversation_id}")
def api_history_conversation(conversation_id: str):
    if not re.match(CONVERSATION_ID_RE, conversation_id):
        raise _err("not-found", "Conversation not found.", 404)
    conversation = history_store.get_conversation(conversation_id)
    if conversation is None:
        raise _err("not-found", "Conversation not found.", 404)
    return _envelope_ok({"conversation": conversation})


# ------------------------------------------------------------ static files
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index():
    if (STATIC_DIR / "index.html").exists():
        return FileResponse(str(STATIC_DIR / "index.html"))
    return JSONResponse({
        "ok": True,
        "data": {
            "name": "query-your-docs API",
            "version": APP_VERSION,
            "docs": "/docs",
            "message": "Frontend not built yet (see Step C). API is live.",
        },
    })


def main() -> None:
    import uvicorn

    host = os.environ.get("QYD_HOST", "127.0.0.1")
    print(f"query-your-docs server on http://{host}:{DEFAULT_PORT} (API docs at /docs)")
    uvicorn.run(app, host=host, port=DEFAULT_PORT, log_level="info")


if __name__ == "__main__":
    main()
