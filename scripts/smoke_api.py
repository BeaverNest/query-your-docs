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

                # Step B: ask honors saved retrieval.top_k + persona (design 6.8/7).
                code, j = req("PUT", "/api/settings", body={
                    "persona": {"preset": "detailed", "custom": "Always cite page numbers."},
                    "retrieval": {"top_k": 6},
                })
                check("pre-ask: save top_k=6 + detailed persona",
                      code == 200 and j["data"]["retrieval"]["top_k"] == 6
                      and j["data"]["persona"]["preset"] == "detailed", str(j)[:200])
                code, j = req("POST", "/api/ask", body={"conversation_id": conv_id, "question": "Apa isi utama laporan PMI manufaktur Indonesia?"})
                check("ask uses saved top_k=6 (sources count = 6)",
                      code == 200 and len(j["data"]["sources"]) == 6,
                      f"status {code}, sources={len(j.get('data', {}).get('sources', []))}" if code == 200 else str(j)[:200])
                # reset knobs for the rest of the smoke (defaults)
                code, j = req("PUT", "/api/settings", body={
                    "persona": {"preset": "concise", "custom": ""},
                    "retrieval": {"top_k": 4},
                })
                check("post-ask: reset knobs to defaults", code == 200, str(j)[:200])

                print("== history ==")
                code, j = req("GET", "/api/history")
                check("history lists 1 conversation", code == 200 and len(j["data"]["conversations"]) == 1, str(j)[:200])
                code, j = req("GET", f"/api/history/{conv_id}")
                check("history detail has messages (user+assistant pairs from both asks)",
                      code == 200 and len(j["data"]["conversation"]["messages"]) == 4, str(j)[:200])
                msgs = j["data"]["conversation"]["messages"]
                check("assistant message stores sources", msgs[1]["role"] == "assistant" and msgs[1]["sources"] and len(msgs[1]["sources"]) > 0)

                print("== history continuation ==")
                code, j = req("POST", "/api/ask", body={"conversation_id": conv_id, "question": "Apa tren suku bunga?"})
                check("ask continues existing conversation", code == 200 and j["data"]["conversation_id"] == conv_id, str(j)[:200])
                code, j = req("GET", f"/api/history/{conv_id}")
                check("history detail now has 6 messages", len(j["data"]["conversation"]["messages"]) == 6, str(j)[:200])
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

        print("== settings test endpoint (Step B) ==")
        # GET /api/config extended with persona + retrieval knobs (design 10.5).
        # The smoke mutates settings above, so only assert shape + range, not defaults.
        code, j = req("GET", "/api/config")
        d_cfg = j.get("data", {})
        check("config exposes persona.preset + retrieval.top_k",
              code == 200
              and isinstance(d_cfg.get("persona", {}).get("preset"), str)
              and d_cfg["persona"]["preset"] in ("concise", "detailed", "beginner", "indonesian")
              and isinstance(d_cfg.get("retrieval", {}).get("top_k"), int)
              and 1 <= d_cfg["retrieval"]["top_k"] <= 10, str(j)[:200])

        # chunk_size applies on the NEXT index (whole-library rebuild). The
        # KB was emptied by the remove-source section, so re-upload + reindex
        # with a small fixture, then compare chunk counts for 200 vs 2000.
        code, j = req("POST", "/api/upload", files={
            "files": ("chunk_probe.txt", b"Probe sentence for chunk sizing. " * 200),
        })
        check("chunk probe: upload txt -> ready",
              code == 200 and j["data"]["results"][0]["status"] == "ready", str(j)[:200])
        code, j = req("PUT", "/api/settings", body={"retrieval": {"chunk_size": 200}})
        check("chunk probe: save chunk_size=200", code == 200 and j["data"]["retrieval"]["chunk_size"] == 200, str(j)[:200])
        code, j = req("POST", "/api/index")
        n_small = j["data"]["result"]["chunks"] if code == 200 else -1
        check("chunk probe: index with chunk_size=200 succeeds", code == 200 and n_small > 0, str(j)[:200])
        code, j = req("PUT", "/api/settings", body={"retrieval": {"chunk_size": 2000}})
        check("chunk probe: save chunk_size=2000", code == 200 and j["data"]["retrieval"]["chunk_size"] == 2000, str(j)[:200])
        code, j = req("POST", "/api/index")
        n_large = j["data"]["result"]["chunks"] if code == 200 else -1
        check("chunk probe: index with chunk_size=2000 succeeds", code == 200 and n_large > 0, str(j)[:200])
        check("chunk probe: smaller chunk_size -> more chunks (applies on next index)",
              n_small > n_large, f"200->{n_small} chunks, 2000->{n_large} chunks")

        # validation failures -> 400 bad-request
        code, j = req("POST", "/api/settings/test", body={})
        check("settings/test: missing name -> 400 bad-request",
              code == 400 and j["error"]["code"] == "bad-request", str(j)[:200])
        code, j = req("POST", "/api/settings/test", body={"name": "m", "base_url": "not-a-url", "api_key": "k"})
        check("settings/test: invalid base_url -> 400 bad-request",
              code == 400 and j["error"]["code"] == "bad-request", str(j)[:200])
        code, j = req("POST", "/api/settings/test", body={"name": "x" * 121, "api_key": "k"})
        check("settings/test: name >120 chars -> 400 bad-request",
              code == 400 and j["error"]["code"] == "bad-request", str(j)[:200])
        code, j = req("POST", "/api/settings/test", body={"name": "m", "api_key": 123})
        check("settings/test: non-string api_key -> 400 bad-request",
              code == 400 and j["error"]["code"] == "bad-request", str(j)[:200])

        # unreachable provider -> connection-failed (deterministic, fast, no save)
        code, j = req("POST", "/api/settings/test", body={
            "name": "staged-model", "base_url": "http://127.0.0.1:9/v1", "api_key": "«redacted:sk-…»",
        })
        check("settings/test: unreachable provider -> 502 connection-failed",
              code == 502 and j["error"]["code"] == "connection-failed", str(j)[:200])
        check("settings/test: error message never contains the staged key",
              "«redacted:sk-…»" not in json.dumps(j), str(j)[:200])

        # staged test must NOT persist anything
        code, j = req("GET", "/api/settings")
        check("settings/test: staged values not saved (model still smoke-model)",
              code == 200 and j["data"]["model"]["name"] == "smoke-model", str(j["data"]["model"])[:200])

        # real provider round-trip -> ok + latency_ms (only when a live key exists)
        if WITH_ASK and os.environ.get("OPENAI_API_KEY"):
            real_model = os.environ.get("RAG_LLM_MODEL", "deepseek-v4-flash")
            real_base = os.environ.get("OPENAI_BASE_URL", "")
            body_t = {"name": real_model, "api_key": os.environ["OPENAI_API_KEY"]}
            if real_base:
                body_t["base_url"] = real_base
            code, j = req("POST", "/api/settings/test", body=body_t)
            ok_shape = (code == 200 and j.get("ok") is True
                        and isinstance(j["data"].get("latency_ms"), int)
                        and j["data"].get("model") == real_model)
            check("settings/test: real provider -> ok + latency_ms", ok_shape, str(j)[:200])
        else:
            print("  SKIP  settings/test real-provider step (no OPENAI_API_KEY)")

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
