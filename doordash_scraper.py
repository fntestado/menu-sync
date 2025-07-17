#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Full DoorDash Menu Scraper with Proxy Support:
1) Uses undetected-chromedriver + Selenium to load and lazy-scroll the store page,
   through a residential proxy to bypass Cloudflare.
2) Runs inside a virtual X display on UI-less VPS.
3) Parses menu items + images and outputs CSV.
"""
import json
import re
import csv
import time
import shutil
import os
import manualv2
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from pyvirtualdisplay import Display
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium_stealth import stealth


# ─── CONFIG ────────────────────────────────────────────────────────────────
STORE_URL   = "https://www.doordash.com/store/erika's-flowers-&-events-white-plains-30717958/40935151/"
COOKIE_FILE = "cookies.json"
OUTPUT_CSV  = "doordash_menu_with_images.csv"
PAGE_HTML   = "page_new.html"
# ─────────────────────────────────────────────────────────────────────────────

def load_cookies(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_prettified_html(raw_html, path=PAGE_HTML):
    soup = BeautifulSoup(raw_html, 'html.parser')
    pretty = soup.prettify()
    with open(path, 'w', encoding='utf-8') as f:
        f.write(pretty)
    print(f"💾 Saved prettified HTML to {path}")


def clean_image_url(url):
    if not url:
        return ''
    base = url.split('?',1)[0]
    return base if urlparse(base).scheme in ('http','https') else ''


def extract_items(text, typename):
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
    lookup = {}
    soup = BeautifulSoup(html, 'html.parser')
    text = html
    # A) GraphQL blobs
    for blob in extract_items(text, 'StorePageCarouselItem') + extract_items(text, 'MenuPageItem'):
        name = blob.get('name','').strip()
        url  = blob.get('imgUrl') or blob.get('imageUrl') or ''
        if name and url:
            lookup.setdefault(name, clean_image_url(url))
    # B) Apollo cache
    nd = soup.find('script', id='__NEXT_DATA__')
    if nd and nd.string:
        try:
            data = json.loads(nd.string)
            ap = data.get('props',{}).get('apolloState',{}) or {}
            for obj in ap.values():
                if isinstance(obj, dict) and obj.get('__typename') in ('StorePageCarouselItem','MenuPageItem'):
                    name = obj.get('name','').strip()
                    url  = obj.get('imgUrl') or obj.get('imageUrl') or ''
                    if name and url and name not in lookup:
                        lookup[name] = clean_image_url(url)
        except Exception:
            pass
    # C) regex fallback
    for m in re.finditer(r'{\s*"name"\s*:\s*"([^"]+)"[^}]+?"imageUrl"\s*:\s*"([^"]+)"', text):
        lookup.setdefault(m.group(1).strip(), clean_image_url(m.group(2).strip()))
    # D/E) <img> + style
    for img in soup.find_all('img', alt=True):
        n = img['alt'].strip()
        if n and n not in lookup:
            for attr in ('src','data-src','data-lazy-src'):
                v = img.get(attr)
                if v:
                    lookup[n] = clean_image_url(v); break
    print(f"↳ Total images in lookup: {len(lookup)}")
    return lookup


def extract_menu_items(html):
    soup = BeautifulSoup(html, 'html.parser')
    menu = None
    for s in soup.find_all('script', type='application/ld+json'):
        try:
            jd = json.loads(s.string or s.get_text() or '')
            if jd.get('@type')=='Restaurant' and 'hasMenu' in jd:
                menu = jd['hasMenu']; break
        except: pass
    if not menu:
        raise RuntimeError("Could not find JSON-LD menu")
    secs = menu.get('hasMenuSection', [])
    if secs and isinstance(secs[0], list):
        secs = [sub for sec in secs for sub in (sec if isinstance(sec, list) else [sec])]
    lookup = build_image_lookup(html)
    rows = []
    for sec in secs:
        cat = sec.get('name','').strip()
        for mi in sec.get('hasMenuItem', []):
            nm = mi.get('name','').strip()
            if not nm: continue
            desc = mi.get('description','').strip()
            price = re.sub(r'[^\d.]','', str(mi.get('offers',{}).get('price','0')))
            rows.append({
                'Category':cat, 'Name':nm,
                'Description':desc, 'Price (USD)':price,
                'Image URL': lookup.get(nm,'')
            })
    print(f"✅ Parsed {len(rows)} menu items")
    return rows


def save_to_csv(rows, path=OUTPUT_CSV):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    got = sum(1 for r in rows if r.get('Image URL'))
    print(f"💾 Saved {len(rows)} rows ({got} images) to {path}")


def scrape_and_extract(seed_ui: bool = False):
    """
    Fetch & prettify the menu page via Selenium+uc through proxy,
    inside a virtual X display so it runs on a UI-less VPS.
    """
    with Display(visible=0, size=(1920, 1080)):
        opts = uc.ChromeOptions()
        # opts.add_argument(f"--proxy-server={PROXY}")
        # opts.headless = not seed_ui
        # opts.add_argument("--no-sandbox")
        # opts.add_argument("--disable-dev-shm-usage")
        # opts.add_argument("--disable-gpu")
        # opts.add_argument("--window-size=1920,1080")
        opts.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/96.0.4664.110 Safari/537.36"
        )

        mode = "headed" if seed_ui else "headless"
        print(f"🔎 Starting {mode} Chrome via proxy…")
        driver = uc.Chrome(options=opts)
        # stealth fingerprinting
        stealth(driver,
                languages=["en-US","en"], vendor="Google Inc.",
                platform="Win32", webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine", fix_hairline=True)

        raw = None
        try:
            driver.get("https://www.doordash.com")
            for ck in load_cookies(COOKIE_FILE):
                try: driver.add_cookie(ck)
                except: pass
            driver.get(STORE_URL)
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.TAG_NAME, 'body'))
            )
            for _ in range(5):
                driver.execute_script("window.scrollBy(0, document.body.scrollHeight/5)")
                time.sleep(1)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)
            deadline = time.time() + 30
            while time.time() < deadline:
                raw = driver.page_source
                if '"@type":"Restaurant"' in raw and '"hasMenu"' in raw:
                    break
                time.sleep(2)
            else:
                raise RuntimeError("JSON-LD menu not found after 30s")
        except Exception:
            driver.quit()
            if not seed_ui:
                print("⚠️ Headless failed, retrying headed…")
                return scrape_and_extract(seed_ui=True)
            raise
        finally:
            if raw: save_prettified_html(raw)
            driver.quit()

        return extract_menu_items(raw)


# def run(store_url):
#     global STORE_URL; STORE_URL = store_url
#     rows = scrape_and_extract()
#     save_to_csv(rows)
#     shutil.copy(OUTPUT_CSV, f"{rows[0]['Category']}.csv")

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
    run(STORE_URL)