#!/usr/bin/env python3
"""smoke_settings_step_d.py — browser smoke for Settings drawer Step D.

Covers Task D DoD:
- Persona preset cards (radio, Concise default) + additive custom chip
- Advanced retrieval collapsed by default; expands <=150ms
- Appearance (theme/UI lang) applies instantly
- About section read-only (version/docs/chunks/conversations/server status)
- Footer Save/Discard with dirty state (Save only dirty+valid; Discard resets)
- API key never displayed as plaintext on load
- Responsive <640 (full-width sheet, stacked preset cards, sticky footer)

Self-contained: creates a THROWAWAY temp env, spawns its own server, runs the
Playwright flow, then tears everything down. The real data/ dirs are never
touched. PUT /api/settings is the real backend endpoint (no interception).

Usage:
  .venv/bin/python scripts/smoke_settings_step_d.py

Env:
  QYD_FE_PORT   port to bind the throwaway server (default 8896)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER_PY = REPO / ".venv" / "bin" / "python"
PORT = int(os.environ.get("QYD_FE_PORT", "8896"))
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


def http_json(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def main() -> None:
    from playwright.sync_api import sync_playwright

    tmp = Path(tempfile.mkdtemp(prefix="qyd-stepd-smoke-"))
    docs = tmp / "docs"; docs.mkdir()
    env = {
        **os.environ,
        "QYD_DOCS_DIR": str(docs),
        "RAG_DB": str(tmp / "kb.db"),
        "QYD_HISTORY_DB": str(tmp / "history.db"),
        "QYD_SETTINGS_DB": str(tmp / "settings.db"),
        "QYD_PORT": str(PORT),
        "HOME": os.path.expanduser("~"),
    }
    proc = subprocess.Popen(
        [str(SERVER_PY), str(REPO / "server.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_health():
            out = proc.stdout.read(2000).decode(errors="replace")
            print("server boot failed; log:", out)
            return 1

        # Seed rich settings so all sections render from GET (key saved for has_key).
        seed = http_json("/api/settings", "PUT", {
            "model": {"name": "deepseek-v4-flash", "base_url": "", "api_key": "sk-secret"},
            "persona": {"preset": "concise", "custom": ""},
            "retrieval": {"top_k": 4, "chunk_size": 600},
            "appearance": {"theme": "light", "language": "en"},
        })
        check("seed PUT ok", seed.get("ok") is True, str(seed)[:200])
        seeded_view = seed.get("data", {})
        check("seed has_key only (no raw key)",
              seeded_view.get("model", {}).get("api_key", {}).get("has_key") is True
              and "sk-secret" not in json.dumps(seed), json.dumps(seed)[:200])

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
                    if "favicon" in text or ("Failed to load resource" in text and "404" in text):
                        return
                    console_errors.append(text)

            def on_pageerror(exc):
                page_errors.append(str(exc))

            page.on("console", on_console)
            page.on("pageerror", on_pageerror)

            # ------------------------------------------------- 1. boot + open drawer
            print("== 1. boot + open drawer ==")
            resp = page.goto(BASE + "/", wait_until="load")
            check("GET / serves index.html", resp is not None and resp.status == 200)
            page.wait_for_function("window.QYD && window.QYD.state.config !== null")
            page.locator("#settingsBtn").click()
            page.wait_for_selector("#settingsDrawer", state="visible", timeout=5_000)
            page.wait_for_timeout(300)
            page.wait_for_function("window.QYD.state.settings.loaded === true", timeout=10_000)

            sections = page.evaluate(
                "Array.from(document.querySelectorAll('.drawer-body .settings-section .settings-label')).map(e=>e.textContent)"
            )
            check("all 5 sections render", any("Model" in s for s in sections)
                  and any("Persona" in s for s in sections)
                  and any("Retrieval" in s for s in sections)
                  and any("Appearance" in s for s in sections)
                  and any("About" in s for s in sections), str(sections))

            # ------------------------------------------------- 2. persona presets
            print("== 2. persona preset cards ==")
            cards = page.locator(".preset-card").count()
            check("4 preset cards", cards == 4, str(cards))
            check("Concise checked by default",
                  page.evaluate("document.querySelector('input[name=\"personaPreset\"]:checked').value") == "concise")
            page.locator('input[name="personaPreset"][value="detailed"]').check()
            check("Detailed now checked",
                  page.evaluate("window.QYD.state.settings.form.preset") == "detailed")
            check("preset change marks dirty", page.evaluate("window.QYD.state.settings.dirty") is True)
            check("dirty chip visible", page.locator("#settingsDirtyChip").is_visible())
            check("Save enabled when dirty+valid", page.locator("#settingsSaveBtn").is_enabled())

            # custom additive box + chip
            page.fill("#settingsCustom", "Always include the raw numbers")
            chip = page.locator("#settingsCustomChip")
            check("custom chip visible", chip.is_visible())
            check("chip text 'Custom + Detailed'", chip.inner_text() == "Custom + Detailed", chip.inner_text())
            check("custom count updates", page.locator("#settingsCustomCount").inner_text() == "30 / 2000")
            page.fill("#settingsCustom", "   ")
            check("whitespace-only custom hides chip", page.locator("#settingsCustomChip").is_hidden())
            page.fill("#settingsCustom", "Always include the raw numbers")

            # ------------------------------------------------- 3. advanced retrieval
            print("== 3. advanced retrieval collapsed ==")
            check("advanced body hidden by default", page.locator("#settingsAdvancedBody").is_hidden())
            check("toggle aria-expanded=false",
                  page.locator("#settingsAdvancedToggle").get_attribute("aria-expanded") == "false")
            t0 = time.time()
            page.locator("#settingsAdvancedToggle").click()
            page.wait_for_selector("#settingsAdvancedBody", state="visible", timeout=1_000)
            elapsed_ms = (time.time() - t0) * 1000
            check("advanced expands <=150ms", elapsed_ms <= 150, f"{elapsed_ms:.0f}ms")
            check("toggle aria-expanded=true",
                  page.locator("#settingsAdvancedToggle").get_attribute("aria-expanded") == "true")
            check("top-k renders from GET", page.locator("#settingsTopK").input_value() == "4")
            check("chunk size renders from GET", page.locator("#settingsChunkSize").input_value() == "600")
            check("'applies on next index' caption present",
                  "applies on next index" in page.locator(".advanced-body").inner_text())

            # invalid top-k -> inline error + Save disabled
            page.fill("#settingsTopK", "99")
            check("invalid top-k error shown", page.locator("#settingsTopKErr").is_visible())
            check("Save disabled when invalid", page.locator("#settingsSaveBtn").is_disabled())
            page.fill("#settingsTopK", "6")
            check("valid top-k clears error", page.locator("#settingsTopKErr").is_hidden())
            check("Save re-enabled", page.locator("#settingsSaveBtn").is_enabled())

            # ------------------------------------------------- 4. appearance instant
            print("== 4. appearance applies instantly ==")
            # Theme radio reflects the LIVE localStorage preference (design §6.9 "one key") —
            # initTheme writes 'system' at boot, so that is what the drawer shows, not the
            # seeded backend value. It must match what was actually applied.
            live_theme = page.evaluate("localStorage.getItem('qyd-theme')")
            radio_theme = page.evaluate("document.querySelector('input[name=\"appearanceTheme\"]:checked').value")
            check("theme radio matches live preference", radio_theme == live_theme, f"radio={radio_theme} live={live_theme}")
            page.locator('input[name="appearanceTheme"][value="dark"]').check()
            check("data-theme=dark instantly",
                  page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark")
            check("localStorage qyd-theme=dark",
                  page.evaluate("localStorage.getItem('qyd-theme')") == "dark")
            check("lang radio English checked", page.evaluate(
                "document.querySelector('input[name=\"appearanceLanguage\"]:checked').value") == "en")
            page.locator('input[name="appearanceLanguage"][value="id"]').check()
            check("document lang=id instantly", page.evaluate("document.documentElement.getAttribute('lang')") == "id")
            check("localStorage qyd-language=id", page.evaluate("localStorage.getItem('qyd-language')") == "id")

            # ------------------------------------------------- 5. about read-only
            print("== 5. about section ==")
            line = page.locator("#settingsAboutLine").inner_text()
            check("about shows version", "v0.1.0" in line, line)
            check("about shows docs/chunks/conversations", "docs" in line and "chunks" in line and "conversation" in line, line)
            status = page.locator("#settingsAboutStatus").inner_text()
            check("about server status OK", "Server OK" in status, status)
            check("about has no editable control", page.locator("#settingsAboutBody input, #settingsAboutBody textarea, #settingsAboutBody button").count() == 0)

            # ------------------------------------------------- 6. save / discard
            print("== 6. save + discard ==")
            page.locator("#settingsSaveBtn").click()
            page.wait_for_function("window.QYD.state.settings.saving === false && window.QYD.state.settings.dirty === false", timeout=10_000)
            got = http_json("/api/settings").get("data", {})
            check("PUT persisted preset", got.get("persona", {}).get("preset") == "detailed", json.dumps(got.get("persona")))
            check("PUT persisted custom", got.get("persona", {}).get("custom") == "Always include the raw numbers")
            check("PUT persisted top_k", got.get("retrieval", {}).get("top_k") == 6)
            check("PUT persisted appearance theme", got.get("appearance", {}).get("theme") == "dark")
            check("PUT persisted appearance lang", got.get("appearance", {}).get("language") == "id")
            check("dirty chip hidden after save", page.locator("#settingsDirtyChip").is_hidden())
            check("Save disabled when clean", page.locator("#settingsSaveBtn").is_disabled())

            # appearance is instant-only (design §6.1): changing theme in a clean form
            # must NOT enable Save or mark dirty — it already applied + persisted locally.
            page.locator('input[name="appearanceTheme"][value="light"]').check()
            check("theme change in clean form does not dirty", page.evaluate("window.QYD.state.settings.dirty") is False)
            check("Save still disabled after theme-only change", page.locator("#settingsSaveBtn").is_disabled())
            check("theme still applied instantly", page.evaluate("document.documentElement.getAttribute('data-theme')") == "light")
            page.locator('input[name="appearanceTheme"][value="dark"]').check()
            check("theme reverted back applied", page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark")

            # discard resets staged fields
            page.locator("#settingsAdvancedToggle").click()  # collapse (layout)
            page.locator("#settingsAdvancedToggle").click()  # expand again for editing
            page.fill("#settingsTopK", "9")
            check("dirty after edit", page.evaluate("window.QYD.state.settings.dirty") is True)
            page.locator("#settingsDiscardBtn").click()
            check("discard resets top_k", page.locator("#settingsTopK").input_value() == "6")
            check("discard clears dirty", page.evaluate("window.QYD.state.settings.dirty") is False)
            check("discard re-enables? Save disabled clean", page.locator("#settingsSaveBtn").is_disabled())

            # dirty-close confirm
            page.fill("#settingsCustom", "x")  # staged change
            page.locator("#settingsCloseBtn").click()
            check("dirty close shows confirm", page.locator("#settingsDiscardConfirm").is_visible())
            check("drawer stays open", page.locator("#settingsDrawer").is_visible())
            page.locator("#settingsConfirmCancel").click()
            check("cancel hides confirm", page.locator("#settingsDiscardConfirm").is_hidden())
            check("drawer still open", page.locator("#settingsDrawer").is_visible())
            page.locator("#settingsCloseBtn").click()
            page.locator("#settingsConfirmDiscard").click()
            page.wait_for_selector("#settingsDrawer", state="hidden", timeout=5_000)
            check("confirm discard closes drawer", page.locator("#settingsDrawer").is_hidden())
            after = http_json("/api/settings").get("data", {})
            check("confirm discard did NOT save", after.get("persona", {}).get("custom") == "Always include the raw numbers")

            # ------------------------------------------------- 7. key never plaintext
            print("== 7. API key masking ==")
            page.locator("#settingsBtn").click()
            page.wait_for_selector("#settingsDrawer", state="visible", timeout=5_000)
            page.wait_for_function("window.QYD.state.settings.loaded === true", timeout=10_000)
            check("key field empty on load", page.locator("#settingsApiKey").input_value() == "")
            check("key type=password", page.locator("#settingsApiKey").get_attribute("type") == "password")
            check("Saved chip shown (has_key)", page.locator("#settingsKeySaved").is_visible())
            check("placeholder masked dots", "\u2022" in page.locator("#settingsApiKey").get_attribute("placeholder"))

            # ------------------------------------------------- 8. responsive <640
            print("== 8. responsive 375x667 ==")
            page.set_viewport_size({"width": 375, "height": 667})
            page.wait_for_timeout(300)
            dw = page.locator(".drawer").bounding_box()
            check("drawer full-width <640", dw is not None and abs(dw["width"] - 375) < 1, str(dw))
            cols = page.evaluate("getComputedStyle(document.querySelector('.preset-grid')).gridTemplateColumns")
            check("preset cards stack 1 column", cols.split().count("375px") == 1 or cols.split().count("343px") == 1 or len(cols.split()) == 1, cols)
            preset_h = page.evaluate("document.querySelector('.preset-card').getBoundingClientRect().height")
            save_h = page.evaluate("document.querySelector('#settingsSaveBtn').getBoundingClientRect().height")
            custom_h = page.evaluate("document.querySelector('#settingsCustom').getBoundingClientRect().height")
            check("touch targets >= 44px", min(preset_h, save_h, custom_h) >= 44, f"{preset_h},{save_h},{custom_h}")
            foot = page.locator(".drawer-foot").bounding_box()
            check("sticky footer visible", foot is not None and foot["y"] + foot["height"] <= 668, str(foot))
            no_h = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            check("no horizontal scroll", no_h)
            page.keyboard.press("Escape")
            page.wait_for_selector("#settingsDrawer", state="hidden", timeout=5_000)
            check("mobile Esc closes cleanly", page.locator("#settingsDrawer").is_hidden())

            # ------------------------------------------------- 9. console
            print("== 9. console ==")
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
