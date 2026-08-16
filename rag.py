#!/usr/bin/env python3
"""rag.py — local RAG document intelligence (docs -> KB -> Q&A with citations).

Pipeline: PDF/TXT -> per-page text -> chunks (with overlap) -> local e5-small
embeddings (ONNX, no API cost) -> SQLite vector store -> cosine top-k retrieval
-> LLM answer with source citations.

Commands:
  python rag.py index <pdf-or-dir>   build/rebuild the knowledge base
  python rag.py ask "question" [-k N]  answer with citations from the KB
  python rag.py stats                 show KB statistics

Library use (imported by server.py):
  rag.index_docs(paths)            -> structured summary dict
  rag.kb_sources()                 -> [{id, title, pages, chunks}]
  rag.kb_count()                   -> total chunk count
  rag.answer_question(q, k)        -> {"answer", "sources":[{n, doc_id, title,
                                     page, chunk_idx, score, snippet}]}

Environment (or .env next to this file):
  OPENAI_API_KEY   required for `ask`/answer_question (any OpenAI-compatible API)
  OPENAI_BASE_URL  optional, e.g. https://api.openai.com/v1
  RAG_LLM_MODEL    optional, LLM model name (default deepseek-v4-flash)
  RAG_MODEL_DIR    optional, ONNX model dir (default ./models/...)
  RAG_DB           optional, SQLite path (default data/kb.db)
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from embed import E5Embedder, EMBED_DIM

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("RAG_DB", SCRIPT_DIR / "data" / "kb.db"))

# Chunking parameters (token counts via the e5 tokenizer).
TARGET_CHUNK_TOKENS = 600
OVERLAP_TOKENS = 120
TOP_K_DEFAULT = 4


class RagError(Exception):
    """Base error for pipeline failures; maps to a JSON error envelope."""

    code = "transient"
    status = 500


class KbEmptyError(RagError):
    code = "kb-empty"
    status = 409


class LlmNotConfiguredError(RagError):
    code = "llm-not-configured"
    status = 503


# ---------------------------------------------------------------- env/.env
def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and os.environ.get(key) is None:
            os.environ[key] = value


# ---------------------------------------------------------------- extraction
def page_text(pdf: Path, page: int) -> str:
    """Extract one page of a PDF via pdftotext (poppler)."""
    try:
        out = subprocess.run(
            ["pdftotext", "-f", str(page), "-l", str(page), str(pdf), "-"],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        sys.exit("ERROR: pdftotext not found. Install poppler-utils (e.g. `apt install poppler-utils`).")
    except subprocess.TimeoutExpired:
        return ""
    return out.stdout or ""


def pdf_page_count(pdf: Path) -> int:
    try:
        out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=60)
        m = re.search(r"^Pages:\s+(\d+)", out.stdout, re.M)
        return int(m.group(1)) if m else 1
    except Exception:
        return 1


def doc_title(pdf: Path) -> str:
    try:
        out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=60)
        m = re.search(r"^Title:\s+(.+)$", out.stdout, re.M)
        if m and m.group(1).strip():
            return m.group(1).strip()
    except Exception:
        pass
    return pdf.stem.replace("_", " ").strip()


def extract_doc_pages(path: Path) -> list[tuple[int, str]]:
    """Return [(page_number, text)] for a PDF or TXT document.

    PDFs use per-page pdftotext extraction; TXT files are read whole as
    page 1. Empty/scanned pages are skipped (the caller decides whether
    the document yielded any text at all).
    """
    if path.suffix.lower() == ".txt":
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [(1, text)] if text.strip() else []
    pages: list[tuple[int, str]] = []
    for page in range(1, pdf_page_count(path) + 1):
        text = page_text(path, page)
        if text.strip():
            pages.append((page, text))
    return pages


def doc_page_count(path: Path) -> int:
    return 1 if path.suffix.lower() == ".txt" else pdf_page_count(path)


# ---------------------------------------------------------------- chunking
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text)]
    return [p for p in parts if p]


def chunk_text(text: str, tokenizer, target: int = TARGET_CHUNK_TOKENS, overlap: int = OVERLAP_TOKENS) -> list[str]:
    """Greedy sentence-level chunking with a token-counted tail overlap."""
    sents = split_sentences(text)
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    i = 0
    while i < len(sents):
        sent = sents[i]
        n = len(tokenizer.encode(sent).ids)
        if n == 0:
            i += 1
            continue
        if current and current_tokens + n > target and current_tokens >= target * 0.5:
            chunks.append(" ".join(current))
            tail: list[str] = []
            tail_tokens = 0
            for t in reversed(current):
                tn = len(tokenizer.encode(t).ids)
                if tail_tokens + tn > overlap:
                    break
                tail.insert(0, t)
                tail_tokens += tn
            current, current_tokens = tail, tail_tokens
        current.append(sent)
        current_tokens += n
        i += 1
    if current:
        chunks.append(" ".join(current))
    return chunks


# ---------------------------------------------------------------- KB
def kb_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT NOT NULL,
            doc_title TEXT NOT NULL,
            page INTEGER NOT NULL,
            chunk_idx INTEGER NOT NULL,
            text TEXT NOT NULL,
            embedding BLOB NOT NULL,
            UNIQUE(doc_id, page, chunk_idx)
        )
        """
    )
    return con


def kb_count() -> int:
    con = kb_connect()
    n = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    con.close()
    return n


def kb_sources() -> list[dict]:
    """List indexed documents with page/chunk counts, ordered by doc_id."""
    con = kb_connect()
    rows = con.execute(
        "SELECT doc_id, doc_title, COUNT(*) AS chunks, COUNT(DISTINCT page) AS pages "
        "FROM chunks GROUP BY doc_id ORDER BY doc_id"
    ).fetchall()
    con.close()
    return [{"id": r[0], "title": r[1], "chunks": r[2], "pages": r[3]} for r in rows]


# ------------------------------------------------------------------ index
def _get_embedder() -> E5Embedder:
    """Lazy singleton so a long-running server does not reload the model per ask."""
    if getattr(_get_embedder, "_cache", None) is None:
        _get_embedder._cache = E5Embedder()
    return _get_embedder._cache


def _get_tokenizer():
    from tokenizers import Tokenizer
    from embed import TOKENIZER_JSON

    if getattr(_get_tokenizer, "_cache", None) is None:
        tok = Tokenizer.from_file(TOKENIZER_JSON)
        tok.enable_truncation(max_length=512)
        _get_tokenizer._cache = tok
    return _get_tokenizer._cache


def index_docs(paths: list[Path]) -> dict:
    """(Re)build the knowledge base from the given PDF/TXT files.

    The KB is a whole-library store: every index run deletes all chunks and
    re-embeds the given documents (uploading one doc means reindexing all).

    Returns a structured summary:
      {docs_indexed, docs_total, chunks, per_doc: [{name, pages, chunks,
        status: "ok"|"error", error?}], db}
    """
    embedder = _get_embedder()
    tokenizer = _get_tokenizer()

    con = kb_connect()
    con.execute("DELETE FROM chunks")  # rebuild

    total_chunks = 0
    per_doc: list[dict] = []
    for path in paths:
        pages_count = doc_page_count(path)
        title = doc_title(path)
        doc_id = path.stem
        chunk_texts: list[tuple[int, str]] = []  # (page, text)
        for page, text in extract_doc_pages(path):
            for chunk in chunk_text(text, tokenizer):
                chunk_texts.append((page, chunk))

        if not chunk_texts:
            per_doc.append({
                "name": path.name,
                "pages": pages_count,
                "chunks": 0,
                "status": "error",
                "error": "no-text-extracted",
            })
            print(f"  [warn] {path.name}: no extractable text, document ignored")
            continue

        batch = 32
        for start in range(0, len(chunk_texts), batch):
            slice_ = chunk_texts[start : start + batch]
            vectors = embedder.embed_docs([c for _, c in slice_])
            for offset, ((page, text), vec) in enumerate(zip(slice_, vectors)):
                con.execute(
                    "INSERT OR REPLACE INTO chunks (doc_id, doc_title, page, chunk_idx, text, embedding) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (doc_id, title, page, start + offset, text, vec.tobytes()),
                )
        total_chunks += len(chunk_texts)
        per_doc.append({
            "name": path.name,
            "pages": pages_count,
            "chunks": len(chunk_texts),
            "status": "ok",
            "error": None,
        })
        print(f"  indexed {path.name}: {pages_count} pages, {len(chunk_texts)} chunks")

    con.commit()
    con.close()
    summary = {
        "docs_indexed": sum(1 for d in per_doc if d["status"] == "ok"),
        "docs_total": len(paths),
        "chunks": total_chunks,
        "per_doc": per_doc,
        "db": str(DB_PATH),
    }
    print(f"INDEX_OK: {summary['docs_indexed']} docs, {total_chunks} chunks -> {DB_PATH}")
    return summary


# --------------------------------------------------------------- retrieval
def retrieve(query_vec, k: int):
    con = kb_connect()
    rows = con.execute("SELECT doc_id, doc_title, page, chunk_idx, text, embedding FROM chunks").fetchall()
    con.close()
    if not rows:
        return []
    import numpy as np

    mat = np.vstack([np.frombuffer(r[5], dtype=np.float32) for r in rows])
    sims = mat @ query_vec
    top = np.argsort(sims)[::-1][:k]
    return [(rows[i][0], rows[i][1], rows[i][2], rows[i][3], rows[i][4], float(sims[i])) for i in top]


# ------------------------------------------------------------------ answer
def answer_question(question: str, k: int = TOP_K_DEFAULT) -> dict:
    """Retrieve top-k chunks and produce an LLM answer with citations.

    Returns {"answer": str, "sources": [{n, doc_id, title, page, chunk_idx,
    score, snippet}]} where `n` matches the [n] markers in `answer`.
    Raises KbEmptyError / LlmNotConfiguredError / RagError.
    """
    import numpy as np
    from openai import OpenAI

    embedder = _get_embedder()
    query_vec = embedder.embed_query(question)
    hits = retrieve(query_vec, k)
    if not hits:
        raise KbEmptyError("Knowledge base is empty. Upload and index documents first.")

    context_parts = []
    for i, (doc_id, title, page, cidx, text, score) in enumerate(hits, 1):
        header = f"[{i}] {title} (page {page})"
        context_parts.append(f"{header}\n{text}")
    context = "\n\n".join(context_parts)

    system = (
        "You are a document-intelligence assistant. Answer the user's question using ONLY "
        "the provided context. Cite the source of every factual claim with its bracketed "
        "reference number, e.g. [1] or [1,2]. If the context does not contain the answer, "
        "say so clearly and do not invent facts. Answer in the language of the question. "
        "Keep the answer concise (max ~120 words)."
    )
    user = f"Question: {question}\n\nContext:\n{context}"

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LlmNotConfiguredError("OPENAI_API_KEY not set. Add it to .env or export it.")
    client = OpenAI(api_key=api_key, base_url=os.environ.get("OPENAI_BASE_URL") or None, timeout=120)
    resp = client.chat.completions.create(
        model=os.environ.get("RAG_LLM_MODEL", "deepseek-v4-flash"),
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        max_tokens=700,
    )
    answer = (resp.choices[0].message.content or "").strip()

    sources = [
        {
            "n": i + 1,
            "doc_id": doc_id,
            "title": title,
            "page": page,
            "chunk_idx": cidx,
            "score": round(float(score), 4),
            "snippet": text[:240],
        }
        for i, (doc_id, title, page, cidx, text, score) in enumerate(hits)
    ]
    return {"answer": answer, "sources": sources}


def ask(question: str, k: int) -> None:
    """CLI wrapper around answer_question (kept for `python rag.py ask`)."""
    try:
        result = answer_question(question, k)
    except RagError as exc:
        print(f"ASK_ERROR: {exc}")
        sys.exit(1)
    print("Question:", question)
    print()
    print("Answer:", result["answer"])
    print()
    print("Sources:")
    for s in result["sources"]:
        print(f"  [{s['n']}] {s['title']} (page {s['page']}, score {s['score']:.3f})")


def stats() -> None:
    con = kb_connect()
    total = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    docs = con.execute("SELECT doc_id, doc_title, COUNT(*), COUNT(DISTINCT page) FROM chunks GROUP BY doc_id ORDER BY doc_id").fetchall()
    con.close()
    print(f"DB: {DB_PATH}")
    print(f"total chunks: {total}")
    for doc_id, title, n, pages in docs:
        print(f"  {doc_id}: {n} chunks across {pages} pages")


# ---------------------------------------------------------------- CLI
def collect_input(path_str: str) -> list[Path]:
    p = Path(path_str)
    if p.is_file():
        return [p]
    if p.is_dir():
        pdfs = sorted([f for f in p.iterdir() if f.suffix.lower() == ".pdf"])
        txts = sorted([f for f in p.iterdir() if f.suffix.lower() == ".txt"])
        if not pdfs and not txts:
            sys.exit(f"ERROR: no PDF/TXT files found in directory {p}")
        return pdfs + txts
    sys.exit(f"ERROR: path not found: {p}")


def main() -> None:
    load_dotenv(SCRIPT_DIR / ".env")
    parser = argparse.ArgumentParser(description="Local RAG document intelligence")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="build/rebuild the knowledge base")
    p_index.add_argument("path", nargs="?", default=str(SCRIPT_DIR / "data" / "docs"), help="PDF/TXT file or directory of PDF/TXT files")
    p_index.add_argument("--chunk-tokens", type=int, default=TARGET_CHUNK_TOKENS)
    p_index.add_argument("--overlap-tokens", type=int, default=OVERLAP_TOKENS)

    p_ask = sub.add_parser("ask", help="answer a question with citations")
    p_ask.add_argument("question")
    p_ask.add_argument("-k", "--top-k", type=int, default=TOP_K_DEFAULT, help="number of chunks to retrieve (default 4)")

    sub.add_parser("stats", help="show KB statistics")

    args = parser.parse_args()

    if args.command == "index":
        index_docs(collect_input(args.path))
    elif args.command == "ask":
        ask(args.question, args.top_k)
    elif args.command == "stats":
        stats()


if __name__ == "__main__":
    main()
