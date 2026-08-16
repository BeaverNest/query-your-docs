#!/usr/bin/env python3
"""smoke_frontend.py — browser smoke for the query-your-docs web UI (Step C).

Self-contained: creates a THROWAWAY temp env (docs/KB/history), spawns its
own server, runs the Playwright flow, then tears everything down — the real
data/kb.db is never touched. Requires Playwright (chromium) and a real
OPENAI_API_KEY in the repo .env for the ask-with-citations step.

Usage:
  .venv/bin/python scripts/smoke_frontend.py

Env:
  QYD_FE_PORT   port to bind the throwaway server (default 8894)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "bin" / "python"
PORT = int(os.environ.get("QYD_FE_PORT", "8894"))
BASE = f"http://127.0.0.1:{PORT}"

PASS, FAIL = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {detail}")


def wait_health(timeout: int = 40) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> None:
    from playwright.sync_api import sync_playwright

    # ---- throwaway env + fixtures (docs dir starts EMPTY for boot states) ----
    tmp = Path(tempfile.mkdtemp(prefix="qyd-fe-smoke-"))
    docs = tmp / "docs"; docs.mkdir()
    fixtures = tmp / "fixtures"; fixtures.mkdir()
    txt1 = fixtures / "smoke_notes.txt"
    txt1.write_text(
        "The Indonesian manufacturing PMI rose to 51.2 in July 2026, "
        "signalling expansion. New orders and output both improved during "
        "the month. The automotive industry in Indonesia grew 4.1 percent "
        "year-on-year in the first half of 2026, driven by domestic demand. "
        "Electric vehicle sales increased sharply. Macroeconomic conditions "
        "remained stable with inflation below 3 percent.\n",
        encoding="utf-8",
    )
    txt2 = fixtures / "smoke_notes2.txt"
    txt2.write_text(
        "The 2026 automotive outlook is positive. Domestic car sales are "
        "expected to reach 1.1 million units. Electric vehicle adoption "
        "continues to rise with new charging infrastructure.\n",
        encoding="utf-8",
    )
    bad_xlsx = fixtures / "bad.xlsx"
    bad_xlsx.write_bytes(b"not really an excel file")

    env = {
        **os.environ,
        "QYD_DOCS_DIR": str(docs),
        "RAG_DB": str(tmp / "kb.db"),
        "QYD_HISTORY_DB": str(tmp / "history.db"),
        "QYD_PORT": str(PORT),
        "HOME": os.path.expanduser("~"),
    }
    proc = subprocess.Popen(
        [str(PY), str(REPO / "server.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_health():
            out = proc.stdout.read(2000).decode(errors="replace")
            print("server boot failed; log:", out)
            return 1

        console_errors: list[str] = []
        page_errors: list[str] = []

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(30_000)
    
            def on_console(msg):
                t = msg.type
                text = msg.text
                if t == "error":
                    # known unrelated noise
                    if "favicon" in text or "Failed to load resource" in text and "404" in text:
                        return
                    console_errors.append(text)
                elif t == "warning":
                    pass
    
            def on_pageerror(exc):
                page_errors.append(str(exc))
    
            page.on("console", on_console)
            page.on("pageerror", on_pageerror)
    
            # ------------------------------------------------------------- 1. boot
            print("== 1. boot / layout (desktop 1440x900) ==")
            resp = page.goto(BASE + "/", wait_until="load")
            check("GET / serves index.html", resp is not None and resp.status == 200)
            page.wait_for_selector("#sourcesList", timeout=15_000)
            page.wait_for_function("window.QYD && window.QYD.state.config !== null")
    
            check("title", "query-your-docs" in page.title())
            for f in ("index.html", "style.css", "app.js"):
                got = page.evaluate(f"fetch('/static/{f}').then(r=>r.text())")
                disk = (REPO / "static" / f).read_text(encoding="utf-8")
                check(f"static/{f} served == disk", got == disk)
    
            # zones
            check("topbar height 56px", page.evaluate("document.querySelector('.topbar').getBoundingClientRect().height") == 56)
            check("sidebar 300px", page.evaluate("document.querySelector('.sidebar').getBoundingClientRect().width") == 300)
            chat_inner_w = page.evaluate("document.querySelector('#chatInner').getBoundingClientRect().width")
            check("chat inner <= 760px", chat_inner_w <= 760)
            centered = page.evaluate(
                "(() => { const c = document.querySelector('#chatScroll').getBoundingClientRect();"
                " const i = document.querySelector('#chatInner').getBoundingClientRect();"
                " return Math.abs(i.left - (c.left + (c.width - i.width) / 2)) < 2; })()"
            )
            check("chat inner centered", centered)
            check("tabs present", page.locator(".tab").count() == 2)
            check("theme toggle present", page.locator("#themeBtn").count() == 1)
            check("Add sources present", page.locator("#addSourcesBtn").count() == 1)
    
            # empty state
            check("empty hero title", page.locator(".empty-title").inner_text() == "Ask your documents")
            check("empty CTA visible", page.locator("#emptyUploadBtn").is_visible())
            check("composer disabled with hint", page.locator("#composer").is_disabled())
            hint = page.locator("#composerHint").inner_text()
            check("composer hint", "Add documents" in hint, hint)
            check("send disabled", page.locator("#sendBtn").is_disabled())
    
            # sources empty state
            check("sources empty state", "No sources yet" in page.locator("#sourcesList").inner_text())
            # history empty state
            page.locator("#tabHistory").click()
            check("history empty state", "No conversations yet" in page.locator("#historyList").inner_text())
            page.locator("#tabSources").click()
    
            # ------------------------------------------------- 1b. citation renderer unit
            print("== 1b. citation renderer unit ==")
            render = page.evaluate("""() => {
              const qyd = window.QYD;
              return {
                md: qyd.inlineMd('Answer with [1] and [1,2] citation'),
                nums: qyd.citedNumbers('x [1,2] y [1] z [3, 4]'),
                body: qyd.renderAnswerHtml('**bold** line\\n\\n- item one\\n- item two\\n\\nlast [1]'),
              };
            }""")
            check("inlineMd makes chips", 'cite-chip' in render["md"])
            check("inlineMd hides raw [n]", "[1]" not in render["md"] and "[1,2]" not in render["md"], render["md"])
            check("citedNumbers parse", render["nums"] == [1, 2, 3, 4], str(render["nums"]))
            check("renderAnswerHtml bold+list+chip",
                  "<strong>bold</strong>" in render["body"] and "<ul>" in render["body"] and "cite-chip" in render["body"],
                  render["body"][:200])
    
            # ------------------------------------------------------------- 2. upload
            print("== 2. upload modal ==")
            page.locator("#emptyUploadBtn").click()
            check("modal opens", page.locator("#uploadModal").is_visible())
            check("modal width 560", page.evaluate("document.querySelector('#uploadModal .modal').getBoundingClientRect().width") == 560)
    
            # queue: 2 valid txt + rejected xlsx (client-side type reject)
            page.set_input_files("#fileInput", [str(txt1), str(txt2), str(bad_xlsx)])
            page.wait_for_selector(".file-row", timeout=10_000)
            statuses = page.evaluate("[...document.querySelectorAll('#fileQueue .file-row')].map(r => r.querySelector('.f-status').textContent)")
            check("queue has 3 rows", len(statuses) == 3, str(statuses))
            ready_rows = [s for s in statuses if "ready" in s]
            rejected_row = [s for s in statuses if "unsupported" in s]
            check("txts ready after upload", len(ready_rows) == 2, str(statuses))
            check("xlsx rejected inline", len(rejected_row) == 1, str(statuses))
    
            # ------------------------------------------------------------- 3. index
            print("== 3. index ==")
            idx_btn = page.locator("#indexBtn")
            check("index button enabled+label", not idx_btn.is_disabled() and "Index 2 documents" in idx_btn.inner_text())
            idx_btn.click()
            check("index progress visible", page.locator("#indexProgress").is_visible())
            # wait for modal close (index done) + toast
            page.wait_for_selector("#uploadModal", state="hidden", timeout=120_000)
            page.wait_for_selector("#toast", state="visible", timeout=5_000)
            check("toast shows ready", "ready" in page.locator("#toast").inner_text())
            page.wait_for_selector("#toast", state="hidden", timeout=6_000)
    
            # sources list now shows ready rows
            src_text = page.locator("#sourcesList").inner_text()
            check("sources show smoke_notes", "smoke notes" in src_text)
            check("sources show smoke_notes2", "smoke notes2" in src_text)
            check("sources show Ready chips", src_text.count("Ready") >= 2, src_text[:200])
            check("composer now enabled", not page.locator("#composer").is_disabled())
            check("hint cleared", page.locator("#composerHint").inner_text() == "")
    
            # suggestions visible (docs ready, no messages)
            check("suggestions visible", page.locator("#suggestions").is_visible() and page.locator(".suggestion").count() >= 1)
    
            # ------------------------------------------------------------- 4. chat + citations
            print("== 4. chat with citations ==")
            q = "What does the notes say about the manufacturing PMI?"
            page.fill("#composer", q)
            check("send enabled", not page.locator("#sendBtn").is_disabled())
            page.locator("#sendBtn").click()
    
            # typing indicator appears
            page.wait_for_selector(".msg-assistant .typing", timeout=5_000)
            check("typing indicator shown", True)
            # wait for answer
            page.wait_for_selector("#msgList .msg-assistant .sources-row", timeout=120_000)
            check("user msg rendered", page.locator("#msgList .msg-user .msg-body").inner_text() == q)
    
            body = page.locator("#msgList .msg-assistant .msg-body").inner_text()
            chips = page.locator("#msgList .msg-assistant .cite-chip").count()
            src_chips = page.locator("#msgList .msg-assistant .sources-row .source-chip").count()
            check("answer has citation chips", chips >= 1, body[:200])
            check("sources row chips", src_chips >= 1)
            check("no raw [n] in answer", not re.search(r"\[\d", body), body[:200])
            check("no typing left", page.locator("#msgList .msg-assistant .typing").count() == 0)
    
            # assistant markdown body uses <p>
            check("answer has paragraph markup", page.locator("#msgList .msg-assistant .msg-body p").count() >= 1)
    
            # ------------------------------------------------------------- 5. history
            print("== 5. history ==")
            page.locator("#tabHistory").click()
            rows = page.locator("#historyList .conv-row")
            check("conversation listed", rows.count() >= 1)
            check("active row highlighted", page.locator("#historyList .conv-row.active").count() == 1)
            title_txt = rows.first.inner_text()
            check("title = question truncated", q[:20] in title_txt or "PMI" in title_txt, title_txt)
    
            # switch to Sources tab and back via row click
            page.locator("#tabSources").click()
            page.locator("#tabHistory").click()
            page.locator("#historyList .conv-row").first.click()
            page.wait_for_selector("#msgList .msg-assistant .sources-row", timeout=30_000)
            check("conversation loads messages", page.locator("#msgList .msg-user").count() >= 1 and page.locator("#msgList .msg-assistant").count() >= 1)
    
            # ------------------------------------------------------------- 6. persistence
            print("== 6. persistence after reload ==")
            page.reload(wait_until="load")
            page.wait_for_selector("#sourcesList", timeout=15_000)
            page.wait_for_function("window.QYD && window.QYD.state.config !== null")
            check("sources persist", "smoke notes2" in page.locator("#sourcesList").inner_text())
            page.locator("#tabHistory").click()
            page.wait_for_selector("#historyList .conv-row", timeout=10_000)
            check("history persists", page.locator("#historyList .conv-row").count() >= 1)
            page.locator("#historyList .conv-row").first.click()
            page.wait_for_selector("#msgList .msg-assistant .sources-row", timeout=30_000)
            check("history messages reload", page.locator("#msgList .msg-user").count() >= 1)
    
            # ------------------------------------------------------------- 7. remove source
            print("== 7. remove source ==")
            page.locator("#tabSources").click()
            # remove smoke_notes2 (second ready row's trash; keep smoke_notes)
            page.locator('#sourcesList [data-remove]').nth(1).hover()
            page.locator('#sourcesList [data-remove]').nth(1).click()
            check("confirm modal opens", page.locator("#confirmModal").is_visible())
            page.locator("#confirmOkBtn").click()
            page.wait_for_selector("#confirmModal", state="hidden", timeout=5_000)
            # reindex is synchronous; wait until sources list no longer contains smoke_notes2
            page.wait_for_function(
                "!document.querySelector('#sourcesList').innerText.includes('smoke notes2')",
                timeout=120_000,
            )
            check("source removed", "smoke notes2" not in page.locator("#sourcesList").inner_text())
            check("remaining source kept", "smoke notes" in page.locator("#sourcesList").inner_text())
    
            # ------------------------------------------------------------- 8. responsive <640
            print("== 8. responsive (375x667) ==")
            page.set_viewport_size({"width": 375, "height": 667})
            page.wait_for_timeout(400)
            check("menu button visible", page.locator("#menuBtn").is_visible())
            check("sidebar hidden (drawer)", page.evaluate("document.querySelector('#sidebar').getBoundingClientRect().right <= 0"))
            page.locator("#menuBtn").click()
            check("drawer opens", page.locator("#sidebar.open").count() == 1)
            check("scrim visible", page.locator("#scrim").is_visible())
            page.locator("#scrim").click(force=True)
            check("scrim closes drawer", page.locator("#sidebar.open").count() == 0)
            # upload modal full-screen sheet
            page.locator("#addSourcesBtn").click()
            modal_box = page.locator("#uploadModal .modal").bounding_box()
            check("modal is full-screen sheet", modal_box is not None and modal_box["width"] == 375, str(modal_box))
            check("dropzone min 120px", page.evaluate("document.querySelector('#dropZone').getBoundingClientRect().height") >= 120)
            page.locator("#closeModalBtn").click()
            # touch target >= 44
            send_h = page.evaluate("document.querySelector('#sendBtn').getBoundingClientRect().height")
            check("send button >= 44px", send_h >= 44, str(send_h))
            # no horizontal scroll
            no_h = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            check("no horizontal scroll", no_h)

            # ------------------------------------------------- 8b. dark mode toggle
            print("== 8b. dark mode toggle ==")
            page.set_viewport_size({"width": 1440, "height": 900})
            page.wait_for_timeout(300)
            theme0 = page.evaluate("document.documentElement.getAttribute('data-theme')")
            check("theme attribute set", theme0 in ("light", "dark"), theme0)
            page.locator("#themeBtn").click()
            theme1 = page.evaluate("document.documentElement.getAttribute('data-theme')")
            check("toggle flips theme", theme1 != theme0, theme0 + "->" + theme1)
            stored = page.evaluate("localStorage.getItem('qyd-theme')")
            check("theme persisted to localStorage", stored == theme1, str(stored))
            page.reload(wait_until="load")
            page.wait_for_function("window.QYD && window.QYD.state.config !== null")
            theme2 = page.evaluate("document.documentElement.getAttribute('data-theme')")
            check("theme survives reload", theme2 == theme1, theme2)
            # restore light for the console section
            if theme2 != "light":
                page.locator("#themeBtn").click()
    
            # ------------------------------------------------------------- 9. console
            print("== 9. console errors ==")
            check("no pageerror", len(page_errors) == 0, str(page_errors))
            check("no console errors", len(console_errors) == 0, str(console_errors))
    
            browser.close()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'-' * 50}")
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    main()
