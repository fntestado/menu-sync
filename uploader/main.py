#!/usr/bin/env python3
# uploader/main.py

import os
import requests
import pandas as pd
from pathlib import Path
from playwright.sync_api import sync_playwright

IMAGES_DIR   = "images"
COOKIES_FILE = "orders_auth.json"

class NotLoggedInError(Exception):
    """Raised when Orders.co still asks for credentials."""
    pass

def upload_to_orders(csv_path: str):
    """
    1) Reads the CSV of scraped menu items
    2) Logs into Orders.co (re-uses cookies if available)
    3) Creates categories & items (with images) via the web UI
    4) Persists cookies back to disk
    """
    os.makedirs(IMAGES_DIR, exist_ok=True)
    df = pd.read_csv(csv_path)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # reuse your logged-in state if you have it
        if os.path.exists(COOKIES_FILE):
            context = browser.new_context(storage_state=COOKIES_FILE)
        else:
            context = browser.new_context()
        page = context.new_page()

        # go to the menu page
        page.goto("https://partners.orders.co/menu/overview", timeout=60000)
        page.wait_for_timeout(3000)

        # ── Detect login form ─────────────────────────────────────────
        if page.locator('input[name="email"]').is_visible():
            # wipe out any partial cookies for next time
            context.storage_state(path=COOKIES_FILE)
            browser.close()
            raise NotLoggedInError(
                "You must log in to Orders.co first (use the Login button below)."
            )

        # now we're sure we're authenticated
        for category, items in df.groupby("Category"):
            # 1) add or skip category
            try:
                print(f"📁 Adding category: {category}")
                page.click("text=Add")
                page.click("text=Add Category")
                page.wait_for_selector('input[name="name"]')
                page.fill('input[name="name"]', category)
                page.click("button:has-text('Save')")
                page.wait_for_timeout(2000)
            except:
                print(f"⚠️ Category '{category}' may already exist or failed to create")

            # 2) expand that category
            try:
                print(f"🔍 Expanding: {category}")
                li = page.locator(f'li.MuiListItem-root:has-text("{category}")').first
                li.scroll_into_view_if_needed()
                li.locator('svg[data-testid="ExpandMoreIcon"]').click()
                page.wait_for_timeout(1000)
            except Exception as e:
                print(f"❌ Could not expand '{category}': {e}")
                continue

            # 3) add each item under it
            for _, item in items.iterrows():
                try:
                    print(f"  ➕ Adding item: {item['Name']}")

                    # download image
                    img_path = Path(IMAGES_DIR) / f"{item['Name']}.jpg"
                    if not img_path.exists() and item.get("Image URL"):
                        resp = requests.get(item["Image URL"], timeout=30)
                        resp.raise_for_status()
                        img_path.write_bytes(resp.content)

                    # click the “+ Add Item”
                    add_btn = li.locator(
                        "xpath=following-sibling::div//div[@id='sortableElementAdd']"
                    )
                    add_btn.click()
                    page.wait_for_selector('input[name="name"]')

                    # fill in the item form
                    page.fill('input[name="name"]', item["Name"])
                    if img_path.exists():
                        page.locator("input[type='file']").set_input_files(str(img_path))
                        # wait for crop dialog & Save
                        page.wait_for_selector("text=Product Photo", timeout=5000)
                        page.click("button:has-text('Save')")
                        page.wait_for_timeout(1000)

                    page.fill('textarea[name="description"]', str(item.get("Description","")))
                    page.fill('input[name="price"]', str(item["Price (USD)"]))
                    # final save
                    page.click("button:has-text('Save')")
                    page.wait_for_timeout(3000)

                except Exception as e:
                    print(f"❌ Failed to add '{item['Name']}': {e}")

        print("✅ Done uploading all items!")
        context.storage_state(path=COOKIES_FILE)
        browser.close()

if __name__ == "__main__":
    upload_to_orders("doordash_menu_with_images_copy.csv")