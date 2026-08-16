# query-your-docs — Web API Contract

FastAPI backend for the NotebookLM-style web UI. Served by `server.py`
(default `http://127.0.0.1:8000`, port via `QYD_PORT`). Interactive docs:
`GET /docs`.

## Conventions

- **Envelope.** Success: `{ok: true, data: {...}}`. Failure:
  `{ok: false, error: {code, message}}` with a meaningful HTTP status.
- **Error codes.** `bad-request`, `not-found`, `unsupported-type`,
  `size-cap`, `batch-too-large`, `dup-name`, `no-text-extracted`,
  `indexing-in-progress`, `kb-empty`, `llm-not-configured`,
  `connection-failed`, `transient`.
- **Secrets.** The API key is read from `.env` / environment and is never
  returned by any endpoint or error.
- **Time.** All timestamps are UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SS+00:00`).
- **Index model.** The KB is whole-library: every `POST /api/index` (and
  every source removal) deletes all chunks and re-embeds every document in
  the docs dir. Uploading one doc means reindexing all docs.

## Limits

- Upload: `.pdf` / `.txt` only; 50 MB per file; 20 files per request.
- Duplicate filenames in one request are rejected (`dup-name`).
- Question: max 4000 characters.
- Conversation id: `c_` + 8–64 `[A-Za-z0-9]`.

---

## GET /api/health

```json
{"ok": true, "data": {"status": "ok", "version": "0.1.0",
  "kb_chunks": 73, "conversations": 2, "indexing": false}}
```

## GET /api/config

```json
{"ok": true, "data": {"llm_configured": true, "model": "deepseek-v4-flash",
  "persona": {"preset": "concise"}, "retrieval": {"top_k": 4}}}
```

`persona.preset` and `retrieval.top_k` reflect saved settings so the chat
path (and any client) can use the same knobs as `/api/ask`.

## GET /api/settings

Returns saved settings. **The API key is never returned** — only
`api_key.has_key`. Persisted in SQLite (`data/settings.db`, `QYD_SETTINGS_DB`
to override); defaults mirror the env config.

```json
{"ok": true, "data": {
  "model": {"name": "deepseek-v4-flash", "base_url": "",
            "api_key": {"has_key": true}},
  "persona": {"preset": "concise", "custom": ""},
  "retrieval": {"top_k": 4, "chunk_size": 600},
  "appearance": {"theme": "light", "language": "en"},
  "about": {"version": "0.1.0", "docs": 3, "chunks": 73,
            "conversations": 2, "server_ok": true}
}}
```

- `persona.preset` ∈ `concise | detailed | beginner | indonesian`;
  `custom` is additive instructions (≤2000 chars).
- `appearance.theme` ∈ `dark | light | system`; `appearance.language` ∈ `en | id`.
- `chunk_size` applies on the **next index** (whole-library rebuild), not hot.

## PUT /api/settings

Accepts the same shape as GET **minus `about`**; returns the saved settings
(key as `has_key` only). Partial saves are allowed — sections omitted from
the body keep their current value. An empty/absent `api_key` keeps the
existing key.

Validation (400 `bad-request` with a joined message):

- `model.name` — required, non-empty, ≤120 chars.
- `model.base_url` — optional; when set must parse as `http(s)://` URL.
- `persona.preset` — one of the four preset ids.
- `persona.custom` — optional, ≤2000 chars.
- `retrieval.top_k` — integer 1–10 (default 4).
- `retrieval.chunk_size` — integer 100–2000 (default 600).
- `appearance.theme` / `appearance.language` — allowed values above.

Example:

```json
{"model": {"name": "gpt-4o-mini", "base_url": "https://api.openai.com/v1",
           "api_key": "sk-..."},
 "persona": {"preset": "detailed", "custom": "Always cite page numbers."},
 "retrieval": {"top_k": 6, "chunk_size": 800},
 "appearance": {"theme": "system", "language": "id"}}
```

Saved model values (name/base_url/api_key) are applied to the process
environment, so `/api/ask` uses them immediately and after restart.

## POST /api/settings/test

Tests a **staged** LLM connection without saving anything. Body is the flat
shape from the Model form; missing fields fall back to saved settings (so a
user who only edits the model name can test with the existing key/base):

```json
{"name": "gpt-4o-mini", "base_url": "https://api.openai.com/v1",
 "api_key": "sk-..."}
```

```json
{"ok": true, "data": {"latency_ms": 842, "model": "gpt-4o-mini"}}
```

- Performs one tiny chat completion (`max_tokens=1`) with a 10s timeout.
- `latency_ms` is the round-trip in milliseconds.
- **Never persists anything** — the settings store is untouched.
- **Never returns or logs the API key.**

Errors (same envelope):

- 400 `bad-request` — model name missing/empty or >120 chars, bad base URL,
  oversized fields.
- 503 `llm-not-configured` — no API key staged and none saved.
- 502 `connection-failed` — provider/auth/network failure (message is
  sanitized: never contains the key).

## GET /api/sources

```json
{"ok": true, "data": {"indexing": false, "sources": [
  {"id": "laporan_pmi", "title": "Laporan PMI ...", "pages": 6,
   "chunks": 9, "status": "ready", "error": null},
  {"id": "uploaded_but_not_indexed", "title": "uploaded but not indexed",
   "pages": 3, "chunks": 0, "status": "pending", "error": null}
]}}
```

`status`: `ready` (in KB), `pending` (uploaded, not yet indexed),
`error` (failed at last index, e.g. `no-text-extracted`).

## POST /api/upload

Multipart form, field name `files`, multiple files.

```json
{"ok": true, "data": {"indexed": false, "results": [
  {"name": "laporan.pdf", "size": 269289, "status": "ready", "error": null},
  {"name": "notes.xlsx", "size": 0, "status": "rejected", "error": "unsupported-type"},
  {"name": "big.pdf", "size": 52428801, "status": "rejected", "error": "size-cap"}
]}}
```

Per-file rejections never fail the batch. Then call `POST /api/index`.

## POST /api/index

Synchronous rebuild of the whole KB from the docs dir.

```json
{"ok": true, "data": {"indexed": true, "sources": [ ...same as /api/sources... ],
  "result": {"docs_indexed": 3, "docs_total": 3, "chunks": 73,
    "per_doc": [{"name": "laporan_pmi.pdf", "pages": 6, "chunks": 9,
      "status": "ok", "error": null}]}}}
```

409 `indexing-in-progress` if another rebuild is running.

## GET /api/index/status

```json
{"ok": true, "data": {"status": "idle", "started_at": null,
  "finished_at": "...", "result": { ...last index summary... }, "error": null}}
```

## DELETE /api/sources/{doc_id}

Removes the file and reindexes the remaining documents.

```json
{"ok": true, "data": {"removed": "old_doc", "sources": [ ... ]}}
```

404 `not-found` if neither the file nor the KB entry exists.

## POST /api/ask

```json
{"conversation_id": "c_1a2b...", "question": "What does the PMI report say about manufacturing?"}
```

- `conversation_id` optional: omit to start a new conversation (title = first
  question, truncated). Provide an existing id to continue it (404 if missing).
- Retrieval and persona come from saved settings: `retrieval.top_k` chunks are
  retrieved and the active `persona.preset` (+ additive `custom`) is used as
  the system prompt. Defaults: top_k=4, preset=`concise` (byte-identical to
  the historical hardcoded prompt).
- Response: `n` in each source matches the `[n]` citation markers in `answer`.

```json
{"ok": true, "data": {
  "conversation_id": "c_1a2b...",
  "answer": "Manufacturing PMI rose to 51.2 in July [1]. ...",
  "sources": [
    {"n": 1, "doc_id": "laporan_pmi", "title": "Laporan PMI Manufaktur Indonesia 2026-07",
     "page": 1, "chunk_idx": 0, "score": 0.8231,
     "snippet": "Indeks PMI Manufaktur Indonesia pada Juli 2026 tercatat 51,2 ..."}
  ],
  "created_at": "2026-08-16T12:00:00+00:00"
}}
```

Errors: 400 `bad-request` (empty/too-long question, bad conversation_id),
404 `not-found` (unknown conversation), 409 `kb-empty` /
`indexing-in-progress`, 503 `llm-not-configured`, 502 `transient`
(LLM/network failure).

## GET /api/history

```json
{"ok": true, "data": {"conversations": [
  {"id": "c_1a2b...", "title": "What does the PMI report say...",
   "created_at": "...", "updated_at": "...", "message_count": 4}
]}}
```

Sorted by `updated_at` descending.

## GET /api/history/{conversation_id}

```json
{"ok": true, "data": {"conversation": {
  "id": "c_1a2b...", "title": "...", "created_at": "...", "updated_at": "...",
  "messages": [
    {"role": "user", "content": "Question", "created_at": "...", "sources": null},
    {"role": "assistant", "content": "Answer with [1] citations", "created_at": "...",
     "sources": [ {same shape as /api/ask sources} ]}
  ]
}}}
```

## Static / frontend

- `GET /static/*` serves files from `static/` (mounted only if the dir exists).
- `GET /` serves `static/index.html` when present, else a JSON info page.
- Step C (Nara) wires the NotebookLM-style UI to these endpoints.
