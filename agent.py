"""
Lufthansa flight search agent using Playwright.

Searches lufthansa.com for:
  - Return ticket (both legs in one booking)
  - Two separate one-way tickets (outbound + inbound)

For each search it collects all available flights within a ±2 h time window
around the requested departure time, plus flights outside that window so we
can detect whether shifting by ±2 h would be meaningfully cheaper (>10 %).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FlightLeg:
    """A single one-way flight offer scraped from the results page."""

    origin: str
    destination: str
    departure: datetime
    arrival: datetime
    flight_no: str
    duration_min: int
    stops: int
    cabin: str
    price: float
    currency: str
    # Populated later after FX conversion
    price_eur: float = 0.0


@dataclass
class SearchResult:
    """All raw legs returned for one search run."""

    trip_type: str  # "return" | "outbound" | "inbound"
    legs: list[FlightLeg] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def _new_context(browser: Browser) -> BrowserContext:
    ctx = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        user_agent=_UA,
        locale="en-US",
        timezone_id="Europe/Berlin",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    # Basic stealth: hide webdriver property
    await ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return ctx


async def _dismiss_cookie_banner(page: Page) -> None:
    """Accept / close any cookie consent overlay."""
    selectors = [
        "button[id*='accept']",
        "button[data-id='cm-acceptAll']",
        "button.onetrust-accept-btn-handler",
        "button[aria-label*='Accept']",
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept all')",
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Accept')",
        "[data-testid='uc-accept-all-button']",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await page.wait_for_timeout(800)
                return
        except Exception:
            pass


async def _fill_airport(page: Page, field_selector: str, iata: str) -> None:
    """Type an IATA code into an airport autocomplete field and pick first suggestion."""
    loc = page.locator(field_selector).first
    await loc.click()
    await page.wait_for_timeout(300)
    await loc.fill("")
    await page.wait_for_timeout(200)
    await loc.type(iata, delay=80)
    # Wait for suggestion dropdown
    await page.wait_for_timeout(1200)
    # Try to click the first autocomplete suggestion
    suggestion_selectors = [
        f"[data-testid='autocomplete-item']:first-child",
        ".autocomplete-suggestion:first-child",
        "[role='option']:first-child",
        f"li[data-value*='{iata}']:first-child",
        f"li:has-text('{iata}'):first-child",
        ".lh-autocomplete__item:first-child",
        "[class*='suggestion']:first-child",
        "[class*='option']:first-child",
        "[class*='item']:first-child",
    ]
    for sel in suggestion_selectors:
        try:
            sug = page.locator(sel).first
            if await sug.is_visible(timeout=1500):
                await sug.click()
                await page.wait_for_timeout(400)
                return
        except Exception:
            pass
    # Fallback: press Enter / Tab to accept first suggestion
    await loc.press("ArrowDown")
    await page.wait_for_timeout(300)
    await loc.press("Enter")
    await page.wait_for_timeout(400)


async def _set_date(page: Page, date: datetime, field_index: int = 0) -> None:
    """Set a date in the date picker. Tries direct input, then calendar navigation."""
    date_str = date.strftime("%d/%m/%Y")
    date_selectors = [
        "[data-testid='date-input']",
        "input[placeholder*='dd']",
        "input[placeholder*='Date']",
        "input[name*='date']",
        "input[id*='date']",
        ".date-picker input",
        "[class*='dateInput'] input",
    ]
    for sel in date_selectors:
        locs = page.locator(sel)
        count = await locs.count()
        if count > field_index:
            loc = locs.nth(field_index)
            try:
                await loc.click()
                await page.wait_for_timeout(400)
                await loc.triple_click()
                await loc.type(date_str, delay=60)
                await page.wait_for_timeout(600)
                return
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------

_PRICE_RE = re.compile(r"[\d\.,]+")
_CURRENCY_SYMBOLS = {"€": "EUR", "£": "GBP", "$": "USD", "¥": "JPY", "CHF": "CHF"}


def _parse_price(text: str) -> tuple[float, str]:
    """Extract numeric price and currency from a price string like '€ 345,00' or '345.00 EUR'."""
    currency = "EUR"
    for sym, code in _CURRENCY_SYMBOLS.items():
        if sym in text:
            currency = code
            break
    # Also look for 3-letter currency codes
    m = re.search(r"\b([A-Z]{3})\b", text)
    if m and m.group(1) in {"EUR", "USD", "GBP", "CHF", "JPY", "SEK", "NOK", "DKK"}:
        currency = m.group(1)
    nums = _PRICE_RE.findall(text)
    if not nums:
        return 0.0, currency
    raw = nums[-1].replace(".", "").replace(",", ".")
    try:
        return float(raw), currency
    except ValueError:
        return 0.0, currency


def _parse_duration(text: str) -> int:
    """Parse '2h 30m' → 150 minutes."""
    h = re.search(r"(\d+)\s*h", text)
    m = re.search(r"(\d+)\s*m", text)
    return (int(h.group(1)) if h else 0) * 60 + (int(m.group(1)) if m else 0)


def _parse_time(text: str, base_date: datetime) -> datetime:
    """Parse 'HH:MM' relative to base_date."""
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if not m:
        return base_date
    return base_date.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Main agent class
# ---------------------------------------------------------------------------


class LufthansaAgent:
    """Playwright-based agent that scrapes Lufthansa flight offers."""

    BASE_URL = "https://www.lufthansa.com/de/en/flight-search"

    CABIN_MAP = {
        "economy": "Economy",
        "premium_economy": "Premium Economy",
        "business": "Business",
        "first": "First",
    }

    def __init__(
        self,
        origin: str,
        destination: str,
        outbound_date: datetime,
        outbound_time: datetime,
        inbound_date: datetime,
        inbound_time: datetime,
        cabin: str = "economy",
        headless: bool = True,
        debug: bool = False,
    ) -> None:
        self.origin = origin.upper()
        self.destination = destination.upper()
        self.outbound_date = outbound_date
        self.outbound_time = outbound_time
        self.inbound_date = inbound_date
        self.inbound_time = inbound_time
        self.cabin = cabin.lower()
        self.headless = headless
        self.debug = debug

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> dict[str, list[FlightLeg]]:
        """
        Execute all three searches (return, outbound one-way, inbound one-way).
        Returns a dict with keys 'return', 'outbound', 'inbound'.
        """
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            try:
                results: dict[str, list[FlightLeg]] = {}

                # --- Return ticket search ---
                ctx = await _new_context(browser)
                page = await ctx.new_page()
                legs = await self._search_return(page)
                results["return"] = legs
                await ctx.close()

                await asyncio.sleep(2)

                # --- Outbound one-way ---
                ctx = await _new_context(browser)
                page = await ctx.new_page()
                legs = await self._search_oneway(
                    page,
                    self.origin,
                    self.destination,
                    self.outbound_date,
                    direction="outbound",
                )
                results["outbound"] = legs
                await ctx.close()

                await asyncio.sleep(2)

                # --- Inbound one-way ---
                ctx = await _new_context(browser)
                page = await ctx.new_page()
                legs = await self._search_oneway(
                    page,
                    self.destination,
                    self.origin,
                    self.inbound_date,
                    direction="inbound",
                )
                results["inbound"] = legs
                await ctx.close()

                return results
            finally:
                await browser.close()

    # ------------------------------------------------------------------
    # Search routines
    # ------------------------------------------------------------------

    async def _search_return(self, page: Page) -> list[FlightLeg]:
        logger.info("Searching return %s→%s / %s→%s",
                    self.origin, self.destination, self.destination, self.origin)
        await page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        await _dismiss_cookie_banner(page)
        await page.wait_for_timeout(500)

        await self._fill_search_form(
            page,
            origin=self.origin,
            destination=self.destination,
            outbound_date=self.outbound_date,
            inbound_date=self.inbound_date,
            trip_type="return",
        )

        legs = await self._scrape_legs(page, self.outbound_date, "return")
        return legs

    async def _search_oneway(
        self,
        page: Page,
        orig: str,
        dest: str,
        date: datetime,
        direction: str,
    ) -> list[FlightLeg]:
        logger.info("Searching one-way %s→%s on %s", orig, dest, date.date())
        await page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        await _dismiss_cookie_banner(page)
        await page.wait_for_timeout(500)

        await self._fill_search_form(
            page,
            origin=orig,
            destination=dest,
            outbound_date=date,
            inbound_date=None,
            trip_type="oneway",
        )

        legs = await self._scrape_legs(page, date, direction)
        return legs

    # ------------------------------------------------------------------
    # Form filling
    # ------------------------------------------------------------------

    async def _fill_search_form(
        self,
        page: Page,
        origin: str,
        destination: str,
        outbound_date: datetime,
        inbound_date: Optional[datetime],
        trip_type: str,
    ) -> None:
        """Fill and submit the Lufthansa booking widget."""

        # 1. Select trip type
        if trip_type == "oneway":
            one_way_selectors = [
                "label:has-text('One way')",
                "label:has-text('One-way')",
                "input[value='OW']",
                "[data-testid='tripType-OW']",
                "button:has-text('One way')",
                "[class*='oneWay']",
            ]
            for sel in one_way_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        await page.wait_for_timeout(400)
                        break
                except Exception:
                    pass

        # 2. Origin airport
        origin_selectors = [
            "[data-testid='origin-input']",
            "input[placeholder*='From']",
            "input[placeholder*='Origin']",
            "input[name*='origin']",
            "input[id*='origin']",
            "input[aria-label*='From']",
            "[class*='origin'] input",
        ]
        filled = False
        for sel in origin_selectors:
            try:
                if await page.locator(sel).first.is_visible(timeout=2000):
                    await _fill_airport(page, sel, origin)
                    filled = True
                    break
            except Exception:
                pass
        if not filled:
            logger.warning("Could not find origin field")

        await page.wait_for_timeout(500)

        # 3. Destination airport
        dest_selectors = [
            "[data-testid='destination-input']",
            "input[placeholder*='To']",
            "input[placeholder*='Destination']",
            "input[name*='destination']",
            "input[id*='destination']",
            "input[aria-label*='To']",
            "[class*='destination'] input",
        ]
        for sel in dest_selectors:
            try:
                if await page.locator(sel).first.is_visible(timeout=2000):
                    await _fill_airport(page, sel, destination)
                    break
            except Exception:
                pass

        await page.wait_for_timeout(500)

        # 4. Outbound date
        await _set_date(page, outbound_date, field_index=0)

        # 5. Inbound date (return only)
        if inbound_date:
            await _set_date(page, inbound_date, field_index=1)

        await page.wait_for_timeout(400)

        # 6. Cabin class
        cabin_label = self.CABIN_MAP.get(self.cabin, "Economy")
        cabin_selectors = [
            "[data-testid='cabin-class-dropdown']",
            "select[name*='cabin']",
            "select[id*='cabin']",
            "[class*='cabin'] select",
            "button:has-text('Economy')",
            "button:has-text('Cabin')",
            "[aria-label*='cabin']",
            "[aria-label*='Cabin']",
        ]
        for sel in cabin_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2000):
                    tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        await loc.select_option(label=cabin_label)
                    else:
                        await loc.click()
                        await page.wait_for_timeout(400)
                        # Look for option in dropdown
                        opt_sel = f"[role='option']:has-text('{cabin_label}')"
                        opt = page.locator(opt_sel).first
                        if await opt.is_visible(timeout=1500):
                            await opt.click()
                    await page.wait_for_timeout(300)
                    break
            except Exception:
                pass

        await page.wait_for_timeout(300)

        # 7. Submit
        search_selectors = [
            "button[type='submit']",
            "button:has-text('Search flights')",
            "button:has-text('Search')",
            "[data-testid='search-button']",
            "[class*='searchButton']",
            "[class*='submit']",
        ]
        for sel in search_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    break
            except Exception:
                pass

        # Wait for results to load
        await page.wait_for_timeout(3000)
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)

        if self.debug:
            await page.screenshot(path=f"debug_{trip_type}_{origin}_{destination}.png")

    # ------------------------------------------------------------------
    # Result scraping
    # ------------------------------------------------------------------

    async def _scrape_legs(
        self, page: Page, base_date: datetime, direction: str
    ) -> list[FlightLeg]:
        """
        Scrape flight cards from the results page and return FlightLeg objects.
        Tries multiple selector strategies to handle LH's dynamic layout.
        """
        legs: list[FlightLeg] = []

        # Strategy 1: Look for structured flight card elements
        card_selectors = [
            "[data-testid='flight-offer']",
            "[data-testid='offer-card']",
            "[class*='flightOffer']",
            "[class*='flight-offer']",
            "[class*='offerCard']",
            "[class*='resultItem']",
            "[class*='result-item']",
            ".flight-result",
            "[class*='flightResult']",
        ]

        cards_found = False
        for sel in card_selectors:
            cards = page.locator(sel)
            count = await cards.count()
            if count > 0:
                logger.info("Found %d flight cards with selector '%s'", count, sel)
                cards_found = True
                for i in range(count):
                    leg = await self._parse_card(cards.nth(i), base_date, direction)
                    if leg:
                        legs.append(leg)
                break

        # Strategy 2: Try to parse page text as a fallback
        if not cards_found or not legs:
            logger.info("Falling back to page-text parsing for %s", direction)
            legs = await self._parse_page_text(page, base_date, direction)

        logger.info("Scraped %d legs for direction=%s", len(legs), direction)
        return legs

    async def _parse_card(
        self, card, base_date: datetime, direction: str
    ) -> Optional[FlightLeg]:
        """Extract a FlightLeg from a single flight card element."""
        try:
            text = await card.inner_text()
            return self._leg_from_text(text, base_date, direction)
        except Exception as exc:
            logger.debug("Card parse error: %s", exc)
            return None

    async def _parse_page_text(
        self, page: Page, base_date: datetime, direction: str
    ) -> list[FlightLeg]:
        """
        Last-resort: extract all visible text and attempt to find flight blocks.
        Looks for patterns like 'HH:MM  HH:MM  Xh Ym  €NNN'.
        """
        try:
            text = await page.inner_text("body")
        except Exception:
            return []

        legs: list[FlightLeg] = []
        # Split by potential flight block separators
        blocks = re.split(r"\n{2,}", text)
        for block in blocks:
            leg = self._leg_from_text(block, base_date, direction)
            if leg and leg.price > 0:
                legs.append(leg)
        return legs

    def _leg_from_text(
        self, text: str, base_date: datetime, direction: str
    ) -> Optional[FlightLeg]:
        """Parse a FlightLeg from raw text block."""
        # Need at minimum a price and a departure time
        times = re.findall(r"\b(\d{1,2}:\d{2})\b", text)
        if len(times) < 2:
            return None

        price_matches = re.findall(
            r"(?:€|EUR|£|GBP|\$|USD)?\s*([\d]{2,4}[,.][\d]{2}|[\d]{3,5})\s*(?:€|EUR|£|GBP|\$|USD)?",
            text,
        )
        if not price_matches:
            return None

        dep_time = _parse_time(times[0], base_date)
        arr_time = _parse_time(times[1], base_date)
        if arr_time < dep_time:
            arr_time += timedelta(days=1)

        price_raw = price_matches[-1].replace(",", ".").replace(".", "", price_matches[-1].count(".") - 1)
        try:
            price = float(price_raw.replace(",", "."))
        except ValueError:
            return None

        if price < 1:
            return None

        currency_match = re.search(r"€|EUR|£|GBP|\$|USD", text)
        currency = "EUR"
        if currency_match:
            sym = currency_match.group(0)
            currency = _CURRENCY_SYMBOLS.get(sym, sym)

        flight_no_m = re.search(r"\b(LH|OS|SN|LX)\s*(\d{3,4})\b", text)
        flight_no = f"{flight_no_m.group(1)}{flight_no_m.group(2)}" if flight_no_m else "LH????"

        duration_m = re.search(r"(\d+\s*h\s*\d*\s*m?)", text, re.IGNORECASE)
        duration_min = _parse_duration(duration_m.group(1)) if duration_m else int(
            (arr_time - dep_time).total_seconds() / 60
        )

        stops_m = re.search(r"(\d+)\s+stop", text, re.IGNORECASE)
        stops = int(stops_m.group(1)) if stops_m else (0 if "non-stop" in text.lower() or "direct" in text.lower() else 0)

        if direction == "inbound":
            orig, dest = self.destination, self.origin
        else:
            orig, dest = self.origin, self.destination

        return FlightLeg(
            origin=orig,
            destination=dest,
            departure=dep_time,
            arrival=arr_time,
            flight_no=flight_no,
            duration_min=duration_min,
            stops=stops,
            cabin=self.CABIN_MAP.get(self.cabin, "Economy"),
            price=price,
            currency=currency,
        )
