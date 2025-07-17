#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DoorDash menu + image scraper with aggressive fallbacks:
  – JSON blobs, Apollo cache, regex, alt-tags, inline-styles
  – substring alt-tag scan
  – **slugified filename** scan
"""
import json, re, csv
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import unicodedata
import subprocess
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import csv
import time
import socket

# ─── CONFIG ──────────────────────────────────────────────────
SERVICE_ACCOUNT_FILE = "doordash-scraper-466104-fb93a3a8694e.json"
SPREADSHEET_ID = "1iSRrERayb8TjVLdGdnTtKKFj9YpxWkkY6RIgYTSzpJ0"
PAGE_HTML  = "page_new.html"
OUTPUT_CSV = "doordash_menu_with_images.csv"
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]
# set a global socket timeout (so every HTTP call will time out after 60s)
socket.setdefaulttimeout(60)
# ─────────────────────────────────────────────────────────────

def upload_to_sheets():
    # 1) authorize
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        SERVICE_ACCOUNT_FILE, SCOPES
    )
    gc = gspread.authorize(creds)

    # 2) open spreadsheet, add sheet
    new_title = "Run " + time.strftime("%Y-%m-%d %H:%M")
    ss = gc.open_by_key(SPREADSHEET_ID)
    ws = ss.add_worksheet(title=new_title, rows="1000", cols="10")

    # 3) load CSV
    with open(OUTPUT_CSV, encoding="utf-8") as f:
        data = list(csv.reader(f))

    # 4) write it starting at A1, with retries
    max_tries = 3
    for attempt in range(1, max_tries + 1):
        try:
            ws.update("A1", data)
            break
        except Exception as e:
            print(f"⚠️ Update attempt {attempt}/{max_tries} failed: {e}")
            if attempt == max_tries:
                raise RuntimeError("Failed to push to Sheets after multiple tries") from e
            backoff = 2 ** attempt
            print(f"⏳ Retrying in {backoff}s…")
            time.sleep(backoff)

    print(f"✅ Appended new tab '{new_title}' in spreadsheet {SPREADSHEET_ID}")
    return ss.url, new_title


def extract_items(text, typename):
    items=[]
    pat=rf'"__typename"\s*:\s*"{typename}"'
    for m in re.finditer(pat, text):
        start=text.rfind('{',0,m.start())
        if start<0: continue
        depth=0
        for i,ch in enumerate(text[start:], start):
            if ch=='{': depth+=1
            elif ch=='}':
                depth-=1
                if depth==0:
                    raw=text[start:i+1]
                    try: items.append(json.loads(raw))
                    except: pass
                    break
    return items

def clean_url(u):
    return u.split('?',1)[0] if u else ''

def slugify(s):
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"[^a-z0-9]+", "-", s.lower())
    return s.strip("-")

def build_image_lookup(html, soup):
    """
    Build a name→imgUrl lookup by scanning:
      A) GraphQL blobs in every <script> (StorePageCarouselItem + MenuPageItem)
      B) __NEXT_DATA__ Apollo cache
      C) Broad regex for any blob containing both name+imgUrl/imageUrl
      D) Quick name+imageUrl regex
      E) <img alt="Name"...> tags
      F) inline background-image styles
    """
    lookup = {}

    # A) scan every <script> tag’s full text through extract_items
    script_texts = [html] + [tag.get_text() or "" for tag in soup.find_all("script")]
    for text in script_texts:
        for typename in ("StorePageCarouselItem", "MenuPageItem"):
            for blob in extract_items(text, typename):
                name = blob.get("name","").strip()
                url  = blob.get("imgUrl") or blob.get("imageUrl") or ""
                if name and url:
                    lookup.setdefault(name, clean_url(url))

    # B) Apollo cache fallback
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd:
        try:
            data = json.loads(nd.get_text() or "{}")
            ap = data.get("props",{}).get("apolloState",{}) or {}
            for obj in ap.values():
                if not isinstance(obj, dict):
                    continue
                if obj.get("__typename") not in ("StorePageCarouselItem","MenuPageItem"):
                    continue
                name = obj.get("name","").strip()
                url  = obj.get("imgUrl") or obj.get("imageUrl") or ""
                if name and url and name not in lookup:
                    lookup[name] = clean_url(url)
        except Exception:
            pass

    # C) Broad regex: catch any blob that has __typename, name and imgUrl/imageUrl
    broad = re.compile(
        r'"__typename"\s*:\s*"(StorePageCarouselItem|MenuPageItem)".*?'
        r'"name"\s*:\s*"([^"]+)".*?'
        r'"(?:imgUrl|imageUrl)"\s*:\s*"([^"]+)"',
        re.DOTALL
    )
    for m in broad.finditer(html):
        name, url = m.group(2).strip(), m.group(3).strip()
        if name and url and name not in lookup:
            lookup[name] = clean_url(url)

    # D) Quick, simpler regex fallback
    for m in re.finditer(
        r'{\s*"name"\s*:\s*"([^"]+)"[^}]+?"imageUrl"\s*:\s*"([^"]+)"',
        html, re.DOTALL
    ):
        name, url = m.group(1).strip(), m.group(2).strip()
        lookup.setdefault(name, clean_url(url))

    # E) <img alt="Name"...>
    for img in soup.find_all("img", alt=True):
        name = img["alt"].strip()
        if not name or name in lookup:
            continue
        # try common attrs
        for attr in ("src","data-src","data-lazy-src"):
            val = img.get(attr)
            if val:
                lookup[name] = clean_url(val)
                break
        else:
            # fallback to srcset
            ss = img.get("srcset","").split(",")
            if ss and ss[0].strip():
                lookup[name] = clean_url(ss[0].split()[0])

    # F) inline background-image
    style_re = re.compile(r'url\(["\']?(https?://[^)"\']+)')
    for el in soup.find_all(style=True):
        m = style_re.search(el["style"])
        if not m:
            continue
        name = (el.get("aria-label") or el.get("title") or el.get("alt") or "").strip()
        if name and name not in lookup:
            lookup[name] = clean_url(m.group(1))

    print(f"↳ Built image lookup with {len(lookup)} entries")
    return lookup

def main():
    # prettify on disk
    subprocess.run([
        "prettier",
        "--parser", "html",
        "--write", PAGE_HTML
    ], check=True)
    
    html=open(PAGE_HTML,encoding="utf-8").read()
    soup=BeautifulSoup(html,"html.parser")
    lookup=build_image_lookup(html,soup)

    # JSON-LD menu
    menu=None
    for s in soup.find_all("script",type="application/ld+json"):
        try:
            jd=json.loads(s.string or s.get_text() or "")
            if jd.get("@type")=="Restaurant" and "hasMenu" in jd:
                menu=jd["hasMenu"]; break
        except: pass
    if not menu:
        print("❌ JSON-LD menu missing"); return

    secs=menu.get("hasMenuSection",[])
    if secs and isinstance(secs[0],list):
        secs=[sub for group in secs for sub in group]

    rows=[]
    for sec in secs:
        cat=sec.get("name","").strip()
        for mi in sec.get("hasMenuItem",[]):
            nm=mi.get("name","").strip()
            if not nm: continue
            desc=mi.get("description","").strip()
            price=re.sub(r"[^\d.]","",str(mi.get("offers",{}).get("price","0")))
            rows.append({"Category":cat,"Name":nm,"Description":desc,"Price (USD)":price,"Image URL":lookup.get(nm,"")})

    # 1) substring alt-match (old)
    resolved=0
    for r in rows:
        if not r["Image URL"]:
            nl=r["Name"].lower()
            for img in soup.find_all("img",alt=True):
                alt=img["alt"] or ""
                if nl in alt.lower():
                    src=img.get("src") or img.get("data-src") or ""
                    if src:
                        r["Image URL"]=clean_url(src)
                        resolved+=1
                        break
    if resolved:
        print(f"↳ Resolved {resolved} via alt-substring")

    # 2) **slug-filename** fallback
    more=0
    all_imgs=[img.get("src") or img.get("data-src") or "" for img in soup.find_all("img")]
    slug_map={}
    for url in all_imgs:
        cu=clean_url(url)
        path=urlparse(cu).path or ""
        fname=path.rsplit("/",1)[-1]
        slug_map.setdefault(slugify(fname),cu)
    for r in rows:
        if not r["Image URL"]:
            key=slugify(r["Name"])
            for slug,cu in slug_map.items():
                if key in slug:
                    r["Image URL"]=cu
                    more+=1
                    break
    if more:
        print(f"↳ Resolved {more} via slug-filename scan")

    # write CSV
    with open(OUTPUT_CSV,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"✅ Wrote {len(rows)} rows ({sum(1 for r in rows if r['Image URL'])} with images)")

if __name__=="__main__":
    main()