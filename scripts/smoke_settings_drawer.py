#!/usr/bin/env python3
"""smoke_settings_drawer.py — browser smoke for the Settings drawer (Task C).

Covers Task C DoD:
- Drawer opens/closes (Esc / backdrop / x)
- Model fields render from GET /api/settings
- Key masked; reveal shows typed value only (never prefilled)
- Test flow staged (uses current form values), never saves
- Dirty tracking starts (dirty chip + state)

Self-contained: creates a THROWAWAY temp env (docs/KB/history/settings DB),
spawns its own server, runs the Playwright flow, then tears everything down.
The real data/ dirs are never touched. POST /api/settings/test is intercepted
in-browser (Step B owns the real endpoint, running in parallel).

Usage:
  .venv/bin/python scripts/smoke_settings_drawer.py

Env:
  QYD_FE_PORT   port to bind the throwaway server (default 8895)
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
PY = REPO / ".venv" / "bin" / "python"
PORT = int(os.environ.get("QYD_FE_PORT", "8895"))
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
        # backend error envelopes are still JSON ({ok:false, error:{...}})
        return json.loads(e.read().decode())


def main() -> None:
    from playwright.sync_api import sync_playwright

    tmp = Path(tempfile.mkdtemp(prefix="qyd-settings-smoke-"))
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
        [str(PY), str(REPO / "server.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_health():
            out = proc.stdout.read(2000).decode(errors="replace")
            print("server boot failed; log:", out)
            return 1

        # Seed settings with a saved key so has_key is true (matches real user state).
        seed = http_json("/api/settings", "PUT", {
            "model": {"name": "deepseek-v4-flash", "base_url": "", "api_key": "sk-secret"},
            "persona": {"preset": "concise", "custom": ""},
            "retrieval": {"top_k": 4, "chunk_size": 600},
            "appearance": {"theme": "light", "language": "en"},
        })
        check("seed PUT ok", seed.get("ok") is True, str(seed)[:200])
        seeded_view = seed.get("data", {})
        check("seed GET has_key only (no raw key)",
              seeded_view.get("model", {}).get("api_key", {}).get("has_key") is True
              and "sk-secret" not in json.dumps(seed), json.dumps(seed)[:200])

        console_errors: list[str] = []
        page_errors: list[str] = []
        captured = {"test_body": None}  # mutable container so route closures can write it

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(30_000)

            def on_console(msg):
                t = msg.type
                text = msg.text
                if t == "error":
                    # favicon + resource misses (incl. real-endpoint 502 connection-failed
                    # which the app handles inline) are expected noise
                    if "favicon" in text or ("Failed to load resource" in text and ("404" in text or "502" in text)):
                        return
                    console_errors.append(text)

            def on_pageerror(exc):
                page_errors.append(str(exc))

            page.on("console", on_console)
            page.on("pageerror", on_pageerror)

            # Intercept the test endpoint (Step B owns it; this proves the staged flow).
            def on_test_route(route):
                req = route.request
                if req.method == "POST":
                    try:
                        captured["test_body"] = json.loads(req.post_data or "{}")
                    except Exception:
                        captured["test_body"] = None
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"ok": True, "data": {"latency_ms": 137}}),
                )

            def on_test_route_fail(route):
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"ok": False, "error": {"code": "connection-failed", "message": "401 Unauthorized"}}),
                )

            # ------------------------------------------------------------- 1. boot + gear
            print("== 1. boot / gear entry ==")
            resp = page.goto(BASE + "/", wait_until="load")
            check("GET / serves index.html", resp is not None and resp.status == 200)
            page.wait_for_function("window.QYD && window.QYD.state.config !== null")

            check("gear button present", page.locator("#settingsBtn").count() == 1)
            gear_first = page.evaluate(
                "document.querySelector('#settingsBtn') === document.querySelector('.topbar-actions').firstElementChild"
            )
            check("gear is first action in topbar-actions", gear_first)
            check("gear aria-label Settings", page.locator("#settingsBtn").get_attribute("aria-label") == "Settings")
            gear_box = page.locator("#settingsBtn").bounding_box()
            check("gear target >= 32px", gear_box is not None and gear_box["width"] >= 32 and gear_box["height"] >= 32, str(gear_box))

            # served == disk (cache-bust v3)
            for f in ("index.html", "style.css", "app.js"):
                got = page.evaluate(f"fetch('/static/{f}').then(r=>r.text())")
                disk = (REPO / "static" / f).read_text(encoding="utf-8")
                check(f"static/{f} served == disk", got == disk)
            check("cache-bust v=3", "v=3" in (REPO / "static" / "index.html").read_text(encoding="utf-8"))

            # ------------------------------------------------------------- 2. open
            print("== 2. drawer opens ==")
            page.locator("#settingsBtn").click()
            page.wait_for_selector("#settingsDrawer", state="visible", timeout=5_000)
            page.wait_for_timeout(300)  # slide transition
            check("drawer visible", page.locator("#settingsDrawer").is_visible())
            check("drawer role=dialog", page.locator(".drawer").get_attribute("role") == "dialog")
            check("drawer aria-modal", page.locator(".drawer").get_attribute("aria-modal") == "true")
            dw = page.locator(".drawer").bounding_box()
            check("drawer width 400px desktop", dw is not None and abs(dw["width"] - 400) < 1, str(dw))
            bd = page.locator("#settingsDrawer").bounding_box()
            check("backdrop full-screen", bd is not None and bd["width"] == 1440 and bd["height"] == 900, str(bd))
            # chat panel still visible behind (no layout shift)
            chat_vis = page.evaluate("!!document.querySelector('#chatPane').getBoundingClientRect().width")
            check("chat visible behind drawer", chat_vis)

            # ------------------------------------------------------------- 3. fields from GET
            print("== 3. model fields render from GET ==")
            page.wait_for_function("window.QYD.state.settings.loaded === true", timeout=10_000)
            check("model name from GET", page.locator("#settingsModelName").input_value() == "deepseek-v4-flash")
            check("base url from GET (empty)", page.locator("#settingsBaseUrl").input_value() == "")
            key_val = page.locator("#settingsApiKey").input_value()
            check("api key NEVER prefilled", key_val == "", repr(key_val))
            check("api key type=password", page.locator("#settingsApiKey").get_attribute("type") == "password")
            check("Saved chip shown (has_key)", page.locator("#settingsKeySaved").is_visible())
            check("placeholder masked dots", "\u2022" in page.locator("#settingsApiKey").get_attribute("placeholder"))
            check("dirty chip hidden on load", page.locator("#settingsDirtyChip").is_hidden())

            # ------------------------------------------------------------- 4. reveal = typed only
            print("== 4. key reveal shows typed only ==")
            page.fill("#settingsApiKey", "sk-typed-abc")
            check("typed value stays masked", page.locator("#settingsApiKey").get_attribute("type") == "password")
            page.locator("#settingsKeyReveal").click()
            check("reveal -> type text", page.locator("#settingsApiKey").get_attribute("type") == "text")
            check("reveal shows typed value", page.locator("#settingsApiKey").input_value() == "sk-typed-abc")
            check("reveal aria-label Hide", page.locator("#settingsKeyReveal").get_attribute("aria-label") == "Hide API key")
            page.locator("#settingsKeyReveal").click()
            check("toggle back -> password", page.locator("#settingsApiKey").get_attribute("type") == "password")
            check("Saved chip hidden while typed", page.locator("#settingsKeySaved").is_hidden())

            # ------------------------------------------------------------- 5. staged test (success)
            print("== 5. test flow staged, no save ==")
            page.unroute("**/api/settings/test")
            page.route("**/api/settings/test", on_test_route)
            page.fill("#settingsModelName", "gpt-test-model")
            page.fill("#settingsBaseUrl", "https://api.example.com/v1")
            page.locator("#settingsTestBtn").click()
            page.wait_for_function("window.QYD.state.settings.testing === false && window.QYD.state.settings.testStatus !== null", timeout=10_000)
            status_txt = page.locator("#settingsTestStatus").inner_text()
            check("test success shows Connected", "Connected" in status_txt and "137ms" in status_txt, status_txt)
            check("test used staged name", captured["test_body"] is not None and captured["test_body"].get("name") == "gpt-test-model", str(captured["test_body"]))
            check("test used staged base_url", captured["test_body"] is not None and captured["test_body"].get("base_url") == "https://api.example.com/v1", str(captured["test_body"]))
            check("test used staged key", captured["test_body"] is not None and captured["test_body"].get("api_key") == "sk-typed-abc", str(captured["test_body"]))
            # no save: GET still returns the ORIGINAL seeded values
            after = http_json("/api/settings")
            m = after.get("data", {}).get("model", {})
            check("test did NOT save name", m.get("name") == "deepseek-v4-flash", json.dumps(m))
            check("test did NOT save base_url", m.get("base_url") == "", json.dumps(m))
            check("test did NOT save key (has_key unchanged)", m.get("api_key", {}).get("has_key") is True, json.dumps(m))
            # dirty still true (nothing saved)
            check("dirty persists after test", page.evaluate("window.QYD.state.settings.dirty") is True)

            # ------------------------------------------------------------- 5b. staged test (failure)
            print("== 5b. test flow failure ==")
            page.unroute("**/api/settings/test")
            page.route("**/api/settings/test", on_test_route_fail)
            page.locator("#settingsTestBtn").click()
            page.wait_for_function(
                "window.QYD.state.settings.testing === false && window.QYD.state.settings.testStatus && window.QYD.state.settings.testStatus.kind === 'err'",
                timeout=10_000,
            )
            fail_txt = page.locator("#settingsTestStatus").inner_text()
            check("test failure shows inline error", "Connection failed" in fail_txt and "401" in fail_txt, fail_txt)
            after2 = http_json("/api/settings")
            check("failure did NOT save either", after2.get("data", {}).get("model", {}).get("name") == "deepseek-v4-flash")
            page.unroute("**/api/settings/test")

            # ------------------------------------------------------------- 5c. live endpoint (real backend)
            print("== 5c. live POST /api/settings/test (real backend) ==")
            live = http_json("/api/settings/test", "POST", {"name": "deepseek-v4-flash", "base_url": ""})
            if live.get("ok") is True:
                check("live endpoint ok:true", True, json.dumps(live))
                check("live endpoint latency_ms number", isinstance(live.get("data", {}).get("latency_ms"), (int, float)), json.dumps(live))
            elif live.get("error", {}).get("code") == "connection-failed":
                check("live endpoint connection-failed code (no key / unreachable)", True, json.dumps(live)[:200])
            else:
                check("live endpoint reachable", False, json.dumps(live)[:200])
            # no save from live test either
            after3 = http_json("/api/settings")
            check("live test did NOT save", after3.get("data", {}).get("model", {}).get("name") == "deepseek-v4-flash", json.dumps(after3)[:200])

            # in-browser live test uses the REAL endpoint (no interception)
            page.fill("#settingsModelName", "deepseek-v4-flash")
            page.fill("#settingsBaseUrl", "")
            page.fill("#settingsApiKey", "")
            page.locator("#settingsTestBtn").click()
            page.wait_for_function(
                "window.QYD.state.settings.testing === false && window.QYD.state.settings.testStatus !== null",
                timeout=15_000,
            )
            live_txt = page.locator("#settingsTestStatus").inner_text()
            check("in-browser live test shows status", live_txt.strip() != "", live_txt)
            check("in-browser live test no pageerror", len(page_errors) == 0, str(page_errors))

            # ------------------------------------------------------------- 6. dirty tracking
            print("== 6. dirty tracking ==")
            # (5c reset the form to saved values; edit again to prove the chip)
            page.fill("#settingsModelName", "dirty-test-model")
            page.wait_for_timeout(150)
            check("dirty chip visible after edit", page.locator("#settingsDirtyChip").is_visible())
            # revert name/base_url to saved -> clean again
            page.fill("#settingsModelName", "deepseek-v4-flash")
            page.fill("#settingsBaseUrl", "")
            page.fill("#settingsApiKey", "")
            page.wait_for_timeout(150)
            check("dirty clears when reverted", page.evaluate("window.QYD.state.settings.dirty") is False)
            check("dirty chip hidden when clean", page.locator("#settingsDirtyChip").is_hidden())

            # ------------------------------------------------------------- 7. close paths
            print("== 7. close: x / Esc / backdrop ==")
            page.locator("#settingsCloseBtn").click()
            page.wait_for_selector("#settingsDrawer", state="hidden", timeout=5_000)
            check("x closes drawer", page.locator("#settingsDrawer").is_hidden())
            check("focus returns to gear", page.evaluate("document.activeElement && document.activeElement.id === 'settingsBtn'"))

            page.locator("#settingsBtn").click()
            page.wait_for_selector("#settingsDrawer", state="visible", timeout=5_000)
            page.keyboard.press("Escape")
            page.wait_for_selector("#settingsDrawer", state="hidden", timeout=5_000)
            check("Esc closes drawer", page.locator("#settingsDrawer").is_hidden())

            page.locator("#settingsBtn").click()
            page.wait_for_selector("#settingsDrawer", state="visible", timeout=5_000)
            page.mouse.click(30, 30)  # backdrop area (drawer is right side)
            page.wait_for_selector("#settingsDrawer", state="hidden", timeout=5_000)
            check("backdrop click closes drawer", page.locator("#settingsDrawer").is_hidden())

            # ------------------------------------------------------------- 8. responsive <640
            print("== 8. responsive (375x667) ==")
            page.set_viewport_size({"width": 375, "height": 667})
            page.wait_for_timeout(300)
            page.locator("#settingsBtn").click()
            page.wait_for_selector("#settingsDrawer", state="visible", timeout=5_000)
            page.wait_for_timeout(300)
            dwm = page.locator(".drawer").bounding_box()
            check("drawer full-width <640", dwm is not None and dwm["width"] == 375, str(dwm))
            name_h = page.evaluate("document.querySelector('#settingsModelName').getBoundingClientRect().height")
            key_h = page.evaluate("document.querySelector('#settingsApiKey').getBoundingClientRect().height")
            reveal_h = page.evaluate("document.querySelector('#settingsKeyReveal').getBoundingClientRect().height")
            test_h = page.evaluate("document.querySelector('#settingsTestBtn').getBoundingClientRect().height")
            check("touch targets >= 44px", min(name_h, key_h, reveal_h, test_h) >= 44, f"{name_h},{key_h},{reveal_h},{test_h}")
            no_h = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            check("no horizontal scroll", no_h)
            # gear reachable without sidebar
            check("gear visible on mobile", page.locator("#settingsBtn").is_visible())
            page.keyboard.press("Escape")
            page.wait_for_selector("#settingsDrawer", state="hidden", timeout=5_000)
            check("mobile Esc closes cleanly", page.locator("#settingsDrawer").is_hidden())

            # ------------------------------------------------------------- 9. console
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
