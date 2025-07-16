#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
uploader/login.py

Launch a headed browser so you can solve the
Cloudflare/login challenge and then, as soon
as you close the tab, persist your Orders.co
session to orders_auth.json.
"""

from playwright.sync_api import sync_playwright, TimeoutError

STORE_URL    = "https://partners.orders.co/"
COOKIES_FILE = "orders_auth.json"

def login_orders():
    """
    1) Open a headed browser and navigate to STORE_URL
    2) If already on the dashboard, save cookies & exit
    3) Otherwise wait for you to finish login, then detect
       the tab being closed and finally persist storage_state
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        # 1) trigger CF / login
        page.goto(STORE_URL, timeout=60000)

        # 2) quick pre-check: are we already on the dashboard?
        try:
            page.wait_for_selector("text=Menu Overview", timeout=10000)
            print("✅ You appear to already be logged in.")
            # save immediately
            context.storage_state(path=COOKIES_FILE)
            print(f"💾  Saved your session to {COOKIES_FILE}")
            browser.close()
            return
        except TimeoutError:
            pass

        # 3) not logged in yet—hand off to the user
        print("🔐 Please complete any Cloudflare/login flows in the browser.")
        print("   When you’re done, simply **close this tab** to finish.")

        # 4) wait until the user closes that tab
        page.wait_for_event("close")
        print("✅ Detected tab/window close — assuming login is complete.")

        # 5) now persist cookies + localStorage
        context.storage_state(path=COOKIES_FILE)
        print(f"💾  Saved your session to {COOKIES_FILE}")

        # clean up
        try:
            browser.close()
        except:
            pass

if __name__ == "__main__":
    login_orders()