"""
geo_scraper_jharkhand.py  –  Enhanced Google Maps scraper for Jharkhand locations

This version is specifically optimized to:
1. Handle all locations from your Jharkhand Excel file (up to 2000 localities)
2. Maximize business discovery across Jharkhand
3. Handle rate limiting and blocking gracefully
4. Resume from where it left off
5. Save progress frequently

Key improvements:
- Adaptive zoom levels based on location type (city/rural)
- Better error recovery and retry logic
- Optimized concurrency settings
- Enhanced blocking detection and handling
- More robust fallback mechanisms

Note: geographic coverage comes entirely from the lat/lng of each row in
LOCATIONS_FILE, so switching that file to the Jharkhand locations workbook
is what "adjusts" coverage to Jharkhand — no other logic changes.

Usage:
    python geo_scraper_jharkhand.py
"""

import asyncio
import json
import logging
import math
import os
import random
import re
import time
from contextlib import asynccontextmanager
from typing import Optional, Dict, List, Set, Tuple
from urllib.parse import quote_plus
from datetime import datetime

import pandas as pd
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
    TimeoutError as PWTimeout,
)

# ------------------------- CONFIGURATION -------------------------

# File paths
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
LOCATIONS_FILE = os.path.join(BASE_DIR, "Jharkhand_Localities_2000.xlsx")
DATA_DIR       = os.path.join(BASE_DIR, "Data_Jharkhand")
PROGRESS_FILE  = os.path.join(BASE_DIR, "jharkhand_progress.json")
LOG_FILE       = os.path.join(BASE_DIR, "scraper_jharkhand.log")

# Concurrency settings
LOCATION_WORKERS = 6       # Number of locations scraped concurrently
CTX_POOL_SIZE    = 12      # Total reusable BrowserContexts
DETAIL_WORKERS   = 8       # Workers per location for detail scraping

# Search settings
SEARCH_TERM = "Mehendi artist"     # Primary search term
# Additional search terms to try if primary yields few results
FALLBACK_SEARCH_TERMS = [
    "Mehndi artist",
    "Henna artist",
    "Mehandi artist",
    "Bridal mehendi artist",
    "Bridal mehndi artist",
    "Mehendi wala",
    "Mehndi wala",
    "Henna tattoo artist",
    "Mehendi designer",
    "Mehndi designer",
    "Bridal henna artist",
    "Mehendi parlour",
    "Mehndi parlour",
]

# Zoom settings
CITY_ZOOM     = 13        # Zoom for city locations (tighter)
TOWN_ZOOM     = 14        # Zoom for town locations
RURAL_ZOOM    = 15        # Zoom for rural locations
DEFAULT_ZOOM  = 14        # Default fallback
FALLBACK_ZOOMS = [13, 12, 11, 10]  # Zoom OUT gradually

# Distance thresholds (meters)
MAX_RESULT_DISTANCE_CITY = 30_000   # 30 km for cities
MAX_RESULT_DISTANCE_TOWN = 20_000   # 20 km for towns
MAX_RESULT_DISTANCE_RURAL = 15_000  # 15 km for rural

# Scrolling and timing
SCROLL_ITERS = 80          # Maximum scroll iterations
SCROLL_SLEEP = (0.3, 0.6)  # Sleep between scrolls
JITTER_PRE   = (0.5, 2.0)  # Random delay before requests
JITTER_GOTO  = (0.3, 0.8)  # Random delay after navigation

# Session management
MAX_CTX_USES = 40          # Contexts reused this many times before refresh
SAVE_EVERY   = 3           # Save progress every N businesses
MAX_RETRIES  = 3           # Maximum retries per location
MAX_RESULTS  = 150         # Maximum businesses per location

# Rate limiting
REQUEST_DELAY_MIN = 2      # Minimum delay between requests
REQUEST_DELAY_MAX = 5      # Maximum delay between requests
BATCH_PAUSE = 60           # Pause after N locations
BATCH_SIZE = 10            # Locations per batch before pause

# Blocking detection
BLOCK_THRESHOLD = 2        # Number of empty results before assuming block
BLOCK_SLEEP_MIN = 180      # Minimum sleep on block (3 minutes)
BLOCK_SLEEP_MAX = 600      # Maximum sleep on block (10 minutes)

# User agents
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]

_BLOCKED_TYPES = {"image", "media", "font", "stylesheet"}

_STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    window.chrome = { runtime: {} };
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ? Promise.resolve({ state: 'denied' }) : originalQuery(parameters)
    );
"""

# ------------------------- LOGGING SETUP -------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, mode='a'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ------------------------- PROGRESS HELPERS -------------------------

def load_progress() -> dict:
    """Load progress from file"""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as f:
                data = json.load(f)
            if isinstance(data, list):
                return {"completed": set(data), "started": set(), "failed": set()}
            return {
                "completed": set(data.get("completed", [])),
                "started": set(data.get("started", [])),
                "failed": set(data.get("failed", [])),
                "last_run": data.get("last_run", None)
            }
        except Exception as e:
            logger.warning(f"Could not load progress file: {e}")
    return {"completed": set(), "started": set(), "failed": set()}

def save_progress(completed: set, started: set, failed: set = None):
    """Save progress to file"""
    if failed is None:
        failed = set()
    with open(PROGRESS_FILE, "w") as f:
        json.dump(
            {
                "completed": sorted(completed),
                "started": sorted(started),
                "failed": sorted(failed),
                "last_run": datetime.now().isoformat()
            },
            f, indent=2,
        )

def safe_filename(name: str) -> str:
    """Create safe filename from location name"""
    return re.sub(r"[^\w\s-]", "", name).strip().replace(" ", "_")

def loc_key(loc: dict) -> str:
    """Generate unique key for a location"""
    suffix = str(loc.get('_id', ''))[-6:]
    base = safe_filename(f"{loc.get('area', '')}_{loc.get('postalCode', '')}")
    return f"{base}_{suffix}" if suffix else base

def _is_empty_output(path: str) -> bool:
    """Check if output file is empty"""
    try:
        with open(path) as f:
            return len(json.load(f)) == 0
    except Exception:
        return True

def get_location_type(loc: dict) -> str:
    """Determine if location is city, town, or rural based on data"""
    area = loc.get('area', '').lower()
    city = loc.get('city', '').lower()
    
    # Check for city indicators
    city_keywords = ['city', 'municipal', 'corporation', 'municipality', 'nagar nigam']
    town_keywords = ['town', 'nagar panchayat', 'municipal council']
    
    for kw in city_keywords:
        if kw in area or kw in city:
            return 'city'
    
    for kw in town_keywords:
        if kw in area or kw in city:
            return 'town'
    
    # Default to rural
    return 'rural'

def get_location_settings(loc: dict) -> dict:
    """Get optimal settings for a location based on its type"""
    loc_type = get_location_type(loc)
    search_terms = [SEARCH_TERM] + FALLBACK_SEARCH_TERMS
    
    if loc_type == 'city':
        return {
            'zoom': CITY_ZOOM,
            'max_distance': MAX_RESULT_DISTANCE_CITY,
            'search_terms': search_terms
        }
    elif loc_type == 'town':
        return {
            'zoom': TOWN_ZOOM,
            'max_distance': MAX_RESULT_DISTANCE_TOWN,
            'search_terms': search_terms
        }
    else:  # rural
        return {
            'zoom': RURAL_ZOOM,
            'max_distance': MAX_RESULT_DISTANCE_RURAL,
            'search_terms': search_terms
        }

# ------------------------- CONTEXT POOL -------------------------

class ContextPool:
    """Manages a pool of browser contexts for concurrent scraping"""
    
    def __init__(self, browser: Browser, size: int):
        self._browser = browser
        self._size = size
        self._queue: asyncio.Queue = asyncio.Queue()
        self._active_count = 0

    async def initialize(self):
        """Initialize all contexts"""
        for i in range(self._size):
            entry = await self._make_entry()
            await self._queue.put(entry)
        logger.info(f"[ContextPool] Initialized {self._size} contexts")

    async def _make_entry(self) -> tuple:
        """Create a new browser context with page"""
        ctx = await _build_context(self._browser)
        page = await ctx.new_page()
        return (ctx, page, 0)

    @asynccontextmanager
    async def acquire(self):
        """Acquire a context from the pool"""
        ctx, page, uses = await self._queue.get()
        self._active_count += 1
        try:
            yield ctx, page
        finally:
            self._active_count -= 1
            uses += 1
            if uses >= MAX_CTX_USES:
                try:
                    await ctx.close()
                except Exception:
                    pass
                entry = await self._make_entry()
            else:
                try:
                    await page.goto("about:blank", timeout=5000)
                except Exception:
                    pass
                entry = (ctx, page, uses)
            await self._queue.put(entry)

    async def close(self):
        """Close all contexts"""
        while not self._queue.empty():
            ctx, page, _ = await self._queue.get()
            try:
                await ctx.close()
            except Exception:
                pass
        logger.info("[ContextPool] Closed all contexts")

async def _build_context(browser: Browser) -> BrowserContext:
    """Build a new browser context with anti-detection settings"""
    ctx = await browser.new_context(
        user_agent=random.choice(_USER_AGENTS),
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        timezone_id="Asia/Kolkata",
        java_script_enabled=True,
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-CH-UA-Mobile": "?0",
        },
    )
    await ctx.add_init_script(_STEALTH_SCRIPT)

    async def _block(route):
        if route.request.resource_type in _BLOCKED_TYPES:
            await route.abort()
        else:
            await route.continue_()

    await ctx.route("**/*", _block)
    return ctx

# ------------------------- BREAK STRATEGY -------------------------

class AsyncBreakStrategy:
    """Manages breaks between requests to avoid detection"""
    
    def __init__(self):
        self._completed = 0
        self._empty_results = 0
        self._long_break_after = random.uniform(5400, 7200)
        self._last_long_break = time.monotonic()
        self._last_location_time = time.monotonic()

    async def _sleep(self, seconds: float, label: str):
        logger.info(f"⏸  {label}: {seconds/60:.1f} min")
        await asyncio.sleep(seconds)

    async def on_location_complete(self, success: bool = True):
        """Called after each location is processed"""
        self._completed += 1
        
        if not success:
            self._empty_results += 1
        else:
            self._empty_results = max(0, self._empty_results - 1)
        
        # Check for potential blocking
        if self._empty_results >= BLOCK_THRESHOLD:
            await self._sleep(
                random.uniform(BLOCK_SLEEP_MIN, BLOCK_SLEEP_MAX),
                f"⚠️ Potential block detected after {self._empty_results} empty results"
            )
            self._empty_results = 0
            self._last_long_break = time.monotonic()
            return

        elapsed = time.monotonic() - self._last_long_break

        # Long break
        if elapsed >= self._long_break_after:
            await self._sleep(
                random.uniform(1200, 2700),
                f"Long break after {elapsed/3600:.2f} h"
            )
            self._last_long_break = time.monotonic()
            self._long_break_after = random.uniform(5400, 7200)
            return

        # Medium break every 5 locations
        if self._completed % 5 == 0:
            await self._sleep(
                random.uniform(60, 180),
                f"Medium break after {self._completed} locations"
            )
            return

        # Short delay between locations
        delay = random.uniform(10, 25)
        logger.info(f"⏳ Next location in {delay:.1f}s")
        await asyncio.sleep(delay)

    async def on_blocked(self):
        """Emergency pause when blocking is detected"""
        await self._sleep(
            random.uniform(300, 900),
            "🚨 Blocking detected — emergency pause"
        )
        self._last_long_break = time.monotonic()
        self._long_break_after = random.uniform(5400, 7200)

# ------------------------- GOOGLE MAPS SCRAPER -------------------------

class GoogleMapsGeoScraper:
    """
    Enhanced Google Maps scraper with coordinate-anchored searches
    """
    
    def __init__(
        self,
        search_term: str,
        lat: float,
        lng: float,
        output_json: str,
        zoom: int = DEFAULT_ZOOM,
        max_distance: int = MAX_RESULT_DISTANCE_CITY,
        label: str = "",
    ):
        self.search_term = search_term
        self.lat = lat
        self.lng = lng
        self.zoom = zoom
        self.max_distance = max_distance
        self.label = label or search_term
        self.output_json = output_json
        
        self.scraped_data: List[dict] = []
        self.processed_urls: Set[str] = set()
        self._lock = asyncio.Lock()
        self._dirty = False
        self._collect_succeeded = False
        self._load_existing()

    def _load_existing(self):
        """Load existing data from output file"""
        if os.path.exists(self.output_json):
            try:
                with open(self.output_json, "r", encoding="utf-8") as f:
                    self.scraped_data = json.load(f)
                for entry in self.scraped_data:
                    if entry.get("gmaps_url"):
                        self.processed_urls.add(entry["gmaps_url"])
                logger.info(
                    f"✓ Loaded {len(self.scraped_data)} existing records "
                    f"from {self.output_json}"
                )
            except Exception as e:
                logger.warning(f"Could not load existing data: {e}")

    async def _save(self):
        """Save scraped data to file"""
        async with self._lock:
            if not self._dirty:
                return
            os.makedirs(os.path.dirname(os.path.abspath(self.output_json)), exist_ok=True)
            with open(self.output_json, "w", encoding="utf-8") as f:
                json.dump(self.scraped_data, f, indent=2, ensure_ascii=False)
            self._dirty = False

    @staticmethod
    def _build_url(term: str, lat: float, lng: float, zoom: int) -> str:
        """Build coordinate-anchored Google Maps URL"""
        return f"https://www.google.com/maps/search/{quote_plus(term)}/@{lat},{lng},{zoom}z"

    def _build_search_terms(self) -> List[str]:
        """Build a wider list of mehendi-related search terms for better discovery."""
        terms = [self.search_term]
        for term in FALLBACK_SEARCH_TERMS:
            if term not in terms:
                terms.append(term)
        return terms

    @staticmethod
    def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        """Calculate distance between two coordinates in meters"""
        R = 6_371_000
        lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])
        dlat, dlng = lat2 - lat1, lng2 - lng1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
        return 2 * R * math.asin(math.sqrt(a))

    @staticmethod
    def _coords_from_url(url: str) -> tuple[float, float] | None:
        """Extract coordinates from Google Maps URL"""
        m = re.search(r"!3d(-?[\d.]+)!4d(-?[\d.]+)", url)
        if m:
            return float(m.group(1)), float(m.group(2))
        return None

    async def _handle_cookie_consent(self, page: Page):
        """Handle cookie consent popups"""
        for btn_sel in [
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
            'button:has-text("Reject all")',
            'form:nth-child(2) button',
            'button[aria-label*="Accept"]',
        ]:
            try:
                btn = await page.query_selector(btn_sel)
                if btn and await btn.is_visible():
                    await btn.click(timeout=3000)
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                pass

    async def _extract_place_links(self, page: Page) -> List[str]:
        """Extract place links from the current Google Maps page using a broader selector."""
        try:
            return await page.eval_on_selector_all(
                "a[href*='/maps/place/']",
                "els => [...new Set(els.map(e => e.href).filter(h => h && h.includes('/maps/place/')))]"
            )
        except Exception:
            try:
                return await page.evaluate(
                    """
                    () => {
                        const seen = new Set();
                        const results = [];
                        for (const el of document.querySelectorAll('a[href*="/maps/place/"]')) {
                            const href = el.href;
                            if (href && !seen.has(href)) {
                                seen.add(href);
                                results.push(href);
                            }
                        }
                        return results;
                    }
                    """
                )
            except Exception:
                return []

    async def _collect_urls(
        self, 
        page: Page, 
        lat: float, 
        lng: float, 
        zoom: int, 
        term: str,
        retries: int = 3
    ) -> List[str]:
        """Collect business URLs from search results using a more resilient strategy."""
        for attempt in range(1, retries + 1):
            try:
                url = self._build_url(term, lat, lng, zoom)
                logger.info(f"  [collect] attempt {attempt} (zoom={zoom}, term='{term}'): {url}")
                
                # Navigate with timeout
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(random.uniform(0.5, 1.5))
                
                # Handle cookies
                await self._handle_cookie_consent(page)
                
                # Check if we got a single result page
                if "/maps/place/" in page.url:
                    logger.info("  [collect] single result page")
                    return [page.url]
                
                # Wait for results to load. Some pages render without the feed container,
                # so we fall back to the place-link selector directly.
                try:
                    await page.wait_for_selector("div[role='feed'], a[href*='/maps/place/']", timeout=30000)
                except PWTimeout:
                    logger.warning("  [collect] result container not found, retrying")
                    continue
                
                await asyncio.sleep(random.uniform(0.5, 1.0))
                
                # Scroll through results
                feed = await page.query_selector("div[role='feed']")
                if feed:
                    for _ in range(SCROLL_ITERS):
                        await feed.evaluate("el => el.scrollTop = el.scrollHeight")
                        
                        # Check for "end of list" indicator
                        end = await page.query_selector(".HlvSq")
                        if end:
                            txt = (await end.inner_text()).strip()
                            if "end of the list" in txt.lower() or "no results" in txt.lower():
                                logger.info("  [collect] reached end of list")
                                break
                        
                        # Random pause between scrolls
                        await asyncio.sleep(random.uniform(0.3, 0.7))
                        
                        links = await self._extract_place_links(page)
                        if len(links) >= MAX_RESULTS:
                            logger.info(f"  [collect] reached {MAX_RESULTS} results limit")
                            break
                
                links = await self._extract_place_links(page)
                
                # Filter by distance
                seen: Set[str] = set()
                urls: List[str] = []
                skipped = 0
                
                for lnk in links:
                    if lnk in self.processed_urls or lnk in seen:
                        continue
                    
                    coords = self._coords_from_url(lnk)
                    if coords:
                        dist = self._haversine_m(lat, lng, coords[0], coords[1])
                        if dist > self.max_distance:
                            skipped += 1
                            continue
                    
                    urls.append(lnk)
                    seen.add(lnk)
                
                if skipped:
                    logger.info(f"  [collect] skipped {skipped} results outside {self.max_distance/1000:.0f}km radius")
                
                logger.info(f"  [collect] found {len(urls)} new businesses for term '{term}'")
                self._collect_succeeded = True
                return urls[:MAX_RESULTS]
                
            except Exception as exc:
                logger.error(f"  [collect] attempt {attempt} error: {exc}")
                if attempt < retries:
                    await asyncio.sleep(2 * attempt)
        
        return []

    async def _scrape_details(self, page: Page, url: str) -> Optional[dict]:
        """Scrape detailed information from a business page"""
        for attempt in range(1, 3):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_selector("h1.DUwDvf", timeout=15000)
                break
            except Exception as exc:
                if attempt == 2:
                    logger.error(f"  [detail] error for {url}: {exc}")
                    return None
                await asyncio.sleep(random.uniform(3, 6))
        
        try:
            await asyncio.sleep(random.uniform(0.5, 1.5))
            
            # Scroll to load all content
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, 300)")
                await asyncio.sleep(0.2)
            
            async def _text(selector: str) -> str:
                try:
                    el = await page.query_selector(selector)
                    return (await el.inner_text()).strip() if el else ""
                except Exception:
                    return ""
            
            async def _attr(selector: str, attr: str) -> str:
                try:
                    el = await page.query_selector(selector)
                    return (await el.get_attribute(attr) or "").strip() if el else ""
                except Exception:
                    return ""
            
            # Extract business information
            name = await _text("h1.DUwDvf")
            
            # Get address
            address = ""
            for sel in [
                "button[data-item-id='address'] .Io6YTe",
                "button[data-item-id='address']",
                "[data-item-id='address'] .fontBodyMedium",
            ]:
                t = await _text(sel)
                if t and len(t) > 5:
                    address = t
                    break
            
            # Get phone
            phone = ""
            for sel in [
                "button[data-item-id*='phone'] .Io6YTe",
                "button[data-item-id*='phone']",
                "button[data-tooltip='Copy phone number']",
            ]:
                t = await _text(sel)
                if t and ("+" in t or any(c.isdigit() for c in t)):
                    phone = t
                    break
            
            # Get website
            website = await _attr("a[data-item-id='authority']", "href")
            
            # Get rating and reviews
            rating = await _text(".F7nice span[aria-hidden='true']")
            reviews = await _text(".F7nice span[aria-label*='reviews']")
            
            # Get category
            category = await _text("button.DkEaL")
            
            # Get opening hours
            hours = await _text(".ZDu9vd span")
            
            # Get about information
            about = ""
            try:
                snippets = await page.eval_on_selector_all(
                    ".jftiEf .wiI7pd",
                    "els => els.slice(0,3).map(e => e.innerText.trim()).filter(t => t.length > 10)"
                )
                about = " | ".join(snippets)
            except Exception:
                pass
            
            return {
                "original_data": {
                    "profile_url": "",
                    "name": name,
                    "formerly_known_as": "",
                    "address": address,
                    "rating": rating,
                    "review_count": reviews,
                    "pricing": {"packages": [], "detailed_breakdown": []},
                    "about_us": {"title": "", "content": ""},
                },
                "gmaps_url": url,
                "gmaps_name": name,
                "gmaps_address": address,
                "gmaps_phone": phone,
                "gmaps_rating": rating,
                "gmaps_reviews_count": reviews,
                "gmaps_opening_hours": hours,
                "gmaps_category": category,
                "gmaps_website": website,
                "gmaps_image_urls": [],
                "images": [],
                "gmaps_about": about,
                "scraped_at": datetime.now().isoformat(),
                "location_lat": self.lat,
                "location_lng": self.lng,
                "search_term": self.search_term,
            }
            
        except Exception as exc:
            logger.error(f"  [detail] error for {url}: {exc}")
            return None

    async def _pipeline_worker(
        self,
        pool: ContextPool,
        url_queue: asyncio.Queue,
        idx_counter: List[int],
        total_ref: List[int],
    ):
        """Worker that processes URLs from the queue"""
        while True:
            item = await url_queue.get()
            if item is None:
                url_queue.task_done()
                break
            
            url = item
            
            async with self._lock:
                if idx_counter[0] >= MAX_RESULTS or url in self.processed_urls:
                    url_queue.task_done()
                    break
            
            await asyncio.sleep(random.uniform(0.5, 1.5))
            
            async with pool.acquire() as (ctx, page):
                profile = await self._scrape_details(page, url)
            
            if profile:
                async with self._lock:
                    self.scraped_data.append(profile)
                    self.processed_urls.add(url)
                    self._dirty = True
                    idx_counter[0] += 1
                    should_save = idx_counter[0] % SAVE_EVERY == 0
                    idx = idx_counter[0]
                
                if should_save:
                    await self._save()
                
                logger.info(f"  ✓ [{idx}/{total_ref[0] or '?'}] {profile['gmaps_name']}")
            
            url_queue.task_done()

    async def scrape_all_async(self, pool: ContextPool, max_workers: int = 8):
        """Main scraping method"""
        logger.info("=" * 70)
        logger.info(f"Location: {self.label}")
        logger.info(f"Coordinates: {self.lat}, {self.lng}")
        logger.info(f"Zoom: {self.zoom}z, Max Distance: {self.max_distance/1000:.1f}km")
        logger.info(f"Output: {self.output_json}")
        logger.info("=" * 70)
        
        url_queue = asyncio.Queue()
        idx_counter = [0]
        total_ref = [0]
        
        search_terms = self._build_search_terms()

        # Try the broader search terms first, then widen the zoom if needed.
        for term in search_terms:
            for zoom_level in [self.zoom] + FALLBACK_ZOOMS:
                async with pool.acquire() as (_, collect_page):
                    business_urls = await self._collect_urls(
                        collect_page, self.lat, self.lng, zoom_level, term
                    )
                if business_urls:
                    self.zoom = zoom_level
                    logger.info(f"↩ Using zoom {zoom_level}z and term '{term}'")
                    break
            if business_urls:
                break

        if not business_urls:
            logger.warning(f"No businesses found for: {self.label}")
        
        total_ref[0] = len(business_urls)
        
        if not business_urls:
            logger.warning(f"No businesses found for: {self.label}")
            if not os.path.exists(self.output_json):
                os.makedirs(
                    os.path.dirname(os.path.abspath(self.output_json)),
                    exist_ok=True,
                )
                with open(self.output_json, "w") as f:
                    json.dump([], f)
            return
        
        # Start workers
        workers = [
            asyncio.create_task(
                self._pipeline_worker(pool, url_queue, idx_counter, total_ref)
            )
            for _ in range(max_workers)
        ]
        
        # Add URLs to queue
        for url in business_urls:
            await url_queue.put(url)
        for _ in workers:
            await url_queue.put(None)
        
        # Wait for all workers to complete
        await asyncio.gather(*workers)
        
        # Final save
        self._dirty = True
        await self._save()
        
        logger.info(
            f"DONE | {self.label} | ✓{idx_counter[0]} businesses | {self.output_json}"
        )

# ------------------------- LOCATION WORKER -------------------------

_CRASH_ERRORS = (
    "connection closed", "pipe closed", "target closed", 
    "browser has been closed", "browser context has been closed"
)

def _is_crash(exc: Exception) -> bool:
    return any(p in str(exc).lower() for p in _CRASH_ERRORS)

async def scrape_location(
    loc: dict,
    pool: ContextPool,
    completed: set,
    started: set,
    failed: set,
    breaks: AsyncBreakStrategy,
    progress_lock: asyncio.Lock,
) -> Tuple[str, bool, bool]:
    """Scrape a single location"""
    key = loc_key(loc)
    output_path = os.path.join(DATA_DIR, f"{key}.json")
    
    # Mark as started
    async with progress_lock:
        started.add(key)
        failed.discard(key)
        save_progress(completed, started, failed)
    
    await asyncio.sleep(random.uniform(1.0, 3.0))
    
    lat = loc.get("lat")
    lng = loc.get("lng")
    label = f"{loc.get('area','')} {loc.get('city','')} {loc.get('state','')}".strip()
    
    logger.info(f"▶ Starting: {label}")
    
    try:
        # Get optimal settings for this location
        settings = get_location_settings(loc)
        
        scraper = GoogleMapsGeoScraper(
            search_term=settings['search_terms'][0],
            lat=lat,
            lng=lng,
            output_json=output_path,
            zoom=settings['zoom'],
            max_distance=settings['max_distance'],
            label=label,
        )
        
        await scraper.scrape_all_async(
            pool,
            max_workers=DETAIL_WORKERS,
        )
        
        # Check if we got results
        blocked = (
            os.path.exists(output_path)
            and _is_empty_output(output_path)
            and scraper._collect_succeeded
        )
        
        if blocked:
            logger.warning(f"⚠ Empty results for {key} — possible block")
        
        logger.info(f"✓ Done: {key}")
        return key, True, blocked
        
    except Exception as exc:
        if _is_crash(exc):
            logger.warning(f"✗ Browser crash for {key}: {exc}")
            return key, False, False
        logger.error(f"✗ Failed: {key} — {exc}")
        return key, False, False

async def location_worker(
    worker_id: int,
    loc_queue: asyncio.Queue,
    pool: ContextPool,
    completed: set,
    started: set,
    failed: set,
    all_count: int,
    breaks: AsyncBreakStrategy,
    progress_lock: asyncio.Lock,
):
    """Worker that processes locations from the queue"""
    while True:
        loc = await loc_queue.get()
        if loc is None:
            loc_queue.task_done()
            break
        
        key, success, blocked = await scrape_location(
            loc, pool, completed, started, failed, breaks, progress_lock
        )
        
        if blocked:
            await breaks.on_blocked()
        
        if success:
            async with progress_lock:
                completed.add(key)
                started.discard(key)
                failed.discard(key)
                save_progress(completed, started, failed)
            logger.info(f"[W{worker_id}] Progress: {len(completed)}/{all_count}")
            await breaks.on_location_complete(success)
        else:
            async with progress_lock:
                failed.add(key)
                started.discard(key)
                save_progress(completed, started, failed)
            logger.info(f"[W{worker_id}] Failed: {key}")
            await breaks.on_location_complete(False)
        
        loc_queue.task_done()

# ------------------------- MAIN -------------------------

async def main():
    """Main entry point"""
    os.makedirs(DATA_DIR, exist_ok=True)
    while True:
        try:
            await _run()
            break
        except Exception as exc:
            logger.error(f"Top-level crash ({exc}), restarting in 30s…")
            await asyncio.sleep(30)

async def _run():
    """Main execution loop"""
    os.makedirs(DATA_DIR, exist_ok=True)
    
    # Load locations
    try:
        df = pd.read_excel(LOCATIONS_FILE)
    except Exception as e:
        logger.error(f"Could not read locations file: {e}")
        logger.info("Looking for alternative file names...")
        alt_files = [f for f in os.listdir(BASE_DIR) if f.endswith('.xlsx') and 'Jharkhand' in f]
        if alt_files:
            df = pd.read_excel(os.path.join(BASE_DIR, alt_files[0]))
            logger.info(f"Using alternative file: {alt_files[0]}")
        else:
            raise
    
    # Validate columns
    required = ["area", "city", "district", "state", "country", "postalCode",
                "location.coordinates[0]", "location.coordinates[1]", "_id"]
    
    # Check if columns exist, use alternative names if not
    alt_coord_cols = ["location.coordinates[0]", "location.coordinates[1]"]
    coord_col0 = None
    coord_col1 = None
    
    for col in df.columns:
        if "coordinates[0]" in col or "coord[0]" in col:
            coord_col0 = col
        if "coordinates[1]" in col or "coord[1]" in col:
            coord_col1 = col
    
    if coord_col0 and coord_col1:
        df = df.rename(columns={
            coord_col0: "lng",
            coord_col1: "lat",
        })
    else:
        raise ValueError(
            f"Required coordinate columns not found in {LOCATIONS_FILE}. "
            f"Found columns: {list(df.columns)}"
        )
    
    # Ensure required columns exist
    for req in required:
        if req not in df.columns and req not in ["location.coordinates[0]", "location.coordinates[1]"]:
            raise ValueError(f"Required column '{req}' not found in {LOCATIONS_FILE}")
    
    # Handle missing coordinate columns
    if "lat" not in df.columns or "lng" not in df.columns:
        if "location.coordinates[0]" in df.columns and "location.coordinates[1]" in df.columns:
            df = df.rename(columns={
                "location.coordinates[0]": "lng",
                "location.coordinates[1]": "lat",
            })
    
    # Drop rows with missing coordinates
    df = df.dropna(subset=["area", "lat", "lng"])
    all_locations = df.to_dict("records")
    logger.info(f"Total locations: {len(all_locations)}")
    
    # Load progress
    progress = load_progress()
    completed = progress["completed"]
    started = progress["started"]
    failed = progress.get("failed", set())
    
    # Recover completed from existing data
    if started:
        recovered = {
            k for k in started 
            if _is_empty_output(os.path.join(DATA_DIR, f"{k}.json"))
        }
        if recovered:
            logger.info(f"↩ Auto-completing {len(recovered)} location(s) with existing data")
            completed.update(recovered)
        requeue = started - recovered
        if requeue:
            logger.info(f"↩ Re-queuing {len(requeue)} incomplete location(s)")
        started.clear()
        save_progress(completed, started, failed)
    
    # Determine pending locations
    pending = [
        loc for loc in all_locations 
        if loc_key(loc) not in completed and loc_key(loc) not in failed
    ]
    
    logger.info("=" * 60)
    logger.info(f"Total locations    : {len(all_locations)}")
    logger.info(f"Completed          : {len(completed)}")
    logger.info(f"Failed (will retry): {len(failed)}")
    logger.info(f"Pending            : {len(pending)}")
    logger.info(f"Location workers   : {LOCATION_WORKERS}")
    logger.info(f"Context pool size  : {CTX_POOL_SIZE}")
    logger.info(f"Detail workers     : {DETAIL_WORKERS}")
    logger.info("=" * 60)
    
    if not pending:
        logger.info("✅ All locations already scraped!")
        return
    
    breaks = AsyncBreakStrategy()
    progress_lock = asyncio.Lock()
    
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--memory-pressure-off",
                "--disable-blink-features=AutomationControlled",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--metrics-recording-only",
                "--mute-audio",
                "--no-first-run",
                "--js-flags=--max-old-space-size=512",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        
        pool = ContextPool(browser, size=CTX_POOL_SIZE)
        await pool.initialize()
        
        loc_queue = asyncio.Queue()
        for loc in pending:
            await loc_queue.put(loc)
        for _ in range(LOCATION_WORKERS):
            await loc_queue.put(None)
        
        workers = [
            asyncio.create_task(
                location_worker(
                    i, loc_queue, pool, completed, started, failed,
                    len(all_locations), breaks, progress_lock,
                )
            )
            for i in range(LOCATION_WORKERS)
        ]
        
        try:
            await asyncio.gather(*workers)
        except Exception as exc:
            logger.error(f"Worker crash: {exc}")
        finally:
            try:
                await pool.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
    
    # Final summary
    final_progress = load_progress()
    logger.info("=" * 60)
    logger.info("📊 SCRAPING COMPLETE")
    logger.info(f"   Total locations: {len(all_locations)}")
    logger.info(f"   Completed: {len(final_progress['completed'])}")
    logger.info(f"   Failed: {len(final_progress.get('failed', set()))}")
    logger.info("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())