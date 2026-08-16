#!/usr/bin/env python3
"""smoke_api.py — end-to-end backend smoke test for query-your-docs.

Runs against a THROWAWAY environment (temp docs dir, temp KB, temp history
db) so the real data/docs/kb.db are never touched. Requires a real
OPENAI_API_KEY for the ask-with-citations step (falls back to reporting the
ask step as skipped if the key is missing, e.g. CI).

Usage:
  QYD_SMOKE_WITH_ASK=1 .venv/bin/python scripts/smoke_api.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "bin" / "python"
PORT = int(os.environ.get("QYD_SMOKE_PORT", "8891"))
BASE = f"http://127.0.0.1:{PORT}"
WITH_ASK = os.environ.get("QYD_SMOKE_WITH_ASK", "1") == "1"

# Load the repo .env (keys only) so the ask step can run against the real
# LLM provider; the key itself is never printed.
_env = REPO / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

PASS, FAIL = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {detail}")


def req(method: str, path: str, body=None, files=None, headers=None) -> tuple[int, dict]:
    url = BASE + path
    hdrs = dict(headers or {})
    data = None
    ctype = None
    if files:
        boundary = "----smoke" + os.urandom(8).hex()
        parts = []
        for field, (filename, content) in files.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
            .encode() + content + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        ctype = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        ctype = "application/json"
    if ctype:
        hdrs["Content-Type"] = ctype
    r = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def wait_health(timeout: int = 40) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, j = req("GET", "/api/health")
            if code == 200 and j.get("ok"):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    global PASS, FAIL
    tmp = Path(tempfile.mkdtemp(prefix="qyd-smoke-"))
    docs = tmp / "docs"; docs.mkdir()
    kb = tmp / "kb.db"
    hist = tmp / "history.db"

    # fixtures: 1 PDF (copy of a real repo doc) + 1 TXT + 1 unsupported
    src_pdf = REPO / "data" / "docs" / "laporan_pmi_manufaktur_indonesia_2026-07.pdf"
    if not src_pdf.exists():
        print("SKIP: no fixture PDF found in data/docs")
        return 2
    shutil.copy2(src_pdf, docs / "laporan_pmi.pdf")
    (docs / "catatan.txt").write_text(
        "Kucing domestik menyukai tempat hangat dan tidur 12-16 jam sehari. "
        "Mereka berkomunikasi dengan mengeong dan mendengkur. "
        "Kucing adalah hewan peliharaan yang populer di Indonesia.\n" * 30,
        encoding="utf-8",
    )
    (docs / "reject.xlsx").write_bytes(b"not really xlsx")

    env = {
        **os.environ,
        "QYD_DOCS_DIR": str(docs),
        "RAG_DB": str(kb),
        "QYD_HISTORY_DB": str(hist),
        "QYD_SETTINGS_DB": str(tmp / "settings.db"),
        "QYD_PORT": str(PORT),
        "HOME": os.path.expanduser("~"),
    }

    proc = subprocess.Popen(
        [str(PY), str(REPO / "server.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        print("== server boot ==")
        ok = wait_health()
        check("server boots and /api/health is ok", ok)
        if not ok:
            out = proc.stdout.read(2000).decode(errors="replace")
            print("server log:", out)
            return 1

        print("== config / sources (before index) ==")
        code, j = req("GET", "/api/config")
        check("config returns llm_configured bool", code == 200 and isinstance(j["data"]["llm_configured"], bool))
        code, j = req("GET", "/api/sources")
        check("sources lists 2 valid files before index (pending; xlsx excluded)",
              code == 200 and len(j["data"]["sources"]) == 2, str(j["data"]["sources"])[:200])

        print("== upload ==")
        code, j = req("POST", "/api/upload", files={
            "files": ("uploaded_note.txt", b"Uji unggah file teks.\n" * 40),
        })
        check("upload single txt -> ready", code == 200 and j["data"]["results"][0]["status"] == "ready", str(j))

        code, j = req("POST", "/api/upload", files={
            "files": ("bad.xlsx", b"junk"),
        })
        check("upload unsupported type -> rejected/unsupported-type",
              code == 200 and j["data"]["results"][0]["status"] == "rejected"
              and j["data"]["results"][0]["error"] == "unsupported-type", str(j))

        print("== index ==")
        code, j = req("POST", "/api/index")
        check("index returns indexed=True", code == 200 and j["data"]["indexed"] is True, str(j)[:300])
        check("index embeds pdf + txt (docs_indexed>=2)", j["data"]["result"]["docs_indexed"] >= 2, str(j["data"]["result"])[:300])
        check("index chunks>0", j["data"]["result"]["chunks"] > 0)
        code, j = req("GET", "/api/sources")
        ok_docs = [s for s in j["data"]["sources"] if s["status"] == "ready"]
        check("sources: ready docs present", code == 200 and len(ok_docs) >= 2, str(j)[:300])

        print("== ask with citations ==")
        if WITH_ASK and os.environ.get("OPENAI_API_KEY"):
            code, j = req("POST", "/api/ask", body={"question": "Apa isi utama laporan PMI manufaktur Indonesia?"})
            if code == 200:
                d = j["data"]
                check("ask returns answer + sources", bool(d["answer"]) and len(d["sources"]) > 0, str(j)[:300])
                check("ask conversation_id matches c_ pattern",
                      d["conversation_id"].startswith("c_") and len(d["conversation_id"]) > 9)
                check("ask sources have n/title/page/score", all("n" in s and "title" in s and "page" in s and "score" in s for s in d["sources"]))
                check("ask answer contains [1] citation marker", "[1]" in d["answer"], d["answer"][:120])
                conv_id = d["conversation_id"]

                print("== history ==")
                code, j = req("GET", "/api/history")
                check("history lists 1 conversation", code == 200 and len(j["data"]["conversations"]) == 1, str(j)[:200])
                code, j = req("GET", f"/api/history/{conv_id}")
                check("history detail has 2 messages (user+assistant)",
                      code == 200 and len(j["data"]["conversation"]["messages"]) == 2, str(j)[:200])
                msgs = j["data"]["conversation"]["messages"]
                check("assistant message stores sources", msgs[1]["role"] == "assistant" and msgs[1]["sources"] and len(msgs[1]["sources"]) > 0)

                print("== history continuation ==")
                code, j = req("POST", "/api/ask", body={"conversation_id": conv_id, "question": "Apa tren suku bunga?"})
                check("ask continues existing conversation", code == 200 and j["data"]["conversation_id"] == conv_id, str(j)[:200])
                code, j = req("GET", f"/api/history/{conv_id}")
                check("history detail now has 4 messages", len(j["data"]["conversation"]["messages"]) == 4, str(j)[:200])
            else:
                check("ask with citations works", False, f"status {code}: {str(j)[:300]}")
        else:
            print("  SKIP  ask step (no OPENAI_API_KEY in environment)")

        print("== error cases ==")
        code, j = req("POST", "/api/ask", body={"question": "  "})
        check("ask empty question -> 400 bad-request", code == 400 and j["error"]["code"] == "bad-request", str(j))
        code, j = req("POST", "/api/ask", body={"conversation_id": "c_deadbeef", "question": "x"})
        check("ask unknown conversation -> 404 not-found", code == 404 and j["error"]["code"] == "not-found", str(j))
        code, j = req("GET", "/api/history/c_nonexistent")
        check("history unknown id -> 404 not-found", code == 404 and j["error"]["code"] == "not-found", str(j))
        code, j = req("DELETE", "/api/sources/no_such_doc")
        check("remove unknown source -> 404 not-found", code == 404 and j["error"]["code"] == "not-found", str(j))
        code, j = req("POST", "/api/ask", body={"question": "x" * 5000})
        check("ask too-long question -> 400 bad-request", code == 400 and j["error"]["code"] == "bad-request", str(j))

        print("== remove source + reindex ==")
        code, j = req("DELETE", "/api/sources/catatan")
        check("remove indexed txt -> ok + reindex", code == 200 and j["data"]["removed"] == "catatan", str(j)[:200])
        code, j = req("DELETE", "/api/sources/laporan_pmi")
        check("remove indexed pdf -> ok + reindex", code == 200 and j["data"]["removed"] == "laporan_pmi", str(j)[:200])
        code, j = req("GET", "/api/sources")
        ids = {s["id"] for s in j["data"]["sources"]}
        check("removed docs gone from sources", "laporan_pmi" not in ids and "catatan" not in ids, str(ids))

        print("== settings (Step A) ==")
        env_key = os.environ.get("OPENAI_API_KEY", "")
        code, j = req("GET", "/api/settings")
        d = j.get("data", {})
        check("GET settings: envelope ok + 5 sections",
              code == 200 and j.get("ok") is True
              and all(k in d for k in ("model", "persona", "retrieval", "appearance", "about")), str(d)[:300])
        check("GET settings: api_key is {has_key} only, no raw value",
              isinstance(d.get("model", {}).get("api_key"), dict)
              and "has_key" in d["model"]["api_key"]
              and (len(env_key) < 8 or env_key not in json.dumps(d)), str(d)[:300])
        check("GET settings: defaults (preset concise, top_k 4, chunk 600, lang en)",
              d["persona"]["preset"] == "concise" and d["retrieval"]["top_k"] == 4
              and d["retrieval"]["chunk_size"] == 600 and d["appearance"]["language"] == "en", str(d)[:300])
        check("GET settings: about reflects server",
              d["about"]["version"] and isinstance(d["about"]["chunks"], int)
              and d["about"]["server_ok"] is True, str(d["about"]))

        # save a full valid shape (fake key — never leaves the temp DB)
        code, j = req("PUT", "/api/settings", body={
            "model": {"name": "smoke-model", "base_url": "https://api.example.com/v1",
                      "api_key": "sk-smoke-1234567890"},
            "persona": {"preset": "detailed", "custom": "Always cite page numbers."},
            "retrieval": {"top_k": 6, "chunk_size": 800},
            "appearance": {"theme": "system", "language": "id"},
        })
        check("PUT settings: valid full shape -> ok",
              code == 200 and j.get("ok") is True, str(j)[:300])
        d = j.get("data", {})
        check("PUT settings: response has saved values, key as has_key only",
              d["model"]["name"] == "smoke-model"
              and d["model"]["api_key"]["has_key"] is True
              and "sk-smoke-1234567890" not in json.dumps(d), str(d)[:300])
        check("PUT settings: persona/retrieval/appearance saved",
              d["persona"]["preset"] == "detailed" and d["persona"]["custom"] == "Always cite page numbers."
              and d["retrieval"]["top_k"] == 6 and d["retrieval"]["chunk_size"] == 800
              and d["appearance"]["theme"] == "system" and d["appearance"]["language"] == "id", str(d)[:300])

        # saved model config is bridged to env -> /api/config reflects it
        code, j = req("GET", "/api/config")
        check("config reflects saved model (env bridge)",
              code == 200 and j["data"]["model"] == "smoke-model"
              and j["data"]["llm_configured"] is True, str(j)[:200])

        # partial save keeps untouched sections
        code, j = req("PUT", "/api/settings", body={"persona": {"preset": "beginner"}})
        d = j.get("data", {})
        check("PUT settings: partial save keeps other sections",
              code == 200 and d["persona"]["preset"] == "beginner"
              and d["retrieval"]["top_k"] == 6 and d["model"]["name"] == "smoke-model", str(d)[:300])

        # empty api_key keeps existing key (design 6.2)
        code, j = req("PUT", "/api/settings", body={"model": {"name": "smoke-model", "api_key": ""}})
        d = j.get("data", {})
        check("PUT settings: empty api_key keeps existing key",
              code == 200 and d["model"]["api_key"]["has_key"] is True, str(d)[:300])

        print("== settings validation ==")
        code, j = req("PUT", "/api/settings", body={"model": {"name": "  "}})
        check("PUT settings: empty model name -> 400 bad-request",
              code == 400 and j["error"]["code"] == "bad-request", str(j)[:200])
        code, j = req("PUT", "/api/settings", body={"model": {"name": "x" * 121}})
        check("PUT settings: model name >120 chars -> 400",
              code == 400 and j["error"]["code"] == "bad-request", str(j)[:200])
        code, j = req("PUT", "/api/settings", body={"model": {"name": "m", "base_url": "not-a-url"}})
        check("PUT settings: invalid base_url -> 400",
              code == 400 and j["error"]["code"] == "bad-request", str(j)[:200])
        code, j = req("PUT", "/api/settings", body={"retrieval": {"top_k": 0}})
        check("PUT settings: top_k 0 -> 400",
              code == 400 and j["error"]["code"] == "bad-request", str(j)[:200])
        code, j = req("PUT", "/api/settings", body={"retrieval": {"top_k": 11}})
        check("PUT settings: top_k 11 -> 400",
              code == 400 and j["error"]["code"] == "bad-request", str(j)[:200])
        code, j = req("PUT", "/api/settings", body={"retrieval": {"chunk_size": 50}})
        check("PUT settings: chunk_size 50 -> 400",
              code == 400 and j["error"]["code"] == "bad-request", str(j)[:200])
        code, j = req("PUT", "/api/settings", body={"retrieval": {"chunk_size": 5000}})
        check("PUT settings: chunk_size 5000 -> 400",
              code == 400 and j["error"]["code"] == "bad-request", str(j)[:200])
        code, j = req("PUT", "/api/settings", body={"persona": {"preset": "verbose"}})
        check("PUT settings: unknown persona preset -> 400",
              code == 400 and j["error"]["code"] == "bad-request", str(j)[:200])
        code, j = req("PUT", "/api/settings", body={"appearance": {"theme": "neon"}})
        check("PUT settings: unknown theme -> 400",
              code == 400 and j["error"]["code"] == "bad-request", str(j)[:200])
        code, j = req("PUT", "/api/settings", body={"persona": {"custom": "x" * 2001}})
        check("PUT settings: custom >2000 chars -> 400",
              code == 400 and j["error"]["code"] == "bad-request", str(j)[:200])

        print("== no secrets ==")
        code, j = req("GET", "/api/config")
        check("config never returns api key", "OPENAI_API_KEY" not in json.dumps(j), str(j)[:200])
        code, j = req("GET", "/api/settings")
        check("settings never returns the saved api key",
              "sk-smoke-1234567890" not in json.dumps(j), str(j)[:200])

        print(f"\nRESULT: {PASS} passed, {FAIL} failed")
        return 0 if FAIL == 0 else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
