#!/usr/bin/env python3
"""
Ultra-fast Playwright Web Scraper API for Salesforce/n8n
Production-ready for Railway + Gunicorn
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import re
import os
import sys
import logging
import hashlib
import time
from datetime import datetime, timezone

app = Flask(__name__)
CORS(app)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Optional API Key auth (set API_KEY env var in Railway)
API_KEY = os.environ.get("API_KEY")


class ScraperConfig:
    """Centralized configuration for scraper tuning."""
    CACHE_TTL_SECONDS = 300  # 5 minutes for price monitoring
    NAVIGATION_TIMEOUT = 15000  # 15s timeout
    DEFAULT_WAIT_MS = 800  # Reduced wait time
    HARVEY_NORMAN_WAIT_MS = 2000  # Reduced for Harvey Norman
    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    )
    BROWSER_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--disable-web-security",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-images",  # Block images at browser level
        "--disable-javascript-harmony-shipping",
    ]


class SimpleCache:
    """Simple in-memory cache with TTL and light eviction."""

    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._store = {}

    def get(self, key: str):
        if key not in self._store:
            return None
        value, timestamp = self._store[key]
        if time.time() - timestamp > self._ttl:
            del self._store[key]
            return None
        return value.copy() if isinstance(value, dict) else value

    def set(self, key: str, value):
        # Simple size limit
        if len(self._store) > 1000:
            # Remove oldest ~100 entries
            sorted_items = sorted(self._store.items(), key=lambda x: x[1][1])
            for old_key, _ in sorted_items[:100]:
                del self._store[old_key]
        self._store[key] = (value.copy() if isinstance(value, dict) else value, time.time())


# Global cache
cache = SimpleCache(ttl_seconds=ScraperConfig.CACHE_TTL_SECONDS)


class PriceScraper:
    """Handles web scraping and price extraction logic."""

    @staticmethod
    def extract_price(url: str) -> dict:
        """Main scraping function - creates new browser for each request."""
        # Check cache first
        cache_key = hashlib.md5(url.encode()).hexdigest()
        cached = cache.get(cache_key)
        if cached:
            logger.info(f"Cache hit for {url}")
            # mark that this came from cache (non-destructive copy above)
            if isinstance(cached, dict):
                cached["cache_hit"] = True
            return cached

        start_time = time.time()

        playwright = None
        browser = None

        try:
            # Initialize Playwright for this request
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(
                headless=True,
                args=ScraperConfig.BROWSER_ARGS
            )

            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=ScraperConfig.USER_AGENT,
                ignore_https_errors=True,
                bypass_csp=True,
                java_script_enabled=True,
                has_touch=False,
                is_mobile=False,
                locale="en-US",
            )

            page = context.new_page()

            try:
                # Block heavy resources at page level for maximum speed
                def block_resources(route):
                    try:
                        rtype = route.request.resource_type
                    except Exception:
                        rtype = None
                    if rtype in {"image", "media", "font", "stylesheet"}:
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", block_resources)

                # Navigate with reduced timeout
                logger.info(f"Loading: {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=ScraperConfig.NAVIGATION_TIMEOUT)

                # Minimal wait based on site
                wait_ms = ScraperConfig.HARVEY_NORMAN_WAIT_MS if "harveynorman" in url.lower() else ScraperConfig.DEFAULT_WAIT_MS
                page.wait_for_timeout(wait_ms)

                # Try structured extraction first
                structured_price = PriceScraper._extract_structured_price(page, url)

                # Get page text for fallback extraction
                page_text = page.evaluate("() => document.body ? document.body.innerText : ''")
                title = page.evaluate("() => document.querySelector('h1')?.innerText || document.title || ''")

                # Extract all prices
                all_prices = re.findall(r"\$[\d,]+\.?\d*", page_text)

                # Determine main price
                main_price = structured_price or PriceScraper._find_main_price(page_text, all_prices, url)

                result = {
                    "success": True,
                    "url": url,
                    "title": title.strip(),
                    "price": main_price,
                    "all_prices": list(dict.fromkeys(all_prices))[:10],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "load_time_ms": int((time.time() - start_time) * 1000),
                }

                # Cache successful result
                cache.set(cache_key, result)
                logger.info(f"Scraped {url} in {result['load_time_ms']}ms")
                return result

            finally:
                try:
                    page.close()
                except Exception:
                    pass
                try:
                    context.close()
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Scraping error for {url}: {str(e)}")
            return {
                "success": False,
                "url": url,
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "load_time_ms": int((time.time() - start_time) * 1000),
            }

        finally:
            # Always clean up browser and playwright
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if playwright:
                try:
                    playwright.stop()
                except Exception:
                    pass

    @staticmethod
    def _extract_structured_price(page, url: str) -> str:
        """Extract price using CSS selectors for speed."""
        url_lower = url.lower()

        # JB Hi-Fi specific selectors
        if "jbhifi" in url_lower:
            selectors = [
                'span[class*="PriceTag_actual"]',
                'span[class*="PriceFont_fontStyle"]',
                ".price__current",
                '[data-testid="price-display"]',
            ]
            for selector in selectors:
                try:
                    element = page.query_selector(selector)
                    if element:
                        text = element.inner_text().strip()
                        # Handle numeric-only prices
                        if re.match(r"^[\d,]+(?:\.\d{2})?$", text):
                            return f"${text}"
                        # Extract price with $ symbol
                        match = re.search(r"\$[\d,]+\.?\d*", text)
                        if match:
                            return match.group()
                except Exception:
                    continue

        # Generic selectors for all sites
        generic_selectors = [
            '[itemprop="price"]',
            '[data-testid*="price"]',
            ".price",
            ".product-price",
            ".current-price",
            'meta[itemprop="price"]',
        ]
        for selector in generic_selectors:
            try:
                if selector.startswith("meta"):
                    element = page.query_selector(selector)
                    if element:
                        content = element.get_attribute("content")
                        if content and re.match(r"^[\d,]+\.?\d*$", content):
                            return f"${content}"
                else:
                    element = page.query_selector(selector)
                    if element:
                        # small timeout to avoid blocking
                        text = element.inner_text(timeout=200)
                        match = re.search(r"\$[\d,]+\.?\d*", text)
                        if match:
                            return match.group()
            except Exception:
                continue

        return ""

    @staticmethod
    def _find_main_price(text: str, all_prices: list, url: str) -> str:
        """Site-specific price extraction patterns."""
        url_lower = url.lower()

        # Harvey Norman patterns
        if "harveynorman" in url_lower and "Available on" in text:
            marker_index = text.find("Available on")
            search_text = text[max(0, marker_index - 500):marker_index]

            # EASAVE pattern
            easave = re.findall(r"\$[\d,]+\.?\d*\s*EASAVE", search_text)
            if easave:
                prices = re.findall(r"\$[\d,]+\.?\d*", easave[-1])
                if prices:
                    return prices[0]

            # FROM pattern
            if "FROM" in search_text:
                from_idx = search_text.rfind("FROM")
                prices = re.findall(r"\$[\d,]+\.?\d*", search_text[from_idx:])
                if prices:
                    return prices[0]

            # Last price before marker
            prices = re.findall(r"\$[\d,]+\.?\d*", search_text)
            if prices:
                return prices[-1]

        # JB Hi-Fi patterns
        if "jbhifi" in url_lower:
            # Look for price near login prompt
            if "Log in to see if you have coupons" in text:
                idx = text.find("Log in to see if you have coupons")
                search = text[max(0, idx - 300):idx]
                prices = re.findall(r"\$[\d,]+\.?\d*", search)
                if prices:
                    return prices[-1]

            # Ticket pattern
            if "Ticket" in text:
                idx = text.find("Ticket")
                numbers = re.findall(r"[\d,]+(?:\.\d{2})?", text[idx:idx + 100])
                if len(numbers) >= 2:
                    return f"${numbers[1]}"
                elif numbers:
                    return f"${numbers[0]}"

        # Officeworks pattern
        if "officeworks" in url_lower and "Ways you can get it" in text:
            idx = text.find("Ways you can get it")
            prices = re.findall(r"\$[\d,]+\.?\d*", text[max(0, idx - 300):idx])
            if prices:
                return prices[-1]

        # Fallback: first price over $100
        for price in all_prices:
            try:
                value = float(re.sub(r"[$,]", "", price))
                if value > 100:
                    return price
            except Exception:
                continue

        return all_prices[0] if all_prices else ""


# API Routes
@app.route("/", methods=["GET"])
def home():
    """API information endpoint"""
    return jsonify({
        "service": "Stateless Price Scraper",
        "version": "9.0",
        "features": [
            "New browser per request",
            "No threading issues - guaranteed",
            "No shared state",
            "Aggressive resource blocking",
            "Railway production-ready",
            "5-minute cache TTL",
        ],
    })


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "cache_size": len(cache._store),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/webhook", methods=["POST", "OPTIONS"])
def webhook():
    """Main webhook endpoint for Salesforce/n8n"""
    if request.method == "OPTIONS":
        return "", 204

    # Optional API key enforcement
    if API_KEY and request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        data = request.get_json() or {}

        pid = data.get("pid")
        urls = data.get("urls", [])

        if not urls:
            return jsonify({
                "success": False,
                "error": "No URLs provided",
                "received_data": data
            }), 400

        results = []
        for url_entry in urls:
            # each entry is like {"comp1_url": "..."} → take the first value
            if not isinstance(url_entry, dict) or not url_entry:
                continue
            url = list(url_entry.values())[0]

            logger.info(f"Scraping URL for pid {pid}: {url}")
            result = PriceScraper.extract_price(url)

            # Preserve compX_url key for clarity in response
            comp_key = list(url_entry.keys())[0]
            result["pid"] = pid
            result["comp_key"] = comp_key

            results.append(result)

        return jsonify({
            "pid": pid,
            "results": results,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))

    # Check if running under gunicorn
    if "gunicorn" in sys.modules:
        logger.info("Running under Gunicorn")
    else:
        # Development mode
        logger.info("Running in development mode")
        # threaded=False because Playwright sync + we launch a browser per request
        app.run(host="0.0.0.0", port=port, debug=False, threaded=False)