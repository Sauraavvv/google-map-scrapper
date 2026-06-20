"""
Google Maps Keyword Scraper — Streamlit UI
Search any keyword in any location, pick which fields to extract.
"""

import time
import re
import io
import platform
import urllib.parse
import pandas as pd
import streamlit as st
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException

ON_CLOUD = platform.system() == "Linux"  # Streamlit Cloud runs on Linux

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Google Maps Scraper", page_icon="🗺️", layout="wide")

st.title("🗺️ Google Maps Keyword Scraper")
st.caption("Search any keyword in any location and extract the details you need.")
st.divider()

# ── Sidebar — inputs & field selection ───────────────────────────────────────
with st.sidebar:
    st.header("Search Settings")

    keyword = st.text_input(
        "Keyword *",
        placeholder="e.g. property dealers, hospitals, restaurants",
    )

    location = st.text_input(
        "Location *",
        placeholder="e.g. Connaught Place, Delhi",
    )

    state = st.text_input(
        "State (optional)",
        placeholder="e.g. Delhi, Maharashtra",
    )

    st.divider()
    st.subheader("Fields to Extract")

    want_name     = st.checkbox("Name",             value=True)
    want_address  = st.checkbox("Address",           value=True)
    want_phone    = st.checkbox("Phone / Mobile",    value=True)
    want_lat_lng  = st.checkbox("Latitude & Longitude", value=False)
    want_rating   = st.checkbox("Rating",            value=False)
    want_reviews  = st.checkbox("Reviews Count",     value=False)
    want_category = st.checkbox("Category",          value=False)

    st.divider()
    st.subheader("Scraper Settings")

    max_scrolls  = st.slider("Max Scrolls",       min_value=5,  max_value=100, value=20)
    headless     = st.toggle("Headless Browser",  value=True)
    get_detailed = st.toggle("Detailed Mode (slower, more accurate phone/address)", value=True)

    run_btn = st.button("Start Scraping", type="primary", use_container_width=True)

# ── Core scraper ──────────────────────────────────────────────────────────────

def make_chrome_options(headless: bool) -> Options:
    opts = Options()
    if headless or ON_CLOUD:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--remote-debugging-port=9222")
    if ON_CLOUD:
        opts.binary_location = "/usr/bin/chromium"
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    return opts


def make_driver(headless: bool) -> webdriver.Chrome:
    opts = make_chrome_options(headless)
    if ON_CLOUD:
        service = Service("/usr/bin/chromedriver")
        return webdriver.Chrome(service=service, options=opts)

    return webdriver.Chrome(options=opts)


def extract_lat_lng(url: str):
    if not url:
        return None, None
    for pattern in [
        r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)",
        r"@(-?\d+\.\d+),(-?\d+\.\d+)",
        r"/(-?\d+\.\d+),(-?\d+\.\d+)",
    ]:
        m = re.search(pattern, url)
        if m:
            return m.group(1), m.group(2)
    return None, None


def dismiss_consent(driver):
    for xpath in [
        '//button[contains(., "Accept all")]',
        '//button[contains(., "I agree")]',
        '//button[@aria-label="Accept all"]',
    ]:
        try:
            btn = WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            btn.click()
            time.sleep(0.5)
            return
        except Exception:
            continue


def get_detailed_info(driver):
    info = {"address": None, "phone": None}
    time.sleep(1.5)
    try:
        el = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'button[data-item-id="address"]'))
        )
        info["address"] = el.get_attribute("aria-label").replace("Address: ", "").strip()
    except Exception:
        pass
    try:
        el = driver.find_element(By.CSS_SELECTOR, 'button[data-item-id*="phone"]')
        info["phone"] = el.get_attribute("aria-label").replace("Phone: ", "").strip()
    except Exception:
        pass
    return info


def scrape(query: str, max_scrolls: int, headless: bool, get_detailed: bool,
           log_fn, result_store: list):
    driver = None
    try:
        driver = make_driver(headless)
        log_fn("Browser started.")

        # Navigate directly to search URL (more reliable than typing in box)
        encoded = urllib.parse.quote(query)
        driver.get(f"https://www.google.com/maps/search/{encoded}")
        log_fn(f"Navigated to search URL for: {query}")

        # Dismiss consent if shown
        dismiss_consent(driver)

        # Wait for either feed list or a place page
        try:
            WebDriverWait(driver, 10).until(
                lambda d: 'maps/search' in d.current_url or '/maps/place/' in d.current_url
            )
        except TimeoutException:
            pass

        log_fn(f"Current URL: {driver.current_url[:80]}")

        # Scroll to load results
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'div[role="feed"]'))
            )
            feed = driver.find_element(By.CSS_SELECTOR, 'div[role="feed"]')
            prev_count = 0
            no_change = 0
            for s in range(max_scrolls):
                driver.execute_script(
                    "arguments[0].scrollTop += 1000;", feed
                )
                time.sleep(2)
                items = driver.find_elements(
                    By.CSS_SELECTOR, 'div[role="feed"] > div > div[jsaction]'
                )
                cur = len(items)
                log_fn(f"Scroll {s+1}/{max_scrolls} — {cur} results visible")
                if cur == prev_count:
                    no_change += 1
                    if no_change >= 3:
                        log_fn("No new results — end of list reached.")
                        break
                else:
                    no_change = 0
                prev_count = cur
        except (NoSuchElementException, TimeoutException):
            log_fn("No results list found — Google may have shown a single place or CAPTCHA.")

        # Extract
        items = driver.find_elements(
            By.CSS_SELECTOR, 'div[role="feed"] > div > div[jsaction]'
        )
        log_fn(f"Extracting data from {len(items)} result cards...")
        seen = set()

        for idx, _ in enumerate(items):
            try:
                items = driver.find_elements(
                    By.CSS_SELECTOR, 'div[role="feed"] > div > div[jsaction]'
                )
                if idx >= len(items):
                    break
                el = items[idx]

                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                time.sleep(0.2)

                row = {
                    "name": None, "category": None, "rating": None,
                    "reviews_count": None, "address": None, "phone": None,
                    "latitude": None, "longitude": None,
                }

                try:
                    row["name"] = el.find_element(By.CSS_SELECTOR, "div.fontHeadlineSmall").text
                except Exception:
                    pass

                try:
                    row["rating"] = el.find_element(By.CSS_SELECTOR, "span.MW4etd").text
                except Exception:
                    pass

                try:
                    rev = el.find_element(By.CSS_SELECTOR, "span.UY7F9").text
                    row["reviews_count"] = rev.strip("()").replace(",", "")
                except Exception:
                    pass

                try:
                    spans = el.find_elements(By.CSS_SELECTOR, "div.W4Efsd > span")
                    if spans:
                        row["category"] = spans[0].text or None
                    for sp in spans[1:]:
                        t = sp.text.strip()
                        if t and t != "·":
                            row["address"] = t
                            break
                except Exception:
                    pass

                try:
                    link = el.find_element(By.CSS_SELECTOR, "a")
                    href = link.get_attribute("href")
                    row["latitude"], row["longitude"] = extract_lat_lng(href or "")
                except Exception:
                    pass

                if not row["name"] or row["name"] in seen:
                    continue
                seen.add(row["name"])

                # Click for detailed address / phone
                if get_detailed:
                    try:
                        items = driver.find_elements(
                            By.CSS_SELECTOR, 'div[role="feed"] > div > div[jsaction]'
                        )
                        if idx < len(items):
                            lnk = items[idx].find_element(By.CSS_SELECTOR, "a")
                            driver.execute_script("arguments[0].click();", lnk)
                            detail = get_detailed_info(driver)
                            if detail["address"]:
                                row["address"] = detail["address"]
                            if detail["phone"]:
                                row["phone"] = detail["phone"]
                            # Go back
                            try:
                                back = driver.find_element(
                                    By.CSS_SELECTOR, 'button[aria-label*="Back"]'
                                )
                                back.click()
                                time.sleep(0.5)
                            except Exception:
                                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                                time.sleep(0.5)
                    except Exception:
                        pass

                result_store.append(row)
                log_fn(f"  [{len(result_store)}] {row['name']}")

            except Exception as e:
                log_fn(f"  Error on item {idx}: {e}")

        log_fn(f"Done — {len(result_store)} results collected.")

    except Exception as e:
        log_fn(f"Fatal error: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ── Field filter ──────────────────────────────────────────────────────────────

def filter_fields(results: list, want: dict) -> pd.DataFrame:
    col_map = {
        "name":          want.get("name"),
        "address":       want.get("address"),
        "phone":         want.get("phone"),
        "latitude":      want.get("lat_lng"),
        "longitude":     want.get("lat_lng"),
        "rating":        want.get("rating"),
        "reviews_count": want.get("reviews"),
        "category":      want.get("category"),
    }
    keep = [col for col, include in col_map.items() if include]
    df = pd.DataFrame(results)
    existing = [c for c in keep if c in df.columns]
    return df[existing] if existing else df


# ── Run ───────────────────────────────────────────────────────────────────────

if run_btn:
    if not keyword.strip():
        st.error("Please enter a keyword.")
    elif not location.strip():
        st.error("Please enter a location.")
    else:
        parts = [keyword.strip(), location.strip()]
        if state.strip():
            parts.append(state.strip())
        search_query = " in " .join([keyword.strip(), ", ".join(
            [p for p in [location.strip(), state.strip()] if p]
        )])

        want_map = {
            "name":     want_name,
            "address":  want_address,
            "phone":    want_phone,
            "lat_lng":  want_lat_lng,
            "rating":   want_rating,
            "reviews":  want_reviews,
            "category": want_category,
        }

        results = []

        def log_fn(msg: str):
            pass

        with st.spinner(f"Scraping **{search_query}** ..."):
            scrape(search_query, max_scrolls, headless, get_detailed, log_fn, results)

        st.success(f"Scraping complete — {len(results)} results found.")

        if results:
            df = filter_fields(results, want_map)
            st.dataframe(df, use_container_width=True)

            csv_buf = io.StringIO()
            df.to_csv(csv_buf, index=False)

            st.download_button(
                label="Download CSV",
                data=csv_buf.getvalue(),
                file_name=f"{re.sub(r'[^\\w]+', '_', search_query)}.csv",
                mime="text/csv",
                type="primary",
            )
        else:
            st.warning("No results found. Try a different keyword or location.")
