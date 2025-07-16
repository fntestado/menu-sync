import time
import pandas as pd
import requests
import json
from pathlib import Path
import os

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ─── CONFIG ─────────────────────────────────────
CSV_FILE = "doordash_menu_with_images.csv"
CATEGORY_NAME = "Test"
IMAGE_DIR = "images"
COOKIES_FILE = "orders_cookies.json"
ORDERS_MENU_URL = "https://partners.orders.co/menu/overview"
ORDERS_DOMAIN = "https://partners.orders.co/"
# ────────────────────────────────────────────────

# Prepare image directory
os.makedirs(IMAGE_DIR, exist_ok=True)

# Load menu data
df = pd.read_csv(CSV_FILE)
item = df.iloc[0]
img_url = item["Image URL"]
img_path = Path(IMAGE_DIR) / "item.jpg"

# Download image if not already downloaded
if not img_path.exists():
    r = requests.get(img_url)
    with open(img_path, "wb") as f:
        f.write(r.content)

# Setup Chrome
options = Options()
options.add_argument("--start-maximized")
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
wait = WebDriverWait(driver, 10)

# Go to domain first to set cookie scope
driver.get(ORDERS_DOMAIN)
time.sleep(3)

# Load session cookies
with open(COOKIES_FILE, "r") as f:
    cookies = json.load(f)
    for cookie in cookies:
        if "sameSite" in cookie:
            del cookie["sameSite"]
        driver.add_cookie(cookie)

# Refresh to apply session
driver.get(ORDERS_MENU_URL)
time.sleep(30)

# Optional: Detect if session expired
if "Login" in driver.page_source or "email" in driver.page_source:
    print("❌ Session expired or invalid cookies. Please re-export cookies.")
    driver.save_screenshot("session_expired.png")
    driver.quit()
    exit()

# Try to add category
try:
    driver.find_element(By.XPATH, "//button[contains(text(), 'Add')]").click()
    time.sleep(1)
    driver.find_element(By.XPATH, "//input[@placeholder='Name']").send_keys(CATEGORY_NAME)
    driver.find_element(By.XPATH, "//button[contains(text(), 'Save')]").click()
    time.sleep(2)
except:
    print("⚠️ Category may already exist")

# Add item
try:
    add_item_xpath = f"//p[contains(text(), '{CATEGORY_NAME}')]/../../../../../../following-sibling::div//p[contains(text(), '+ Add Item')]"
    wait.until(EC.element_to_be_clickable((By.XPATH, add_item_xpath))).click()
    time.sleep(2)
except Exception as e:
    driver.save_screenshot("debug_add_item.png")
    print("❌ Couldn't find 'Add Item' button.")
    raise e

# Fill item form
driver.find_element(By.NAME, "name").send_keys(item["Name"])
upload = driver.find_element(By.XPATH, "//input[@type='file']")
upload.send_keys(str(img_path.resolve()))
time.sleep(2)
driver.find_element(By.NAME, "description").send_keys(item["Description"])
driver.find_element(By.NAME, "price").send_keys(str(item["Price (USD)"]))
driver.find_element(By.XPATH, "//button[contains(text(), 'Save')]").click()
time.sleep(3)

print("✅ Item uploaded successfully!")
driver.quit()