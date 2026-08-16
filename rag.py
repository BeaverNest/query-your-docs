#!/usr/bin/env python3
"""rag.py — local RAG document intelligence (docs -> KB -> Q&A with citations).

Pipeline: PDF/TXT -> per-page text -> chunks (with overlap) -> local e5-small
embeddings (ONNX, no API cost) -> SQLite vector store -> cosine top-k retrieval
-> LLM answer with source citations.

Commands:
  python rag.py index <pdf-or-dir>   build/rebuild the knowledge base
  python rag.py ask "question" [-k N]  answer with citations from the KB
  python rag.py stats                 show KB statistics

Environment (or .env next to this file):
  OPENAI_API_KEY   required for `ask` (any OpenAI-compatible API)
  OPENAI_BASE_URL  optional, e.g. https://api.openai.com/v1
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


def index_docs(paths: list[Path]) -> None:
    from tokenizers import Tokenizer
    from embed import TOKENIZER_JSON

    embedder = E5Embedder()
    tokenizer = Tokenizer.from_file(TOKENIZER_JSON)
    tokenizer.enable_truncation(max_length=512)

    con = kb_connect()
    con.execute("DELETE FROM chunks")  # rebuild

    total_chunks = 0
    for pdf in paths:
        pages = pdf_page_count(pdf)
        title = doc_title(pdf)
        doc_id = pdf.stem
        chunk_texts: list[tuple[int, str]] = []  # (page, text)
        for page in range(1, pages + 1):
            text = page_text(pdf, page)
            if not text.strip():
                print(f"  [skip] {pdf.name} page {page}: no text (scanned image?)")
                continue
            for chunk in chunk_text(text, tokenizer):
                chunk_texts.append((page, chunk))
        if not chunk_texts:
            print(f"  [warn] {pdf.name}: no extractable text, document ignored")
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
        print(f"  indexed {pdf.name}: {pages} pages, {len(chunk_texts)} chunks")

    con.commit()
    con.close()
    print(f"INDEX_OK: {len(paths)} docs, {total_chunks} chunks -> {DB_PATH}")


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


def ask(question: str, k: int) -> None:
    import numpy as np
    from openai import OpenAI

    embedder = E5Embedder()
    query_vec = embedder.embed_query(question)
    hits = retrieve(query_vec, k)
    if not hits:
        print("ASK_ERROR: knowledge base is empty. Run `python rag.py index <pdf-or-dir>` first.")
        sys.exit(1)

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
        print("ASK_ERROR: OPENAI_API_KEY not set. Add it to .env or export it.")
        sys.exit(1)
    client = OpenAI(api_key=api_key, base_url=os.environ.get("OPENAI_BASE_URL") or None)
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

    print("Question:", question)
    print()
    print("Answer:", answer)
    print()
    print("Sources:")
    for i, (doc_id, title, page, cidx, text, score) in enumerate(hits, 1):
        print(f"  [{i}] {title} (page {page}, score {score:.3f})")


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
    p_index.add_argument("path", nargs="?", default=str(SCRIPT_DIR / "data" / "docs"), help="PDF file or directory of PDFs")
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