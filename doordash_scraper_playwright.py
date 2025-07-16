#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Full DoorDash Menu Scraper (Playwright edition):
1) Launches a Chromium browser (headless by default, but will fall back to headed once
   if Cloudflare/login blocks headless).
2) Persists cookies & localStorage in cookies.json so future runs stay logged in.
3) Lazy-scrolls the store page, grabs the rendered HTML, prettifies it to page_new.html.
4) Parses BeautifulSoup + JSON-LD for menu items + image URLs, writes doordash_menu_with_images.csv.
5) Hands off to manualv2 to upload to Google Sheets and returns (sheet_url, tab_name, csv_copy).
"""
import json
import re
import csv
import time
import shutil
import os
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import manualv2

# ─── CONFIG ────────────────────────────────────────────────────────────────
STORE_URL    = "https://www.doordash.com/store/erika's-flowers-&-events-white-plains-30717958/40935151/"
COOKIE_FILE  = "cookies.json"
OUTPUT_CSV   = "doordash_menu_with_images.csv"
PAGE_HTML    = "page_new.html"
# ─────────────────────────────────────────────────────────────────────────────


def load_cookies(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        return []


def save_cookies(context, path=COOKIE_FILE):
    context.storage_state(path=path)


def save_prettified_html(raw_html, path=PAGE_HTML):
    """Prettify and save the full page HTML for offline inspection."""
    soup = BeautifulSoup(raw_html, 'html.parser')
    pretty = soup.prettify()
    with open(path, 'w', encoding='utf-8') as f:
        f.write(pretty)
    print(f"💾 Saved prettified HTML to {path}")


def clean_image_url(url):
    if not url:
        return ''
    base = url.split('?', 1)[0]
    parsed = urlparse(base)
    return base if parsed.scheme in ('http', 'https') else ''


def extract_items(text, typename):
    """Brace-counting JSON extractor for __typename blobs."""
    items = []
    pat = rf'"__typename"\s*:\s*"{typename}"'
    for m in re.finditer(pat, text):
        start = text.rfind('{', 0, m.start())
        if start < 0:
            continue
        depth = 0
        end = None
        for i, ch in enumerate(text[start:], start):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if not end:
            continue
        raw = text[start:end]
        try:
            items.append(json.loads(raw))
        except json.JSONDecodeError:
            pass
    return items


def build_image_lookup(html):
    """
    Build a name→image URL map by scanning:
      A) GraphQL blobs
      B) Next.js __NEXT_DATA__ Apollo cache
      C) name+imageUrl regex
      D) <img alt="...">
      E) inline background-image styles
    """
    lookup = {}
    soup = BeautifulSoup(html, 'html.parser')
    text = html

    # A) GraphQL blobs
    for blob in extract_items(text, 'StorePageCarouselItem') + extract_items(text, 'MenuPageItem'):
        name = blob.get('name', '').strip()
        url = blob.get('imgUrl') or blob.get('imageUrl') or ''
        if name and url:
            lookup.setdefault(name, clean_image_url(url.strip()))

    # B) Apollo cache
    nd = soup.find('script', id='__NEXT_DATA__')
    if nd and nd.string:
        try:
            data = json.loads(nd.string)
            ap = data.get('props', {}).get('apolloState', {}) or {}
            added = 0
            for obj in ap.values():
                if isinstance(obj, dict) and obj.get('__typename') in ('StorePageCarouselItem','MenuPageItem'):
                    name = obj.get('name','').strip()
                    url = obj.get('imgUrl') or obj.get('imageUrl') or ''
                    if name and url and name not in lookup:
                        lookup[name] = clean_image_url(url.strip())
                        added += 1
            if added:
                print(f"↳ Added {added} images from Apollo cache")
        except Exception:
            pass

    # C) regex fallback
    for m in re.finditer(
        r'{\s*"name"\s*:\s*"([^"]+)"[^}]+?"imageUrl"\s*:\s*"([^"]+)"',
        text
    ):
        n, u = m.group(1).strip(), m.group(2).strip()
        lookup.setdefault(n, clean_image_url(u))

    # D) <img alt=...>
    for img in soup.find_all('img', alt=True):
        n = img['alt'].strip()
        if not n or n in lookup:
            continue
        for attr in ('src','data-src','data-lazy-src'):
            v = img.get(attr)
            if v:
                lookup[n] = clean_image_url(v)
                break
        else:
            ss = img.get('srcset','').split(',')
            if ss and ss[0].strip():
                lookup[n] = clean_image_url(ss[0].split()[0])

    # E) inline background-image
    style_re = re.compile(r'url\(["\']?(https?://[^)"\']+)')
    for el in soup.find_all(style=True):
        m = style_re.search(el['style'])
        if not m:
            continue
        name = (el.get('aria-label') or el.get('title') or el.get('alt') or '').strip()
        if name and name not in lookup:
            lookup[name] = clean_image_url(m.group(1))

    print(f"↳ Total images in lookup: {len(lookup)}")
    return lookup


def extract_menu_items(html):
    """Parse JSON-LD menu and enrich with images from lookup."""
    soup = BeautifulSoup(html, 'html.parser')
    menu = None
    for s in soup.find_all('script', type='application/ld+json'):
        try:
            jd = json.loads(s.string or s.get_text() or '')
            if jd.get('@type')=='Restaurant' and 'hasMenu' in jd:
                menu = jd['hasMenu']
                break
        except:
            pass
    if not menu:
        raise RuntimeError("Could not find JSON-LD menu")

    secs = menu.get('hasMenuSection', [])
    if secs and isinstance(secs[0], list):
        secs = [sec for sub in secs for sec in sub]

    lookup = build_image_lookup(html)
    rows = []
    for sec in secs:
        cat = sec.get('name','').strip()
        for mi in sec.get('hasMenuItem', []):
            nm = mi.get('name','').strip()
            if not nm:
                continue
            desc = mi.get('description','').strip()
            price = re.sub(r'[^\d.]','', str(mi.get('offers',{}).get('price','0')))
            rows.append({
                'Category':        cat,
                'Name':            nm,
                'Description':     desc,
                'Price (USD)':     price,
                'Image URL':       lookup.get(nm, '')
            })
    print(f"✅ Parsed {len(rows)} menu items from JSON-LD + images")
    return rows


def save_to_csv(rows, path=OUTPUT_CSV):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    got = sum(1 for r in rows if r.get('Image URL'))
    print(f"💾 Saved {len(rows)} rows ({got} images) to {path}")


def scrape_with_playwright(seed_ui: bool = False):
    """
    Fetch the full rendered HTML via Playwright.
    headless by default, but if it fails to locate JSON-LD in 30s,
    retry once in headed mode to seed cookies/login.
    """
    mode = "headed" if seed_ui else "headless"
    print(f"🔎 Starting {mode} Playwright…")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not seed_ui,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        # Load existing cookies state only when not seeding
        storage = COOKIE_FILE if (not seed_ui and os.path.exists(COOKIE_FILE)) else None
        context = browser.new_context(
            storage_state=storage,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/114.0.5735.199 Safari/537.36"
            )
        )
        page = context.new_page()

        page.goto(STORE_URL, wait_until="networkidle")

        # If we're in seed_ui mode, let the user solve CF/login, then save the new state
        if seed_ui:
            print("🔐 Please complete any Cloudflare / login challenge in this browser window, then press ENTER…")
            input()
            context.storage_state(path=COOKIE_FILE)
            print(f"💾 Saved session to {COOKIE_FILE}")

        # lazy-scroll to trigger JS loading
        for _ in range(5):
            page.evaluate("window.scrollBy(0, document.body.scrollHeight/5)")
            time.sleep(1)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(2)

        # wait up to 30s for JSON-LD menu script to appear
        try:
            page.wait_for_selector(
                'xpath=//script[contains(text(),"@type") and contains(text(),"hasMenu")]',
                timeout=30_000
            )
        except PlaywrightTimeoutError:
            browser.close()
            if not seed_ui:
                print("⚠️ Headless blocked, retrying in headed mode…")
                return scrape_with_playwright(seed_ui=True)
            else:
                raise RuntimeError("Could not find JSON-LD menu even in headed mode.")

        # grab the fully rendered HTML
        raw_html = page.content()

        # save cookies again after a successful scrape, so headless runs keep working
        context.storage_state(path=COOKIE_FILE)

        browser.close()
        return raw_html


def scrape_and_extract():
    raw_html = scrape_with_playwright(seed_ui=False)
    save_prettified_html(raw_html)
    rows = extract_menu_items(raw_html)
    return rows


def run(store_url):
    """
    1) scrape & extract → rows
    2) save CSV
    3) upload to Sheets via manualv2 → (sheet_url, tab_name)
    4) copy CSV to <tab_name>.csv and return (sheet_url, tab_name, csv_copy)
    """
    global STORE_URL
    STORE_URL = store_url

    rows = scrape_and_extract()
    save_to_csv(rows)

    # upload & snapshot
    manualv2.main()
    sheet_url, tab_name = manualv2.upload_to_sheets()

    safe = "".join(c if c.isalnum() or c in (' ','_','-') else "_" for c in tab_name)
    csv_copy = f"{safe}.csv"
    if os.path.exists(csv_copy):
        os.remove(csv_copy)
    shutil.copy(OUTPUT_CSV, csv_copy)

    return sheet_url, tab_name, csv_copy


if __name__ == "__main__":
    url, tab, copyfile = run(STORE_URL)
    print("Done!")
    print("Sheet:", url)
    print("Tab:", tab)
    print("Download CSV:", copyfile)