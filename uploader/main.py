#!/usr/bin/env python3
# uploader/main.py

import os
import requests
import time
import pandas as pd
from pathlib import Path
from playwright.sync_api import sync_playwright
from playwright.sync_api import Page, TimeoutError

IMAGES_DIR   = "images"
COOKIES_FILE = "orders_auth.json"

class NotLoggedInError(Exception):
    """Raised when Orders.co still asks for credentials."""
    pass

def select_brand_and_location(page, brand: str, location: str):
    page.click("button#businessNewListAll")
    pop = page.locator(".MuiPopover-paper")
    pop.wait_for(state="visible")

    # pick the brand
    pop.locator("ul").first.locator(f"li:has-text('{brand}')").click()
    page.wait_for_timeout(500)

    # type in the location filter
    snippet = ", ".join(location.split(",")[:2])  # e.g. "35-35 Leverich Street b522, NY 11372"
    loc_textarea = pop.locator('textarea[placeholder="All Locations..."]').first
    loc_textarea.fill("")
    loc_textarea.type(snippet, delay=50)

    # immediately apply—Orders.co will auto-select the top match
    pop.locator("button#businessList, button:has-text('Apply')").click()
    page.wait_for_timeout(1000)


def upload_to_orders(csv_path: str, brand: str, location: str):
    """
    1) Reads the CSV of scraped menu items
    2) Logs into Orders.co (re-uses cookies if available)
    3) Selects the given brand & location
    4) Creates categories & items (with images) via the web UI,
       skipping any that already exist
    5) Persists cookies back to disk
    """
    os.makedirs(IMAGES_DIR, exist_ok=True)
    df = pd.read_csv(csv_path)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = (
            browser.new_context(storage_state=COOKIES_FILE)
            if os.path.exists(COOKIES_FILE)
            else browser.new_context()
        )
        page = context.new_page()

        # ── NAVIGATE & AUTH ────────────────────────────────────
        page.goto("https://partners.orders.co/menu/overview", timeout=60000)
        page.wait_for_timeout(3000)
        if page.locator('input[name="email"]').is_visible():
            context.storage_state(path=COOKIES_FILE)
            browser.close()
            raise NotLoggedInError("You must log in to Orders.co first.")

        # ── SELECT BRAND & LOCATION ────────────────────────────
        select_brand_and_location(page, brand, location)

        # ── PROCESS EACH CATEGORY ──────────────────────────────
        for category, items in df.groupby("Category"):
            # 1) Category existence
            cat_locator = page.locator(f'li.MuiListItem-root:has-text("{category}")')
            if cat_locator.count() == 0:
                print(f"📁 Creating category: {category}")
                page.click("text=Add")
                page.click("text=Add Category")
                page.wait_for_selector('input[name="name"]')
                page.fill('input[name="name"]', category)
                page.click("button:has-text('Save')")
                page.wait_for_timeout(2000)
            else:
                print(f"✅ Category already exists: {category}")

            # 2) Expand category
            try:
                print(f"🔍 Expanding: {category}")
                li = page.locator(f'li.MuiListItem-root:has-text("{category}")').first
                li.scroll_into_view_if_needed()
                if li.locator('svg[data-testid="ExpandMoreIcon"]').count():
                    li.locator('svg[data-testid="ExpandMoreIcon"]').click()
                    page.wait_for_timeout(1000)
            except Exception as e:
                print(f"❌ Could not expand '{category}': {e}")
                continue

            # 3) Build set of existing names under this category
            items_panel = li.locator("xpath=following-sibling::div[1]")
            existing = {
                text.strip().lower()
                for text in items_panel
                    .locator("xpath=.//p[contains(@class,'css-gp1sl7')]")
                    .all_text_contents()
            }

            # 4) Loop through new items, skip ones already in `existing`
            for _, item in items.iterrows():
                name = item["Name"].strip()
                key  = name.lower()
                if key in existing:
                    print(f"✅ Skipping existing item under '{category}': {name}")
                    continue

                print(f"  ➕ Creating item: {name}")

                # download image
                img_path = Path(IMAGES_DIR) / f"{name}.jpg"
                if not img_path.exists() and item.get("Image URL"):
                    resp = requests.get(item["Image URL"], timeout=30)
                    resp.raise_for_status()
                    img_path.write_bytes(resp.content)

                # click “+ Add Item”
                add_btn = li.locator(
                    "xpath=following-sibling::div[1]//div[@id='sortableElementAdd']"
                )
                add_btn.click()
                page.wait_for_selector('input[name="name"]')

                # fill form
                page.fill('input[name="name"]', name)
                if img_path.exists():
                    page.locator("input[type='file']").set_input_files(str(img_path))
                    crop = page.locator("div.MuiDialog-root:has-text('Product Photo')")
                    crop.wait_for(state="visible", timeout=60000)
                    crop.locator("button:has-text('Save')").click()
                    crop.wait_for(state="hidden", timeout=60000)

                page.fill('textarea[name="description"]', str(item.get("Description","")))
                page.fill('input[name="price"]', str(item["Price (USD)"]))

                # save
                page.locator("button#formFooterOrdersUpdate").click()
                page.wait_for_timeout(3000)

                # mark created
                existing.add(key)

        print("✅ Done uploading all items!")
        context.storage_state(path=COOKIES_FILE)
        browser.close()

def scrape_all_brand_locations(pause: float = 0.5) -> dict:
    """
    1) Opens the All-Brands/All-Locations popover
    2) Clicks the brand input to load actual brand options
    3) Filters out any 'No options' placeholder
    4) For each real brand:
         • clicks it
         • waits a moment
         • scrapes *all* its locations
    Returns a dict { brand_name: [ {"name":…, "address":…}, … ], … }
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx     = browser.new_context(storage_state="orders_auth.json")
        page    = ctx.new_page()
        page.goto("https://partners.orders.co/menu/overview")

        if page.locator('input[name="email"]').is_visible():
            raise RuntimeError("Please log in first")
    
        # 1) open the picker
        page.click("button#businessNewListAll")
        page.wait_for_selector(".MuiPopover-paper ul")

        pop = page.locator(".MuiPopover-paper")
        brands_ul = pop.locator("ul").first
        locs_ul   = pop.locator("ul").nth(1)

        # 2) focus the brand input so that real options appear
        pop.locator('input[placeholder="All Brands..."]').click()
        # wait for at least one real brand to show up
        page.wait_for_selector('ul li.MuiListItem-root:not(:has-text("No options"))')

        # 3) collect the indices of real-brand <li>s
        total = brands_ul.locator("li").count()
        real_indices = []
        for i in range(total):
            text = brands_ul.locator("li").nth(i).inner_text().strip()
            if text and text.lower() != "no options":
                real_indices.append(i)

        result = {}
        # 4) for each brand, click & scrape locations
        for idx in real_indices:
            li = brands_ul.locator("li").nth(idx)
            brand_name = li.inner_text().strip()

            li.click()
            time.sleep(pause)

            # scrape this brand's locations
            locs = []
            loc_count = locs_ul.locator("li").count()
            for j in range(loc_count):
                item = locs_ul.locator("li").nth(j)
                name    = item.locator("p").nth(0).inner_text().strip()
                address = item.locator("p").nth(1).inner_text().strip()
                locs.append({"name": name, "address": address})

            result[brand_name] = locs

        # close the popover
        pop.locator("button#businessList, button:has-text('Apply')").click()
        return result

if __name__ == "__main__":
    # upload_to_orders("doordash_menu_with_images_copy.csv")
    # with sync_playwright() as p:
    #     browser = p.chromium.launch(headless=True)
    #     ctx     = browser.new_context(storage_state="orders_auth.json")
    #     page    = ctx.new_page()
    #     page.goto("https://partners.orders.co/menu/overview")

    #     if page.locator('input[name="email"]').is_visible():
    #         raise RuntimeError("Please log in first")

    # brands_and_locs = scrape_all_brand_locations()
    # for brand, locs in brands_and_locs.items():
    #     print(brand)
    #     for l in locs:
    #         print("  –", l["name"], "|", l["address"])

    # browser.close()

    # pass in the brand & location you want to target:
    upload_to_orders(
        "doordash_menu_with_images_copy.csv",
        brand="Bomi - Flowers, Plants & Gift Shop",
        location="89 East 4th Street., New York, NY 10003"
    )