import json
import csv
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
STORE_ID = 30717958
STORE_URL = f"https://www.doordash.com/store/erika's-flowers-&-events-white-plains-{STORE_ID}/40935151/"
COOKIE_FILE = "cookies.json"
OUTPUT_CSV = "doordash_menu_with_images.csv"
API_RESPONSE_JSON = "api_response.json"

def fetch_api_data_in_browser(page):
    print("🚀 Making direct API call from within the authenticated browser session...")
    api_url = "https://www.doordash.com/graphql"
    payload = {
        "operationName": "getMenuBook",
        "variables": {"storeId": STORE_ID, "useNewMenuBook": True},
        "query": "query getMenuBook($storeId: ID!, $useNewMenuBook: Boolean!) { store(id: $storeId) { menuBook @include(if: $useNewMenuBook) { ...MenuFeed } itemUUIDToItemMap { uuid name description imageUrl price } __typename } }"
    }
    json_data = page.evaluate("async (args) => { const r = await fetch(args.url, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(args.payload) }); return r.json(); }", {"url": api_url, "payload": payload})
    if "errors" in json_data: raise RuntimeError(f"API returned errors: {json_data['errors']}")
    print("✅ API call successful!")
    return json_data

def scrape_with_session_file():
    print("🔎 Launching Playwright browser and loading session from file...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        try:
            with open(COOKIE_FILE, 'r') as f:
                cookies = json.load(f)

            # --- FIX for the sameSite error ---
            # This loop cleans the cookies to make them compatible with Playwright.
            valid_same_site_values = ["Strict", "Lax", "None"]
            for cookie in cookies:
                if "sameSite" in cookie and cookie["sameSite"] not in valid_same_site_values:
                    # Set a safe default value if the original is invalid
                    cookie["sameSite"] = "Lax" 
            # ------------------------------------

            context.add_cookies(cookies)
            print(f"✅ Successfully loaded and cleaned {len(cookies)} cookies from {COOKIE_FILE}")
            
        except FileNotFoundError:
            print(f"❌ ERROR: The cookie file '{COOKIE_FILE}' was not found. Please export it first.")
            return

        page = context.new_page()
        try:
            print(f"Navigating to URL: {STORE_URL}")
            page.goto(STORE_URL, timeout=60000)
            page.locator("#main-content").wait_for(timeout=30000)
            print("✅ Page loaded successfully using the cookie session.")

            api_data = fetch_api_data_in_browser(page)
            browser.close()
            return api_data
        except Exception as e:
            print(f"❌ An error occurred: {e}")
            page.screenshot(path="final_error_screenshot.png")
            browser.close()
            raise

def parse_api_response(json_data):
    rows = []
    try:
        item_map = json_data['data']['store']['itemUUIDToItemMap']
        for category in json_data['data']['store']['menuBook']['categories']:
            category_name = category['title']
            for item in category['items']:
                item_details = item_map.get(item['uuid'])
                if not item_details: continue
                price = item_details['price'] / 100 if item_details.get('price') else 0.0
                rows.append({'Category': category_name, 'Name': item_details['name'], 'Description': item_details.get('description', ''), 'Price (USD)': f"{price:.2f}", 'Image URL': item_details.get('imageUrl', '')})
        print(f"✅ Parsed {len(rows)} menu items.")
        return rows
    except Exception as e:
        print(f"❌ Could not parse API response: {e}")
        return []

def save_to_csv(rows, path=OUTPUT_CSV):
    if not rows: return
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"💾 Saved {len(rows)} rows to {path}")

if __name__ == "__main__":
    try:
        final_api_data = scrape_with_session_file()
        with open(API_RESPONSE_JSON, "w", encoding="utf-8") as f:
            json.dump(final_api_data, f, indent=2, ensure_ascii=False)
        menu_items = parse_api_response(final_api_data)
        save_to_csv(menu_items)
        print("\n--- Scraping process completed successfully! ---")
    except Exception as e:
        # NEW CODE
        print(f"\n--- Scraping process failed: {e} ---")