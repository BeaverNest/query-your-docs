#!/usr/bin/env python3
"""Local embedding module: intfloat/multilingual-e5-small via ONNX Runtime.

Torch-free, no API cost. Uses the official ONNX export from the
Hugging Face repo (see scripts/download_model.py to fetch it).

E5 prefix convention (from the model card):
  - query    -> "query: "
  - document -> "passage: "
"""
from __future__ import annotations

import os

import numpy as np

MODEL_DIR = os.environ.get(
    "RAG_MODEL_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "intfloat", "multilingual-e5-small", "onnx"),
)
MODEL_ONNX = os.path.join(MODEL_DIR, "model.onnx")
TOKENIZER_JSON = os.path.join(MODEL_DIR, "tokenizer.json")

EMBED_DIM = 384
MAX_LEN = 512
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "


class E5Embedder:
    """Lazy-loaded embedder (ONNX model is loaded once per process)."""

    def __init__(self) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        if not os.path.exists(MODEL_ONNX) or not os.path.exists(TOKENIZER_JSON):
            raise FileNotFoundError(
                f"ONNX model not found in {MODEL_DIR}. "
                "Run `python scripts/download_model.py` or set RAG_MODEL_DIR."
            )
        self.tokenizer = Tokenizer.from_file(TOKENIZER_JSON)
        self.tokenizer.enable_truncation(max_length=MAX_LEN)
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        self.session = ort.InferenceSession(MODEL_ONNX, providers=["CPUExecutionProvider"])
        self.input_names = [i.name for i in self.session.get_inputs()]

    def _encode(self, texts: list[str]) -> tuple[np.ndarray, np.ndarray]:
        enc = self.tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        return ids, mask

    def embed(self, texts: list[str], prefix: str = PASSAGE_PREFIX) -> np.ndarray:
        """Mean-pool + L2 normalize. Returns float32 [N, EMBED_DIM]."""
        if not texts:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        ids, mask = self._encode([prefix + t for t in texts])
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.input_names:
            feed["token_type_ids"] = np.zeros_like(ids)
        out = self.session.run(None, feed)
        hidden = out[0]  # [N, seq, dim]
        mask3 = mask.astype(np.float32)[:, :, None]
        summed = np.sum(hidden * mask3, axis=1)
        count = np.clip(np.sum(mask3, axis=1), 1.0, None)
        pooled = summed / count
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        norms = np.clip(norms, 1e-9, None)
        return (pooled / norms).astype(np.float32)

    def embed_query(self, query: str) -> np.ndarray:
        return self.embed([query], prefix=QUERY_PREFIX)[0]

    def embed_docs(self, texts: list[str]) -> np.ndarray:
        return self.embed(texts, prefix=PASSAGE_PREFIX)


if __name__ == "__main__":
    import time

    t0 = time.time()
    e = E5Embedder()
    print(f"model loaded in {time.time() - t0:.1f}s; inputs={e.input_names}")
    v = e.embed(["Ini kalimat uji bahasa Indonesia.", "This is an English test sentence."])
    q = e.embed_query("cari kalimat uji bahasa Indonesia")
    print(f"shape={v.shape} dtype={v.dtype}")
    print(f"cos(query, id-kalimat)={float(np.dot(q, v[0])):.4f}  cos(query, en-kalimat)={float(np.dot(q, v[1])):.4f}")
    assert v.shape[1] == EMBED_DIM
    print("EMBED_SMOKE_OK")