#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Full DoorDash Menu Scraper:
1) Uses undetected-chromedriver + Selenium to load and lazy-scroll the store page,
   then saves a prettified HTML dump to page.html.
2) Parses that prettified HTML with BeautifulSoup + regex/JSON-LD to extract
   menu items + images, writing results to doordash_menu_with_images.csv.
"""
import json
import re
import csv
import time
import shutil
import os
import cloudscraper
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium_stealth import stealth
from urllib.parse import urlparse
from manualv2 import SERVICE_ACCOUNT_FILE, SPREADSHEET_ID, SCOPES, OUTPUT_CSV

import manualv2

# ─── CONFIG ────────────────────────────────────────────────────────────────
STORE_URL   = "https://www.doordash.com/store/erika's-flowers-&-events-white-plains-30717958/40935151/"
COOKIE_FILE = "cookies.json"              # Optional session cookies
OUTPUT_CSV  = "doordash_menu_with_images.csv"
PAGE_HTML   = "page_new.html"                 # Prettified HTML dump
# ─────────────────────────────────────────────────────────────────────────────

def load_cookies(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


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
    base = url.split('?',1)[0]
    parsed = urlparse(base)
    return base if parsed.scheme in ('http','https') else ''


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
        for i,ch in enumerate(text[start:], start):
            if ch=='{': depth += 1
            elif ch=='}':
                depth -= 1
                if depth == 0:
                    end = i+1
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
      A) StorePageCarouselItem + MenuPageItem JSON blobs
      B) Next.js __NEXT_DATA__ Apollo cache
      C) Quick name+imageUrl regex
      D) <img alt="Name" ...> tags
      E) inline background-image styles
    """
    lookup = {}
    soup = BeautifulSoup(html, 'html.parser')
    text = html

    # A) GraphQL blobs
    for blob in extract_items(text, 'StorePageCarouselItem') + extract_items(text, 'MenuPageItem'):
        name = blob.get('name','').strip()
        url  = blob.get('imgUrl') or blob.get('imageUrl') or ''
        if name and url:
            lookup.setdefault(name, clean_image_url(url.strip()))

    # B) Apollo cache
    nd = soup.find('script', id='__NEXT_DATA__')
    if nd and nd.string:
        try:
            data = json.loads(nd.string)
            ap = data.get('props',{}).get('apolloState',{}) or {}
            added = 0
            for obj in ap.values():
                if isinstance(obj, dict) and obj.get('__typename') in ('StorePageCarouselItem','MenuPageItem'):
                    name = obj.get('name','').strip()
                    url  = obj.get('imgUrl') or obj.get('imageUrl') or ''
                    if name and url and name not in lookup:
                        lookup[name] = clean_image_url(url.strip())
                        added += 1
            if added:
                print(f"↳ Added {added} images from Apollo cache")
        except Exception:
            pass

    # C) regex fallback
    for m in re.finditer(r'{\s*"name"\s*:\s*"([^"]+)"[^}]+?"imageUrl"\s*:\s*"([^"]+)"', text):
        n,u = m.group(1).strip(), m.group(2).strip()
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
    # find JSON-LD
    menu = None
    for s in soup.find_all('script', type='application/ld+json'):
        try:
            jd = json.loads(s.string or s.get_text() or '')
            if jd.get('@type')=='Restaurant' and 'hasMenu' in jd:
                menu = jd['hasMenu']; break
        except:
            pass
    if not menu:
        raise RuntimeError("Could not find JSON-LD menu")

    # flatten
    secs = menu.get('hasMenuSection', [])
    if secs and isinstance(secs[0], list):
        secs = [sec for sub in secs for sec in sub]

    lookup = build_image_lookup(html)
    rows = []
    for sec in secs:
        cat = sec.get('name','').strip()
        for mi in sec.get('hasMenuItem', []):
            nm   = mi.get('name','').strip()
            if not nm:
                continue
            desc = mi.get('description','').strip()
            price = re.sub(r'[^\d.]', '', str(mi.get('offers',{}).get('price','0')))
            rows.append({
                'Category':cat,
                'Name':nm,
                'Description':desc,
                'Price (USD)':price,
                'Image URL': lookup.get(nm, '')
            })
    print(f"✅ Parsed {len(rows)} menu items from JSON-LD + images")
    return rows


def save_to_csv(rows, path=OUTPUT_CSV):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    got = sum(1 for r in rows if r.get('Image URL'))
    print(f"💾 Saved {len(rows)} rows ({got} images) to {path}")

def scrape_and_extract(seed_ui: bool = False):
    """
    Fetch & prettify the menu page, retrying once with UI if headless fails.
    seed_ui=True forces headed Chrome (for CF cookie seeding).
    """
    opts = uc.ChromeOptions()
    # headless unless explicitly seeded UI
    opts.headless = not seed_ui
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    # override headless UA token
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/96.0.4664.110 Safari/537.36"
    )

    mode = "headed" if seed_ui else "headless"
    print(f"🔎 Starting {mode} Chrome…")

    driver = uc.Chrome(options=opts)
    # patch navigator webdriver and other flags
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": (
            "Object.defineProperty(navigator, 'webdriver', {get:() => undefined});"
            "window.navigator.chrome = {runtime:{}};"
            "Object.defineProperty(navigator, 'plugins', {get:() => [1,2,3,4,5]});"
            "Object.defineProperty(navigator, 'languages', {get:() => ['en-US','en']});"
        )
    })

    raw = None
    try:
        driver.get("https://www.doordash.com")
        for ck in load_cookies(COOKIE_FILE):
            try:
                driver.add_cookie(ck)
            except:
                pass

        driver.get(STORE_URL)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.TAG_NAME, 'body'))
        )

        # lazy scroll to force rendering
        for _ in range(5):
            driver.execute_script("window.scrollBy(0, document.body.scrollHeight/5)")
            time.sleep(1)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(2)

        # wait up to 30s for JSON-LD marker
        deadline = time.time() + 30
        while time.time() < deadline:
            raw = driver.page_source
            if '"@type":"Restaurant"' in raw and '"hasMenu"' in raw:
                break
            time.sleep(2)
        else:
            raise RuntimeError("Could not find JSON-LD menu after waiting 30s")

    except Exception as err:
        driver.quit()
        if not seed_ui:
            print("⚠️ Headless failed, retrying in headed mode…")
            return scrape_and_extract(seed_ui=True)
        raise
    finally:
        if raw:
            save_prettified_html(raw)
        driver.quit()

    return extract_menu_items(raw)

def run(store_url):
    """
    1) Set the target URL
    2) scrape & extract → rows
    3) save CSV
    4) append new tab to existing spreadsheet → returns (sheet_url, tab_name)
    5) copy the CSV locally to <tab_name>.csv and return that too
    """
    global STORE_URL
    STORE_URL = store_url

    # 1–3) scrape + extract + write doordash_menu_with_images.csv
    rows = scrape_and_extract()
    save_to_csv(rows)

    # 4) upload & get back sheet_url, tab_name
    manualv2.main()
    sheet_url, tab_name = manualv2.upload_to_sheets()

    # 5) copy to a new file named after the tab
    safe_name = "".join(c if c.isalnum() or c in (' ','_','-') else "_" for c in tab_name)
    csv_copy = f"{safe_name}.csv"
    # ensure no collision
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
