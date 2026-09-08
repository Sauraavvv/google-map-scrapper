"""
Google Maps Keyword Scraper — Streamlit UI
Search any keyword in any location, pick which fields to extract.
"""

import os
import time
import re
import io
import glob
import shutil
import platform
import subprocess
import urllib.parse
import pandas as pd
import streamlit as st
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException

import chrome_deps

ON_CLOUD = platform.system() == "Linux"  # Streamlit Cloud runs on Linux

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Google Maps Scraper", layout="wide")

st.title("Google Maps Keyword Scraper")
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

    max_scrolls  = st.slider("Max Scrolls",       min_value=1,  max_value=20, value=10)
    get_detailed = st.toggle("Detailed Mode (slower, more accurate phone/address)", value=True)

    if want_phone and not get_detailed:
        st.warning(
            "Phone numbers are only on a place's own page, not the result list. "
            "Turn on Detailed Mode or the column will come back empty."
        )

    run_btn = st.button("Start Scraping", type="primary", use_container_width=True)

# ── Core scraper ──────────────────────────────────────────────────────────────

def find_browser_binary():
    """Path to an installed Chrome/Chromium, or None if the host has none."""
    pinned = os.environ.get("CHROME_BIN")
    if pinned and os.path.exists(pinned):
        return pinned
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    for path in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ):
        if os.path.exists(path):
            return path
    return None


def make_chrome_options(headless: bool, binary: str = None) -> Options:
    opts = Options()
    if headless or ON_CLOUD:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    # Port 0 = let Chrome pick a free one; a fixed port collides across reruns.
    opts.add_argument("--remote-debugging-port=0")

    binary = binary or find_browser_binary()
    if binary:
        opts.binary_location = binary
    elif ON_CLOUD:
        # Streamlit Cloud with no packages.txt: nothing is installed. Naming a
        # version makes Selenium Manager download and cache Chrome for Testing.
        # Only on Linux — elsewhere Selenium Manager finds app-bundle installs
        # on its own, and forcing this would trigger a pointless download.
        opts.browser_version = "stable"

    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    return opts


def missing_shared_libs(path: str) -> list:
    """Shared libraries `path` needs but cannot resolve, via ldd (Linux only)."""
    if not path or not os.path.exists(path) or not shutil.which("ldd"):
        return []
    try:
        out = subprocess.run(
            ["ldd", path], capture_output=True, text=True, timeout=30
        )
    except Exception:
        return []
    seen = []
    for line in (out.stdout + out.stderr).splitlines():
        if "not found" in line:
            soname = line.strip().split(" =>")[0].strip()
            if soname and soname not in seen:
                seen.append(soname)
    return seen


def cached_binaries() -> dict:
    """Binaries Selenium Manager downloaded, so we can inspect them on failure."""
    home = os.path.expanduser("~")
    found = {}
    for label, pattern in (
        ("chromedriver", f"{home}/.cache/selenium/chromedriver/*/*/chromedriver"),
        ("chrome", f"{home}/.cache/selenium/chrome/*/*/chrome"),
    ):
        hits = glob.glob(pattern)
        if hits:
            found[label] = sorted(hits)[-1]
    return found


def launch_diagnostics() -> str:
    """Human-readable reason the browser stack will not start, if we can tell."""
    lines = []
    for label, path in cached_binaries().items():
        missing = missing_shared_libs(path)
        if missing:
            lines.append(f"{label} is missing {len(missing)} shared librar"
                         f"{'y' if len(missing) == 1 else 'ies'}:")
            lines.extend(f"    {so}" for so in missing)
    if not lines:
        return ""
    lines.append("")
    lines.append("These come from system packages that are not installed on this host.")
    return "\n".join(lines)


def preferred_chromedriver():
    """An explicitly provided chromedriver, or one on PATH. None means let
    Selenium Manager resolve it."""
    pinned = os.environ.get("CHROMEDRIVER_BIN")
    if pinned and os.path.exists(pinned):
        return pinned
    return shutil.which("chromedriver")


def retry_with_installed_libs(headless: bool, log):
    """Unpack Chrome's shared libraries, then launch against them.

    Only reached after a first launch has already failed, which means Selenium
    Manager has downloaded the browser and driver — they just could not start.
    """
    root = chrome_deps.ensure_libraries(log)
    if not root:
        return None

    cached = cached_binaries()
    chrome = cached.get("chrome")
    driver_path = preferred_chromedriver() or cached.get("chromedriver")
    if not driver_path:
        log("  no chromedriver to retry with")
        return None

    env = dict(os.environ)
    existing = env.get("LD_LIBRARY_PATH")
    paths = chrome_deps.library_path(root) + ([existing] if existing else [])
    env["LD_LIBRARY_PATH"] = ":".join(paths)

    log("Retrying browser launch against the unpacked libraries...")
    opts = make_chrome_options(headless, binary=chrome)
    return webdriver.Chrome(service=Service(driver_path, env=env), options=opts)


def make_driver(headless: bool, log=lambda m: None) -> webdriver.Chrome:
    opts = make_chrome_options(headless)
    system_driver = preferred_chromedriver()
    try:
        if system_driver:
            return webdriver.Chrome(service=Service(system_driver), options=opts)
        return webdriver.Chrome(options=opts)
    except Exception as e:
        detail = launch_diagnostics()
        # Missing shared libraries are recoverable without root; try once.
        if detail and ON_CLOUD:
            try:
                driver = retry_with_installed_libs(headless, log)
                if driver is not None:
                    return driver
            except Exception as retry_error:
                detail = f"{launch_diagnostics() or detail}\n\nRetry failed: {retry_error}"
        if detail:
            raise RuntimeError(f"{type(e).__name__}: {e}\n\n{detail}") from e
        raise


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


def strip_label(text):
    """Drop the leading field name from an aria-label.

    Google localises these ("Address:", "Phone:", "Direccion:", ...), so match
    the shape rather than the English word.
    """
    if not text:
        return None
    return re.sub(r"^[^:]{0,24}:\s*", "", text.strip()) or None


def read_phone(driver):
    """Phone from an open place panel, or None.

    Preferred source is data-item-id, which looks like "phone:tel:+911123415555"
    and carries no translated text, unlike the aria-label.
    """
    try:
        el = driver.find_element(By.CSS_SELECTOR, 'button[data-item-id^="phone"]')
    except Exception:
        return None
    item_id = el.get_attribute("data-item-id") or ""
    if "tel:" in item_id:
        number = item_id.split("tel:", 1)[1].strip()
        if number:
            return number
    return strip_label(el.get_attribute("aria-label"))


def get_detailed_info(driver):
    info = {"address": None, "phone": None}
    # Wait for the place panel itself, rather than sleeping a fixed 1.5s and
    # hoping. Any data-item-id button means the detail pane has rendered.
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "button[data-item-id]"))
        )
    except TimeoutException:
        return info

    try:
        el = driver.find_element(By.CSS_SELECTOR, 'button[data-item-id="address"]')
        info["address"] = strip_label(el.get_attribute("aria-label"))
    except Exception:
        pass

    info["phone"] = read_phone(driver)
    if info["phone"] is None:
        # Rows of the panel can settle a beat apart; give it one more look
        # before concluding the place genuinely has no number listed.
        time.sleep(1.0)
        info["phone"] = read_phone(driver)

    return info


FEED_CARDS = 'div[role="feed"] > div > div[jsaction]'


def card_text(el, selector: str):
    try:
        return el.find_element(By.CSS_SELECTOR, selector).text.strip() or None
    except Exception:
        return None


def collect_cards(driver, log_fn) -> list:
    """Read every result card off the feed, deduplicated by place URL.

    Stays on the results page throughout: navigating away destroys the feed,
    and it does not reliably return.
    """
    cards = []
    seen = set()
    total = len(driver.find_elements(By.CSS_SELECTOR, FEED_CARDS))
    log_fn(f"Extracting data from {total} result cards...")

    for idx in range(total):
        try:
            items = driver.find_elements(By.CSS_SELECTOR, FEED_CARDS)
            if idx >= len(items):
                break
            el = items[idx]

            row = {
                "name": card_text(el, "div.fontHeadlineSmall"),
                "category": None, "rating": card_text(el, "span.MW4etd"),
                "reviews_count": None, "address": None, "phone": None,
                "latitude": None, "longitude": None, "url": None,
            }

            reviews = card_text(el, "span.UY7F9")
            if reviews:
                row["reviews_count"] = reviews.strip("()").replace(",", "")

            try:
                spans = el.find_elements(By.CSS_SELECTOR, "div.W4Efsd > span")
                if spans:
                    row["category"] = spans[0].text.strip() or None
                for sp in spans[1:]:
                    t = sp.text.strip()
                    if t and t != "·":
                        row["address"] = t
                        break
            except Exception:
                pass

            try:
                href = el.find_element(By.CSS_SELECTOR, "a").get_attribute("href")
                if href:
                    row["url"] = href
                    row["latitude"], row["longitude"] = extract_lat_lng(href)
            except Exception:
                pass

            if not row["name"]:
                continue
            # Key on the place URL so two branches of one chain both survive.
            key = row["url"] or row["name"]
            if key in seen:
                continue
            seen.add(key)
            cards.append(row)

        except Exception as e:
            log_fn(f"  Error on item {idx}: {e}")

    return cards


def scrape(query: str, max_scrolls: int, headless: bool, get_detailed: bool,
           log_fn, result_store: list):
    driver = None
    try:
        driver = make_driver(headless, log_fn)
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

        # Phase 1 — read every card off the list without navigating away.
        # Clicking into a place replaces the feed, and it does not reliably come
        # back, so anything that leaves this page has to wait until the list is
        # fully harvested.
        cards = collect_cards(driver, log_fn)
        log_fn(f"Collected {len(cards)} unique places from the list.")

        # Phase 2 — visit each place directly for the fields the cards omit.
        if get_detailed and cards:
            log_fn(f"Opening {len(cards)} places for address and phone...")
            for i, row in enumerate(cards, 1):
                url = row.pop("url", None)
                if not url:
                    continue
                try:
                    driver.get(url)
                    detail = get_detailed_info(driver)
                    if detail["address"]:
                        row["address"] = detail["address"]
                    if detail["phone"]:
                        row["phone"] = detail["phone"]
                except Exception as e:
                    log_fn(f"  [{i}] {row['name']} — detail failed: {e}")
                if i % 5 == 0 or i == len(cards):
                    log_fn(f"  detailed {i}/{len(cards)}")
        else:
            for row in cards:
                row.pop("url", None)

        result_store.extend(cards)
        for i, row in enumerate(result_store, 1):
            log_fn(f"  [{i}] {row['name']}")

        log_fn(f"Done — {len(result_store)} results collected.")

    except Exception as e:
        log_fn(f"Fatal error: {e}")
        return f"{type(e).__name__}: {e}"
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

        log_placeholder = st.empty()
        log_lines = []
        results   = []

        _hidden = ("Browser started", "Navigated to search URL", "Current URL")

        def log_fn(msg: str):
            if any(msg.startswith(h) for h in _hidden):
                return
            log_lines.append(msg)
            log_placeholder.text("\n".join(log_lines[-30:]))

        with st.spinner(f"Scraping {search_query} ..."):
            error = scrape(search_query, max_scrolls, True, get_detailed, log_fn, results)

        log_placeholder.empty()

        if error:
            headline, _, detail = error.partition("\n")
            st.error(f"Scraper stopped — {headline}")
            if detail.strip():
                st.code(detail.strip())
            st.caption(
                "On the first cloud run this is usually Chrome being downloaded or "
                "failing to launch. Retry once; if it persists, check the app logs."
            )
        else:
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
        elif not error:
            st.warning("No results found. Try a different keyword or location.")
