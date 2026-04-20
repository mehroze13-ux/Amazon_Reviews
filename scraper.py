import os
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException

from database import init_db, upsert_product, insert_review

load_dotenv()

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCRAPE_DAYS_BACK = int(os.environ.get("SCRAPE_DAYS_BACK", 30))
CHROME_PROFILE   = os.getenv("CHROME_PROFILE", str(Path(__file__).parent / "chrome-profile"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


# ── Driver ─────────────────────────────────────────────────────────────────────

def build_driver(headless=False):
    Path(CHROME_PROFILE).mkdir(parents=True, exist_ok=True)

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument(f"--user-agent={random.choice(USER_AGENTS)}")
    opts.add_argument("--start-maximized")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--lang=en-IN")
    # Persistent profile — browser stays logged in between runs
    opts.add_argument(f"--user-data-dir={CHROME_PROFILE}")
    log.info(f"Using Chrome profile: {CHROME_PROFILE}")

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    except Exception:
        driver = webdriver.Chrome(options=opts)

    # CDP-level bot bypass — deeper than execute_script, runs on every new page
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        Object.defineProperty(navigator, 'webdriver',  { get: () => undefined });
        Object.defineProperty(navigator, 'plugins',    { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages',  { get: () => ['en-IN', 'en'] });
        window.chrome = { runtime: {} };
    """})
    return driver


# ── Login detection (no input() required — polls automatically) ────────────────

def _needs_login(driver):
    url = driver.current_url.lower()
    if "ap/signin" in url or "sign-in" in url:
        return True
    try:
        return bool(driver.execute_script("""
            return !!(
                document.querySelector('input#ap_email') ||
                document.querySelector('input#ap_password') ||
                document.querySelector('form[name="signIn"]') ||
                document.querySelector('#auth-signin-button')
            );
        """))
    except Exception:
        return False


def _reviews_visible(driver):
    try:
        return bool(driver.execute_script(
            "return document.querySelectorAll('[data-hook=\"review\"]').length > 0;"
        ))
    except Exception:
        return False


def wait_until_reviews_ready(driver, asin, timeout=300):
    url = (f"https://www.amazon.in/product-reviews/{asin}"
           f"/ref=cm_cr_arp_d_viewopt_srt?sortBy=recent")
    log.info(f"Opening reviews page for {asin}...")
    driver.get(url)

    login_warned = False
    waited = 0
    while waited < timeout:
        time.sleep(2)
        waited += 2

        if _reviews_visible(driver):
            if login_warned:
                log.info("Logged in — reviews visible, continuing...")
            return

        if _needs_login(driver):
            if not login_warned:
                print("\n" + "="*60)
                print("  Amazon is asking you to log in.")
                print("  Please log in in the Chrome window.")
                print("  This script will continue automatically once done.")
                print("="*60)
                login_warned = True
            elif waited % 10 == 0:
                print(f"  Still waiting for login... ({waited}s elapsed)")
            continue

        if waited % 20 == 0 and waited > 0:
            log.info(f"Waiting for page... ({waited}s) URL: {driver.current_url[:80]}")

    raise RuntimeError(f"Timed out waiting for reviews. Last URL: {driver.current_url}")


# ── Review extraction via JavaScript ──────────────────────────────────────────

def _extract_reviews(driver):
    return driver.execute_script("""
        return Array.from(document.querySelectorAll('[data-hook="review"]')).map(r => {
            const ratingEl = r.querySelector('[data-hook="review-star-rating"], [data-hook="cmps-review-star-rating"]');
            const titleEl  = r.querySelector('[data-hook="review-title"]');
            const bodyEl   = r.querySelector('[data-hook="review-body"] span');
            const dateEl   = r.querySelector('[data-hook="review-date"]');
            const nameEl   = r.querySelector('.a-profile-name');
            const verifiedEl = r.querySelector('[data-hook="avp-badge"]');
            return {
                review_id:        r.getAttribute("id"),
                rating:           ratingEl  ? ratingEl.innerText.trim()  : "",
                title:            titleEl   ? titleEl.innerText.trim()   : "",
                body:             bodyEl    ? bodyEl.innerText.trim()    : "",
                review_date:      dateEl    ? dateEl.innerText.trim()    : "",
                reviewer_name:    nameEl    ? nameEl.innerText.trim()    : "Anonymous",
                verified_purchase: !!verifiedEl
            };
        });
    """)


def _first_review_id(driver):
    try:
        return driver.execute_script("""
            const el = document.querySelector('[data-hook="review"]');
            return el ? el.getAttribute('id') : null;
        """)
    except Exception:
        return None


# ── Pagination: try "Show more" button first, fallback to URL ──────────────────

def _find_show_more_button(driver):
    preferred = ["[data-hook='show-more-button']", "a[data-hook='show-more-button']"]
    for sel in preferred:
        try:
            matches = driver.find_elements(By.CSS_SELECTOR, sel)
            btn = next((el for el in matches if el.is_displayed()), None)
            if btn:
                return btn
        except StaleElementReferenceException:
            continue

    trigger_phrases = ("show 10 more reviews", "show more reviews")
    try:
        candidates = driver.find_elements(By.XPATH,
            "//*[self::button or self::a or @role='button' or contains(@class,'a-button')]")
        return next((
            el for el in candidates
            if el.is_displayed() and any(
                p in (el.text or "").lower() for p in trigger_phrases
            )
        ), None)
    except StaleElementReferenceException:
        return None


def _click_show_more(driver, previous_count, timeout=15):
    button = _find_show_more_button(driver)
    if not button:
        return False

    for attempt in range(3):
        try:
            button = _find_show_more_button(driver)
            if not button:
                return False
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
            time.sleep(random.uniform(0.8, 1.4))
            try:
                button.click()
            except Exception:
                driver.execute_script("arguments[0].click();", button)

            WebDriverWait(driver, timeout).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "[data-hook='review']")) > previous_count
            )
            time.sleep(random.uniform(1.0, 2.0))
            return True
        except StaleElementReferenceException:
            if attempt < 2:
                time.sleep(random.uniform(0.3, 0.7))
                continue
            return False
        except TimeoutException:
            return False
    return False


def _goto_next_page_url(driver, asin, page_number, prev_first_id, timeout=15):
    url = (f"https://www.amazon.in/product-reviews/{asin}"
           f"/ref=cm_cr_arp_d_viewopt_srt?sortBy=recent&pageNumber={page_number}")
    try:
        driver.get(url)
        WebDriverWait(driver, timeout).until(
            lambda d: _first_review_id(d) != prev_first_id
        )
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "[data-hook='review']"))
        )
        time.sleep(random.uniform(1.5, 2.5))
        return True
    except TimeoutException:
        log.warning(f"Page {page_number} did not load. Stopping.")
        return False


# ── Date parsing ───────────────────────────────────────────────────────────────

def parse_amazon_date(date_str):
    try:
        cleaned = date_str.replace("Reviewed in India on ", "").strip()
        return datetime.strptime(cleaned, "%d %B %Y").date()
    except Exception:
        return None


def parse_rating(text):
    try:
        return int(float(text.strip().split()[0]))
    except Exception:
        return None


# ── Core scrape function ───────────────────────────────────────────────────────

def scrape_reviews_for_asin(driver, asin, product_name, category="", max_pages=20, days=None):
    days = days or SCRAPE_DAYS_BACK
    cutoff = date.today() - timedelta(days=days)
    reviews = []
    seen_ids = set()

    for batch in range(max_pages):
        # Scroll down 4 times to trigger Amazon's lazy loading
        for _ in range(4):
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(random.uniform(1.5, 2.5))

        visible = _extract_reviews(driver)
        if not visible:
            log.info(f"  No reviews in batch {batch + 1}, stopping.")
            break

        new_reviews = [r for r in visible if r.get("review_id") not in seen_ids]
        if not new_reviews:
            log.info("  No new reviews after scroll, stopping.")
            break

        hit_cutoff = False
        for r in new_reviews:
            if not r.get("body"):
                continue

            parsed_date = parse_amazon_date(r.get("review_date", ""))
            if parsed_date and parsed_date < cutoff:
                log.info(f"  Reached review from {parsed_date} (older than {days} days), stopping.")
                hit_cutoff = True
                break

            if r.get("review_id"):
                seen_ids.add(r["review_id"])

            reviews.append({
                "asin":             asin,
                "reviewer_name":    r.get("reviewer_name", "Anonymous"),
                "rating":           parse_rating(r.get("rating", "")),
                "title":            r.get("title", "").splitlines()[-1].strip() if r.get("title") else "",
                "body":             r["body"],
                "review_date":      parsed_date.isoformat() if parsed_date else r.get("review_date", ""),
                "verified_purchase": r.get("verified_purchase", False),
            })

        if hit_cutoff:
            break

        if batch == max_pages - 1:
            log.info(f"  Reached max_pages={max_pages}, stopping.")
            break

        prev_first_id = visible[0].get("review_id") if visible else None

        # Try inline "Show 10 more" button first
        if _click_show_more(driver, len(visible)):
            log.info(f"  Loaded more reviews via Show More button (batch {batch + 2})")
            continue

        # Fallback: navigate to next page URL
        log.info(f"  No Show More button — navigating to page {batch + 2} via URL")
        if not _goto_next_page_url(driver, asin, batch + 2, prev_first_id):
            break

    log.info(f"  Total: {len(reviews)} reviews for {product_name} (last {days} days)")
    return reviews


# ── Main runner ────────────────────────────────────────────────────────────────

def run_scraper(asins_file="asins.csv", max_pages=20, days=None, headless=False):
    import csv
    init_db()

    products = []
    with open(asins_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            products.append(row)

    if not products:
        log.error("No products found in asins.csv")
        return

    driver = build_driver(headless=headless)
    total_saved = 0

    try:
        for i, product in enumerate(products, 1):
            asin     = product.get("asin", "").strip()
            name     = product.get("name", asin).strip()
            category = product.get("category", "General").strip()
            if not asin:
                continue

            log.info(f"\n── [{i}/{len(products)}] {name} ({asin}) ──")
            upsert_product(asin, name, category)

            # Opens page, waits for login automatically if needed
            wait_until_reviews_ready(driver, asin)

            reviews = scrape_reviews_for_asin(
                driver, asin, name, category=category,
                max_pages=max_pages, days=days
            )

            for r in reviews:
                insert_review(
                    asin=r["asin"],
                    reviewer_name=r["reviewer_name"],
                    rating=r["rating"],
                    title=r["title"],
                    body=r["body"],
                    review_date=r["review_date"],
                    verified=r["verified_purchase"],
                )
                total_saved += 1

            log.info(f"  Saved {len(reviews)} reviews for {name}")

            if i < len(products):
                pause = random.uniform(6, 10)
                log.info(f"  Pausing {pause:.0f}s before next product...")
                time.sleep(pause)

    finally:
        driver.quit()

    log.info(f"\nDone. Total reviews saved: {total_saved}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--asins", default="asins.csv")
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--days",  type=int, default=30)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    run_scraper(asins_file=args.asins, max_pages=args.pages, days=args.days, headless=args.headless)
