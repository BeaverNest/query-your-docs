# query-your-docs — Local RAG Document Intelligence

![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)
![Citations](https://img.shields.io/badge/answers-with%20citations-4caf50?style=flat-square)
![Local AI](https://img.shields.io/badge/local--first-no%20cloud-4caf50?style=flat-square)
![Multilingual](https://img.shields.io/badge/multilingual-ID%20%7C%20EN-2196f3?style=flat-square)

Turn business documents into a searchable knowledge base and ask questions
in natural language — every answer comes with **citations** to the source
document and page. Runs fully on your own machine: local embeddings, SQLite
vector store, and an OpenAI-compatible LLM of your choice.

Built as a portfolio demo for AI document intelligence: PDF → text → chunks →
embeddings → Q&A with citations.

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Quickstart](#quickstart)
- [Usage](#usage)
- [Web UI](#web-ui)
- [Web API](#web-api)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## Features

- **Local embeddings, zero API cost** — `intfloat/multilingual-e5-small`
  (384-dim, 512-token context) via ONNX Runtime. No PyTorch, no GPU needed.
- **Multilingual out of the box** — e5-small is a multilingual model; the
  sample documents below are Indonesian business reports and questions can be
  asked in Indonesian **or** English (see the examples in
  [Usage](#usage)).
- **SQLite vector store** — no external database. Cosine similarity over
  stored embeddings, top-k retrieval.
- **Citations, not hallucinations** — the LLM is instructed to answer only
  from the retrieved context and to cite sources with `[n]` markers; a
  per-answer source list is printed with document, page and relevance score.
- **Web API with history** — `server.py` (FastAPI) exposes upload, index,
  ask-with-citations and conversation-history endpoints for a
  NotebookLM-style web UI (see [API.md](API.md)).

## Architecture

```
 PDF / TXT docs
      │  pdftotext (poppler)
      ▼
 per-page text ──► sentence splitter ──► chunks (~600 tokens, 120 overlap)
                                              │
                                              ▼
                 E5Embedder (ONNX, multilingual-e5-small)
                 "passage: ..."  /  "query: ..."
                                              │
                                              ▼
                                       SQLite (data/kb.db)
                                              │
   "ask 'What was the July 2026 PMI?'" ──► query vector ──┤ cosine top-k
                                              ▼
                        context [1..k] + question
                                              │  OpenAI-compatible API
                                              ▼
                         answer with citations [1..n] + source list
```

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Fetch the ONNX model (~30 MB) from Hugging Face
python scripts/download_model.py
```

Optional: if `pdftotext` is not installed, install poppler-utils
(`apt install poppler-utils` on Debian/Ubuntu, `brew install poppler` on macOS).

Configure your LLM — copy `.env.example` to `.env` and set credentials (any
OpenAI-compatible API works):

```bash
cp .env.example .env
# OPENAI_API_KEY=sk-...
# OPENAI_BASE_URL=            # optional, for non-OpenAI endpoints
# RAG_LLM_MODEL=              # optional, default deepseek-v4-flash
```

`rag.py` also accepts environment variables, so you can export them instead
of using `.env`. Then index your documents and ask your first question — see
[Usage](#usage).

## Usage

### 1. Build the knowledge base

```bash
# index a directory of PDFs (default: data/docs)
python rag.py index data/docs

# or a single PDF
python rag.py index some-report.pdf
```

Output:

```
  indexed laporan_makroekonomi_indonesia_2026-08.pdf: 18 pages, 24 chunks
  indexed laporan_pmi_manufaktur_indonesia_2026-07.pdf: 6 pages, 9 chunks
  indexed riset_industri_otomotif_indonesia_2026-08.pdf: 21 pages, 40 chunks
INDEX_OK: 3 docs, 73 chunks -> data/kb.db
```

### 2. Ask questions

**Works with Indonesian documents too** — the sample reports below are
Indonesian business reports, and you can ask in Indonesian or English.

Ask in Indonesian:

```bash
python rag.py ask "Berapa PMI manufaktur Indonesia pada Juli 2026 dan bagaimana trennya?"
```

```
Question: Berapa PMI manufaktur Indonesia pada Juli 2026 dan bagaimana trennya?

Answer: PMI Manufaktur Indonesia pada Juli 2026 adalah **50,2**, naik dari
**46,9** pada Juni 2026 [1]. Angka ini menandakan kembali ke zona ekspansi
tipis setelah sebelumnya kontraksi [1]. Pemulihan didorong stabilisasi new
orders, kenaikan output pertama sejak Februari, dan rekrutmen karyawan
pertama dalam 5 bulan [1]. Namun, pesanan ekspor masih turun bulan kelima,
biaya input tetap tinggi, dan harga jual naik tajam [1]. Optimisme pelaku
usaha tercatat tertinggi sejak Januari [1].

Sources:
  [1] Laporan Riset: PMI Manufaktur Indonesia (page 1, score 0.911)
  [2] Laporan Riset: PMI Manufaktur Indonesia (page 5, score 0.889)
  [3] riset industri otomotif indonesia 2026-08 (page 4, score 0.867)
  [4] Laporan Riset: PMI Manufaktur Indonesia (page 5, score 0.865)
```

The same question, in English — same documents, same retrieval, answer in
the language of the question:

```bash
python rag.py ask "What was Indonesia's manufacturing PMI in July 2026 and how is the trend?"
```

```
Question: What was Indonesia's manufacturing PMI in July 2026 and how is the trend?

Answer: Indonesia's manufacturing PMI in July 2026 was 50.2, returning to a
thin expansion zone (above the neutral 50 line) [1]. This rose from 46.9 in
June, which was the worst contraction in a year [1]. The recovery was led by
stabilized new orders and increased output (the first rise since February),
plus the first hiring in five months [1]. However, export orders fell for a
fifth consecutive month, input costs remained high (though easing), and
selling prices rose sharply [1]. Business optimism reached its highest level
since January [1].

Sources:
  [1] Laporan Riset: PMI Manufaktur Indonesia (page 1, score 0.883)
  [2] Laporan Riset: PMI Manufaktur Indonesia (page 5, score 0.868)
  [3] Laporan Riset: PMI Manufaktur Indonesia (page 5, score 0.854)
  [4] riset industri otomotif indonesia 2026-08 (page 4, score 0.846)
```

Another cross-document retrieval example — the automotive sector report:

```bash
python rag.py ask "Bagaimana kondisi industri otomotif Indonesia menurut riset terbaru?"
```

```
Question: Bagaimana kondisi industri otomotif Indonesia menurut riset terbaru?

Answer: Berdasarkan riset terbaru (per 14 Agustus 2026), industri otomotif
Indonesia merupakan salah satu ekosistem manufaktur terbesar di ASEAN,
berperan sebagai basis produksi right-hand drive untuk ekspor ke puluhan
negara dengan pasar domestik ~280 juta penduduk [1][2]. Penjualan mobil 2025
turun ke 803.687 unit (-7,2% yoy), terendah sejak 2021, dengan target
Gaikindo 2026 sebesar 850.000 unit (+5,8%) [1][3]. Produksi 2025 diperkirakan
1,147 juta unit, lebih besar dari penjualan domestik, menegaskan peran
ekspor regional [3]. Utilisasi pabrik sekitar 60–75% dari kapasitas 1,5–2,0
juta unit [3]. Hilirisasi nikel memperkuat posisi Indonesia sebagai calon
pemain penting rantai pasok baterai EV global [1].

Sources:
  [1] riset industri otomotif indonesia 2026-08 (page 3, score 0.877)
  [2] riset industri otomotif indonesia 2026-08 (page 3, score 0.866)
  [3] riset industri otomotif indonesia 2026-08 (page 4, score 0.860)
  [4] riset industri otomotif indonesia 2026-08 (page 1, score 0.859)
```

If the knowledge base does not contain the answer, the assistant says so
instead of inventing facts:

```bash
python rag.py ask "Berapa harga penawaran umum saham GoTo saat IPO?"
```

```
Answer: Berdasarkan konteks yang diberikan, tidak ada informasi mengenai
harga penawaran umum saham GoTo saat IPO. ... Tidak ada data tentang GoTo
atau IPO-nya dalam dokumen ini.
```

### 3. Inspect the KB

```bash
python rag.py stats
```

```text
DB: data/kb.db
total chunks: 73
  laporan_makroekonomi_indonesia_2026-08: 24 chunks across 18 pages
  laporan_pmi_manufaktur_indonesia_2026-07: 9 chunks across 6 pages
  riset_industri_otomotif_indonesia_2026-08: 40 chunks across 21 pages
```

## Web UI

Start the server and open <http://127.0.0.1:8000/> in your browser:

```bash
python server.py
# query-your-docs web UI + API on http://127.0.0.1:8000 (API docs at /docs)
```

The NotebookLM-style interface lets you:

- **Upload multiple documents** (PDF/TXT) and index them into one knowledge base.
- **Chat in natural language** (Indonesian or English) and get answers with
  **citation chips** — click a chip to see the exact source document, page
  and relevance score.
- **Keep history** — conversations persist across restarts in the sidebar,
  so you can pick up where you left off.
- **Configure everything in Settings** — the gear-icon drawer manages the
  LLM (model name, base URL, masked API key with live test), answer persona
  presets (concise / detailed / beginner / Indonesian + custom), retrieval
  knobs (top-k, chunk size) and appearance (theme, UI language). Changes are
  saved with an explicit **Save** / **Discard** and apply immediately.

The UI talks to the same pipeline as the CLI: local embeddings, SQLite vector
store, and any OpenAI-compatible LLM you configure in `.env`.

## Web API

Start the FastAPI server (serves the web UI and the JSON API):

```bash
python server.py
# query-your-docs server on http://127.0.0.1:8000 (API docs at /docs)
```

Endpoints:

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/upload` | store uploaded `.pdf`/`.txt` files (multipart, multi-file) |
| POST | `/api/index` | rebuild the whole knowledge base (sync) |
| GET | `/api/sources` | list sources (ready / pending / error) |
| DELETE | `/api/sources/{id}` | remove a source and reindex the rest |
| POST | `/api/ask` | answer with citations; persists to history |
| GET | `/api/history` | list conversations |
| GET | `/api/history/{id}` | load one conversation's messages |
| GET | `/api/config` | `{llm_configured, model}` |
| GET | `/api/settings` | saved settings (API key returned only as `has_key`) |
| PUT | `/api/settings` | persist settings (model, persona, retrieval, appearance) |
| POST | `/api/settings/test` | test a staged model connection (never saves) |
| GET | `/api/health` | status, chunk count, conversation count |

The full contract — response envelopes, error codes, limits — lives in
[API.md](API.md). Every reindex rebuilds the whole KB (the pipeline's
design), so uploading one document means reindexing all of them.

Optional environment variables for the server: `QYD_PORT` (default `8000`),
`QYD_HOST` (default `127.0.0.1`), `QYD_DOCS_DIR`, `QYD_HISTORY_DB`,
`RAG_DB`. Smoke-test the API end-to-end (throwaway data, never touches your
real KB):

```bash
python scripts/smoke_api.py
```

Browser smoke for the web UI (Playwright; throws away its own temp docs/KB/
history, requires `OPENAI_API_KEY` for the live ask step):

```bash
.venv/bin/python scripts/smoke_frontend.py
```

## Project structure

```
rag.py                     CLI + library: index / ask / stats / answer_question
embed.py                   e5-small ONNX embedder (passage/query prefixes)
server.py                  FastAPI web server (upload, index, ask, history)
history.py                 SQLite conversation store (data/history.db)
API.md                     web API contract
scripts/download_model.py  fetch the ONNX model from Hugging Face
scripts/smoke_api.py       end-to-end API smoke test (throwaway data)
scripts/smoke_frontend.py  browser smoke test for the web UI (Playwright)
requirements.txt           Python dependencies
.env.example               config template (copy to .env)
static/                    web UI assets (index.html, style.css, app.js)
data/docs/                 your PDFs (gitignored)
models/                    downloaded model (gitignored)
```

## Troubleshooting

- **`pdftotext` not found when indexing** — install poppler-utils
  (`apt install poppler-utils` on Debian/Ubuntu, `brew install poppler` on
  macOS).
- **Model download fails** — check your connection to Hugging Face; for
  stricter rate limits set `HF_TOKEN` and re-run
  `python scripts/download_model.py`.
- **Empty answer or API errors** — verify `OPENAI_API_KEY` in `.env` (and
  `OPENAI_BASE_URL` / `RAG_LLM_MODEL` if you use a non-default endpoint).

## Notes

- Chunking is sentence-based, ~600 tokens per chunk with a ~120-token
  overlap (tunable via `--chunk-tokens` / `--overlap-tokens` on `index`).
- Retrieval uses cosine similarity; scores are printed per source so you can
  judge confidence.
- `data/`, `models/` and `.env` are gitignored — the repo contains only code.

## License

MIT — see [LICENSE](LICENSE). Part of the
[BeaverNest](https://github.com/BeaverNest) open-source portfolio.
