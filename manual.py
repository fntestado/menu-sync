#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import re
import csv
from bs4 import BeautifulSoup

# ─── CONFIG ────────────────────────────────────────────────────────────────
PAGE_HTML   = "page_new.html"                   # your downloaded DoorDash HTML
OUTPUT_CSV  = "doordash_menu_with_images.csv"
# ─────────────────────────────────────────────────────────────────────────────

def extract_items(text, typename):
    """
    Find every JSON object whose __typename is exactly the given typename,
    by brace-counting from the nearest “{” up to its matching “}”.
    Returns a list of dicts.
    """
    items = []
    pattern = rf'"__typename"\s*:\s*"{typename}"'
    for m in re.finditer(pattern, text):
        # back up to the opening '{'
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
            obj = json.loads(raw)
            items.append(obj)
        except json.JSONDecodeError:
            continue
    return items

def clean_url(url):
    """Strip any query string so we get the raw image URL."""
    return url.split('?', 1)[0] if url else ''

def main():
    # 1) load HTML
    with open(PAGE_HTML, encoding='utf-8') as f:
        html = f.read()
    soup = BeautifulSoup(html, 'html.parser')
    text = html  # for our regex/JSON extractor

    # 2) build name→imgUrl lookup
    lookup = {}

    # --- A/B) Grab every StorePageCarouselItem and MenuPageItem ---
    spc = extract_items(text, 'StorePageCarouselItem')
    mpi = extract_items(text, 'MenuPageItem')
    print(f"↳ Found {len(spc)} StorePageCarouselItem, {len(mpi)} MenuPageItem")

    for itm in spc + mpi:
        name = itm.get('name', '').strip()
        # try both possible keys
        url  = itm.get('imgUrl') or itm.get('imageUrl') or ''
        url  = url.strip()
        if name and url:
            lookup.setdefault(name, clean_url(url))

    # --- C) Next.js __NEXT_DATA__ / Apollo cache fallback ---
    nd = soup.find('script', id='__NEXT_DATA__')
    if nd and nd.string:
        try:
            data = json.loads(nd.string)
            apollo = data.get('props', {}) \
                         .get('apolloState', {}) or {}
        except Exception:
            apollo = {}
        count_apollo = 0
        for obj in apollo.values():
            if not isinstance(obj, dict):
                continue
            tn = obj.get('__typename')
            if tn not in ('StorePageCarouselItem', 'MenuPageItem'):
                continue
            name = obj.get('name','').strip()
            url  = obj.get('imgUrl') or obj.get('imageUrl') or ''
            if name and url:
                if name not in lookup:
                    lookup[name] = clean_url(url)
                    count_apollo += 1
        print(f"↳ Added {count_apollo} images from Apollo cache")

    # --- D) quick regex fallback ---
    for m in re.finditer(
            r'{\s*"name"\s*:\s*"([^"]+)"[^}]+?"imageUrl"\s*:\s*"([^"]+)"', text):
        name, url = m.group(1).strip(), m.group(2).strip()
        lookup.setdefault(name, clean_url(url))

    # --- E1) <img alt="Name" …> scan ---
    for img in soup.find_all('img', alt=True):
        name = img['alt'].strip()
        if not name or name in lookup:
            continue
        for attr in ('src','data-src','data-lazy-src'):
            val = img.get(attr)
            if val:
                lookup[name] = clean_url(val)
                break
        else:
            ss = img.get('srcset','').split(',')
            if ss:
                first = ss[0].strip().split(' ')[0]
                if first:
                    lookup[name] = clean_url(first)

    # --- E2) inline background-image scan ---
    style_url_re = re.compile(r'url\(["\']?(https?://[^)"\']+)["\']?\)')
    for el in soup.find_all(style=True):
        m = style_url_re.search(el['style'])
        if not m:
            continue
        name = (el.get('aria-label') or el.get('title') or el.get('alt') or '').strip()
        if name and name not in lookup:
            lookup[name] = clean_url(m.group(1))

    print(f"↳ Total distinct images in lookup: {len(lookup)}")

    # 3) pull the JSON-LD Restaurant→hasMenu
    menu_data = None
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            jd = json.loads(script.string or script.get_text() or '')
        except Exception:
            continue
        if jd.get('@type') == 'Restaurant' and 'hasMenu' in jd:
            menu_data = jd['hasMenu']
            break

    if not menu_data:
        print("❌ Could not find Restaurant menu in JSON-LD.")
        return

    # 4) flatten sections
    sections = menu_data.get('hasMenuSection', [])
    if sections and isinstance(sections[0], list):
        sections = [sec for sub in sections for sec in sub]

    # 5) build CSV rows
    rows = []
    for sec in sections:
        cat = sec.get('name','').strip()
        for mi in sec.get('hasMenuItem', []):
            name      = mi.get('name','').strip()
            desc      = mi.get('description','').strip()
            price_raw = mi.get('offers', {}).get('price','0')
            price     = re.sub(r'[^\d.]', '', str(price_raw))
            img_url   = lookup.get(name, '')
            rows.append({
                "Category":     cat,
                "Name":         name,
                "Description":  desc,
                "Price (USD)":  price,
                "Image URL":    img_url
            })

    # 6) write CSV
    with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["Category","Name","Description","Price (USD)","Image URL"]
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"✅ Wrote {len(rows)} items to {OUTPUT_CSV}")

if __name__ == "__main__":
    main()