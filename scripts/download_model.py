#!/usr/bin/env python3
"""Fetch the ONNX export of intfloat/multilingual-e5-small from Hugging Face.

Downloads the 6 files used by the embedder into
<repo>/models/intfloat/multilingual-e5-small/onnx/ (override with RAG_MODEL_DIR).

Usage:
    python scripts/download_model.py
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

REPO = "intfloat/multilingual-e5-small"
FILES = [
    "config.json",
    "model.onnx",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
]


def main() -> int:
    default_dest = Path(__file__).resolve().parents[1] / "models" / "intfloat" / "multilingual-e5-small" / "onnx"
    dest = Path(os.environ.get("RAG_MODEL_DIR", str(default_dest)))

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("ERROR: huggingface_hub is not installed. Run: pip install huggingface_hub")
        return 1

    dest.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        cached = hf_hub_download(repo_id=REPO, filename=f"onnx/{name}")
        shutil.copy2(cached, dest / name)
        print(f"downloaded onnx/{name}")
    print(f"MODEL_DOWNLOAD_OK -> {dest}")


if __name__ == "__main__":
    sys.exit(main())