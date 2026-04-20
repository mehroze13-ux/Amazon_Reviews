import os
import time
import random
import logging
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from database import init_db, upsert_product, insert_review

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

AMAZON_EMAIL    = os.getenv("AMAZON_EMAIL", "")
AMAZON_PASSWORD = os.getenv("AMAZON_PASSWORD", "")


def build_driver(headless=False):
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()), options=opts
        )
    except Exception:
        driver = webdriver.Chrome(options=opts)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    driver.set_window_size(1280, 900)
    return driver


def amazon_login(driver):
    if not AMAZON_EMAIL or not AMAZON_PASSWORD:
        log.info("No Amazon credentials in .env — skipping login (may hit CAPTCHAs)")
        return False

    log.info("Logging in to Amazon.in...")
    driver.get("https://www.amazon.in/ap/signin?openid.pape.max_auth_age=0&openid.return_to=https%3A%2F%2Fwww.amazon.in%2F&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.assoc_handle=inflex&openid.mode=checkid_setup&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0")
    time.sleep(2)

    try:
        # Enter email
        email_field = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "ap_email"))
        )
        email_field.clear()
        email_field.send_keys(AMAZON_EMAIL)
        driver.find_element(By.ID, "continue").click()
        time.sleep(2)

        # Enter password
        pwd_field = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "ap_password"))
        )
        pwd_field.clear()
        pwd_field.send_keys(AMAZON_PASSWORD)
        driver.find_element(By.ID, "signInSubmit").click()
        time.sleep(3)

        # Check if OTP / CAPTCHA page appeared
        if "ap/cvf" in driver.current_url or "ap/challenge" in driver.current_url:
            log.warning("Amazon is asking for OTP or CAPTCHA. Please complete it in the browser window.")
            input("Press ENTER here once you've completed the verification in the browser...")

        if "amazon.in" in driver.current_url and "signin" not in driver.current_url:
            log.info("Login successful.")
            return True
        else:
            log.warning("Login may have failed. Continuing anyway...")
            return False

    except Exception as e:
        log.warning(f"Login failed: {e}. Continuing without login.")
        return False


def wait_if_captcha(driver):
    if "captcha" in driver.page_source.lower() or "robot" in driver.page_source.lower():
        log.warning("CAPTCHA detected! Please solve it in the browser window.")
        input("Press ENTER here once you've solved the CAPTCHA...")
        time.sleep(2)


def parse_rating(text):
    try:
        return int(float(text.strip().split()[0]))
    except Exception:
        return None


def scrape_product_reviews(driver, asin, max_pages=5):
    reviews = []
    base_url = f"https://www.amazon.in/product-reviews/{asin}/"

    for page in range(1, max_pages + 1):
        url = f"{base_url}?pageNumber={page}&reviewerType=all_reviews"
        log.info(f"Scraping ASIN {asin} — page {page}")

        driver.get(url)
        time.sleep(random.uniform(2, 4))

        wait_if_captcha(driver)

        try:
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "[data-hook='review']"))
            )
        except TimeoutException:
            log.warning(f"No reviews found on page {page} for {asin} — stopping")
            break

        review_els = driver.find_elements(By.CSS_SELECTOR, "[data-hook='review']")
        if not review_els:
            break

        for el in review_els:
            try:
                name = _text(el, ".a-profile-name")
                rating_text = (
                    _text(el, "[data-hook='review-star-rating'] .a-icon-alt") or
                    _text(el, "[data-hook='cmps-review-star-rating'] .a-icon-alt")
                )
                rating = parse_rating(rating_text) if rating_text else None

                title_el = el.find_element(By.CSS_SELECTOR, "[data-hook='review-title']")
                title = title_el.text.strip()
                for line in title.splitlines():
                    line = line.strip()
                    if line and not line[0].isdigit():
                        title = line
                        break

                body = _text(el, "[data-hook='review-body'] span")
                date_text = _text(el, "[data-hook='review-date']")
                review_date = parse_date(date_text)
                verified = bool(el.find_elements(By.CSS_SELECTOR, "[data-hook='avp-badge']"))

                if body:
                    reviews.append({
                        "asin": asin,
                        "reviewer_name": name or "Anonymous",
                        "rating": rating,
                        "title": title,
                        "body": body,
                        "review_date": review_date,
                        "verified_purchase": verified,
                    })
            except Exception as e:
                log.debug(f"Skipping one review: {e}")
                continue

        try:
            next_btn = driver.find_element(By.CSS_SELECTOR, "li.a-last a")
            if not next_btn.is_displayed():
                break
        except NoSuchElementException:
            break

        time.sleep(random.uniform(1, 3))

    log.info(f"Got {len(reviews)} reviews for {asin}")
    return reviews


def _text(parent, selector):
    try:
        return parent.find_element(By.CSS_SELECTOR, selector).text.strip()
    except NoSuchElementException:
        return None


def parse_date(text):
    if not text:
        return None
    try:
        from datetime import datetime
        parts = text.split(" on ")
        if len(parts) == 2:
            return datetime.strptime(parts[1].strip(), "%d %B %Y").strftime("%Y-%m-%d")
    except Exception:
        pass
    return text


def run_scraper(asins_file="asins.csv", max_pages=5, headless=False):
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
        amazon_login(driver)

        for product in products:
            asin = product.get("asin", "").strip()
            name = product.get("name", asin).strip()
            category = product.get("category", "General").strip()

            if not asin:
                continue

            upsert_product(asin, name, category)
            reviews = scrape_product_reviews(driver, asin, max_pages=max_pages)

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

            log.info(f"Saved {len(reviews)} reviews for {name}")
            time.sleep(random.uniform(3, 6))

    finally:
        driver.quit()

    log.info(f"Done. Total reviews saved: {total_saved}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--asins", default="asins.csv")
    parser.add_argument("--pages", type=int, default=5)
    parser.add_argument("--headless", action="store_true", help="Run browser invisibly")
    args = parser.parse_args()
    run_scraper(asins_file=args.asins, max_pages=args.pages, headless=args.headless)
