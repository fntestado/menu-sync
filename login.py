#!/usr/bin/env python3
from playwright.sync_api import sync_playwright

STORE_URL    = "https://www.doordash.com/store/millennial-florist-corp.-hartsdale-2796849/"
COOKIES_FILE = "doordash_cookies.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()

    # 1) Navigate straight to the store page (triggers CF)
    page.goto(STORE_URL)

    # 2) Pause so you can solve the “I’m human” checkbox challenge
    input("🔐  Solve the Cloudflare challenge, then press ENTER…")

    # 3) Wait for a selector that only exists on the real store page
    #    (for example, the store title or a menu section)
    page.wait_for_selector("h1[data-anchor-id='header-homepage-link']", timeout=60000)

    # 4) (Optional) reload so that the clearance cookie is fully applied server-side
    page.reload()
    page.wait_for_load_state("networkidle")

    # 5) Save your authenticated state
    context.storage_state(path=COOKIES_FILE)
    print(f"💾  Saved your session (including cf_clearance) to {COOKIES_FILE}")

    browser.close()