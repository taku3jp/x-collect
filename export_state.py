#!/usr/bin/env python3
"""Export X login session to state.json.

Run locally:
    pip install playwright
    playwright install chromium
    python export_state.py

A browser opens on x.com. Log in manually, then press Enter in this terminal.
The session is saved to state.json — paste its content into the GitHub Secret
X_STATE_JSON (do NOT commit this file).
"""

from playwright.sync_api import sync_playwright

STATE_PATH = "state.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto("https://x.com/login")
    input("Xにログインしてホームが表示されたら、ここでEnterを押してください: ")
    ctx.storage_state(path=STATE_PATH)
    browser.close()

print(f"saved: {STATE_PATH}")
