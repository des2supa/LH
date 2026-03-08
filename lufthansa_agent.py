"""
Lufthansa Flight Price Research Agent
--------------------------------------
Searches lufthansa.de for return vs. two one-way ticket combinations,
fetches live FX rates, and outputs a markdown comparison table.

Usage:
    python lufthansa_agent.py \
        --origin FRA --destination JFK \
        --outbound-date 2026-04-10 --outbound-window 08:00 12:00 \
        --inbound-date 2026-04-17 --inbound-window 16:00 20:00 \
        --cabin Business

Dependencies (install first):
    pip install playwright httpx tabulate asyncio
    playwright install chromium
"""

import asyncio
import argparse
import httpx
import json
import re
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from tabulate import tabulate
from pathlib import Path
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Flight:
    flight_number: str
    origin: str
    destination: str
    departure: str          # "HH:MM"
    arrival: str            # "HH:MM"
    duration: str           # "Xh Ym"
    stops: int
    price_eur: Optional[float]
    price_display: str      # Raw string from site (may be in USD etc.)
    cabin: str
    date: str               # "YYYY-MM-DD"
    is_preferred_window: bool = True


@dataclass
class SearchResult:
    search_type: str        # "return", "outbound_ow", "inbound_ow"
    flights: list[Flight] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# FX rate fetcher
# ---------------------------------------------------------------------------

async def get_fx_rate(from_currency: str = "EUR", to_currency: str = "USD") -> float:
    """Fetch live FX rate from frankfurter.app (free, no API key needed)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.frankfurter.app/latest",
                params={"from": from_currency, "to": to_currency},
            )
            resp.raise_for_status()
            data = resp.json()
            rate = data["rates"][to_currency]
            print(
                f"[FX] 1 {from_currency} = {rate} {to_currency} "
                f"(source: frankfurter.app, date: {data['date']})"
            )
            return rate
    except Exception as e:
        print(f"[FX] Warning: Could not fetch FX rate ({e}). Defaulting to 1.08.")
        return 1.08


# ---------------------------------------------------------------------------
# Lufthansa scraper (Playwright)
# ---------------------------------------------------------------------------

# Lufthansa cabin codes used in their URL / form
CABIN_MAP = {
    "Economy": "Y",
    "PremiumEconomy": "M",
    "Business": "C",
    "First": "F",
}

# Lufthansa deep-link search URL pattern (works as of early 2026).
# flightType: OW = one-way, RT = return
SEARCH_URL_TEMPLATE = (
    "https://www.lufthansa.com/de/en/flight-search"
    "?origin={origin}"
    "&destination={destination}"
    "&outwardDate={date}"
    "{return_part}"
    "&tripType={trip_type}"
    "&cabin={cabin_code}"
    "&adult=1"
    "&lang=en"
)


class LufthansaScraper:
    BASE_URL = "https://www.lufthansa.com/de/en/flight-search"

    def __init__(self, headless: bool = False):
        # headed mode (headless=False) reduces bot-detection risk
        self.headless = headless

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def search(
        self,
        origin: str,
        destination: str,
        date: str,
        cabin: str,
        trip_type: str,          # "return" or "oneway"
        return_date: Optional[str] = None,
        page: Optional[Page] = None,
        time_window: Optional[tuple[str, str]] = None,
    ) -> list[Flight]:
        """
        Navigate to Lufthansa, perform a search, and return Flight objects.

        Parameters
        ----------
        origin / destination : IATA codes (e.g. "FRA", "JFK")
        date                 : Outbound date "YYYY-MM-DD"
        cabin                : "Economy" | "PremiumEconomy" | "Business" | "First"
        trip_type            : "return" or "oneway"
        return_date          : Inbound date, required when trip_type == "return"
        page                 : Playwright Page object (caller manages browser lifetime)
        time_window          : Optional (start_hhmm, end_hhmm) to flag preferred flights
        """
        if page is None:
            raise ValueError("A Playwright Page must be provided.")

        cabin_code = CABIN_MAP.get(cabin, "Y")
        lh_trip_type = "RT" if trip_type == "return" else "OW"

        return_part = ""
        if trip_type == "return" and return_date:
            return_part = f"&returnDate={return_date}"

        url = SEARCH_URL_TEMPLATE.format(
            origin=origin,
            destination=destination,
            date=date,
            return_part=return_part,
            trip_type=lh_trip_type,
            cabin_code=cabin_code,
        )

        print(f"[Scraper] Navigating to: {url}")

        try:
            await self._fill_search_form(
                page=page,
                url=url,
                origin=origin,
                destination=destination,
                date=date,
                trip_type=trip_type,
                return_date=return_date,
                cabin=cabin,
                cabin_code=cabin_code,
            )
        except Exception as e:
            print(f"[Scraper] Form navigation error: {e}")
            raise

        flights = await self._scrape_results(
            page=page,
            date=date,
            origin=origin,
            destination=destination,
            cabin=cabin,
        )

        # Mark flights inside / outside the preferred time window
        if time_window:
            start_h, start_m = (int(x) for x in time_window[0].split(":"))
            end_h, end_m = (int(x) for x in time_window[1].split(":"))
            for f in flights:
                try:
                    dep_h, dep_m = (int(x) for x in f.departure.split(":"))
                    dep_mins = dep_h * 60 + dep_m
                    win_start = start_h * 60 + start_m
                    win_end = end_h * 60 + end_m
                    f.is_preferred_window = win_start <= dep_mins <= win_end
                except Exception:
                    f.is_preferred_window = True  # Don't penalise on parse failure

        return flights

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _accept_cookies(self, page: Page) -> None:
        """Dismiss cookie / consent banner if present."""
        # Lufthansa uses an OneTrust-style consent dialog.
        # We try several known selector patterns.
        selectors = [
            # OneTrust "Accept all" button
            "#onetrust-accept-btn-handler",
            "button[id*='accept-all']",
            "button[class*='accept-all']",
            # Generic patterns used on lh.com
            "[data-testid='cookiebanner-accept-button']",
            "button[class*='CookieBanner__acceptButton']",
            ".cookie-consent__accept",
            # Fallback text-based
            "button:has-text('Accept all')",
            "button:has-text('Alle akzeptieren')",
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                await btn.click(timeout=4000)
                await page.wait_for_timeout(800)
                print("[Scraper] Cookie banner dismissed.")
                return
            except Exception:
                continue
        print("[Scraper] No cookie banner found (or already dismissed).")

    async def _fill_search_form(
        self,
        page: Page,
        url: str,
        origin: str,
        destination: str,
        date: str,
        trip_type: str,
        return_date: Optional[str],
        cabin: str,
        cabin_code: str,
    ) -> None:
        """
        Navigate to the Lufthansa deep-link URL and wait for the results
        page to begin loading.  The deep-link pre-populates the form so
        we do not need to interact with individual inputs unless the URL
        approach fails, in which case we fall back to manual form filling.
        """
        # --- Attempt 1: deep-link URL -----------------------------------
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await self._accept_cookies(page)

            # Give the React app time to hydrate and submit the search
            await page.wait_for_timeout(3000)

            # Check whether we are on a results-like page
            # (the URL changes or a loading indicator appears)
            await page.wait_for_load_state("networkidle", timeout=30000)
            print("[Scraper] Deep-link navigation succeeded.")
            return
        except PWTimeout:
            print("[Scraper] Deep-link timed out, falling back to manual form fill.")
        except Exception as e:
            print(f"[Scraper] Deep-link error: {e}. Falling back to manual form fill.")

        # --- Attempt 2: manual form fill --------------------------------
        await page.goto(
            "https://www.lufthansa.com/de/en/flights",
            wait_until="domcontentloaded",
            timeout=45000,
        )
        await self._accept_cookies(page)
        await page.wait_for_timeout(2000)

        # Select trip type
        if trip_type == "oneway":
            ow_selectors = [
                "[data-testid='tripType-oneWay']",
                "label[for*='oneWay']",
                "input[value='OW']",
                "button:has-text('One-way')",
                "span:has-text('One-way')",
            ]
            for sel in ow_selectors:
                try:
                    await page.click(sel, timeout=3000)
                    break
                except Exception:
                    continue

        # Origin field
        origin_selectors = [
            "[data-testid='origin-input']",
            "input[placeholder*='Origin']",
            "input[placeholder*='From']",
            "#origin",
            "[aria-label*='Origin']",
            "[aria-label*='From']",
        ]
        for sel in origin_selectors:
            try:
                await page.fill(sel, origin, timeout=3000)
                await page.wait_for_timeout(1000)
                # Accept first autocomplete suggestion
                await page.keyboard.press("ArrowDown")
                await page.keyboard.press("Enter")
                break
            except Exception:
                continue

        # Destination field
        dest_selectors = [
            "[data-testid='destination-input']",
            "input[placeholder*='Destination']",
            "input[placeholder*='To']",
            "#destination",
            "[aria-label*='Destination']",
            "[aria-label*='To']",
        ]
        for sel in dest_selectors:
            try:
                await page.fill(sel, destination, timeout=3000)
                await page.wait_for_timeout(1000)
                await page.keyboard.press("ArrowDown")
                await page.keyboard.press("Enter")
                break
            except Exception:
                continue

        # Date – format as DD/MM/YYYY for Lufthansa's European locale
        dt = datetime.strptime(date, "%Y-%m-%d")
        lh_date = dt.strftime("%d/%m/%Y")

        date_selectors = [
            "[data-testid='outward-date-input']",
            "[data-testid='departureDate']",
            "input[placeholder*='Departure date']",
            "#outwardDate",
            "[aria-label*='Departure date']",
        ]
        for sel in date_selectors:
            try:
                await page.fill(sel, lh_date, timeout=3000)
                await page.keyboard.press("Tab")
                break
            except Exception:
                continue

        if trip_type == "return" and return_date:
            rt_dt = datetime.strptime(return_date, "%Y-%m-%d")
            lh_rt_date = rt_dt.strftime("%d/%m/%Y")
            ret_selectors = [
                "[data-testid='return-date-input']",
                "[data-testid='returnDate']",
                "input[placeholder*='Return date']",
                "#returnDate",
                "[aria-label*='Return date']",
            ]
            for sel in ret_selectors:
                try:
                    await page.fill(sel, lh_rt_date, timeout=3000)
                    await page.keyboard.press("Tab")
                    break
                except Exception:
                    continue

        # Cabin class
        cabin_selectors = [
            "[data-testid='cabin-class-selector']",
            "[data-testid='cabinClass']",
            "select[name*='cabin']",
            "#cabinClass",
            "[aria-label*='Cabin class']",
        ]
        for sel in cabin_selectors:
            try:
                await page.select_option(sel, value=cabin_code, timeout=3000)
                break
            except Exception:
                try:
                    await page.click(sel, timeout=3000)
                    # Click the option label in the dropdown
                    await page.click(f"[data-value='{cabin_code}'], li:has-text('{cabin}')", timeout=3000)
                except Exception:
                    continue

        # Submit search
        submit_selectors = [
            "[data-testid='search-button']",
            "button[type='submit']",
            "button:has-text('Search flights')",
            "button:has-text('Find flights')",
            "[aria-label*='Search']",
        ]
        for sel in submit_selectors:
            try:
                await page.click(sel, timeout=3000)
                break
            except Exception:
                continue

        await page.wait_for_load_state("networkidle", timeout=45000)

    async def _scrape_results(
        self,
        page: Page,
        date: str,
        origin: str,
        destination: str,
        cabin: str,
    ) -> list[Flight]:
        """
        Parse the search-results page into a list of Flight objects.

        Lufthansa's SPA renders results inside card components.
        We try multiple selector strategies to be resilient to DOM changes.
        """
        flights: list[Flight] = []

        # Take a screenshot for debugging
        screenshot_path = Path("output/debug_screenshot.png")
        screenshot_path.parent.mkdir(exist_ok=True)
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"[Scraper] Screenshot saved to {screenshot_path}")

        # ------------------------------------------------------------------
        # Strategy 1: look for structured flight-result cards
        # ------------------------------------------------------------------
        card_selectors = [
            "[data-testid*='flight-result']",
            "[data-testid*='flightResult']",
            ".fli-result",
            ".flight-result-card",
            "[class*='FlightCard']",
            "[class*='flightCard']",
            "[class*='ResultCard']",
            "[class*='resultCard']",
            "article[class*='flight']",
            "li[class*='flight']",
        ]

        cards = []
        for sel in card_selectors:
            try:
                await page.wait_for_selector(sel, timeout=20000)
                cards = await page.query_selector_all(sel)
                if cards:
                    print(f"[Scraper] Found {len(cards)} result cards using '{sel}'")
                    break
            except PWTimeout:
                continue
            except Exception:
                continue

        if not cards:
            print("[Scraper] No result cards found — attempting JSON extraction from page source.")
            flights = await self._extract_from_page_source(page, date, origin, destination, cabin)
            return flights[:5]

        # ------------------------------------------------------------------
        # Parse each card
        # ------------------------------------------------------------------
        price_selectors = [
            "[data-testid*='price']", "[data-testid*='Price']",
            "[class*='price']", "[class*='Price']",
            "span[class*='amount']", "span[class*='Amount']",
        ]
        time_selectors = [
            "[data-testid*='departure-time']", "[data-testid*='departureTime']",
            "[class*='departure']", "[class*='Departure']",
            "[class*='time']",
        ]
        arrival_selectors = [
            "[data-testid*='arrival-time']", "[data-testid*='arrivalTime']",
            "[class*='arrival']", "[class*='Arrival']",
        ]
        duration_selectors = [
            "[data-testid*='duration']", "[class*='duration']", "[class*='Duration']",
        ]
        stops_selectors = [
            "[data-testid*='stop']", "[class*='stop']", "[class*='Stop']",
            "[class*='connection']",
        ]
        flight_num_selectors = [
            "[data-testid*='flight-number']", "[data-testid*='flightNumber']",
            "[class*='flightNumber']", "[class*='flight-number']",
        ]

        async def first_text(element, selectors: list[str]) -> str:
            for sel in selectors:
                try:
                    el = await element.query_selector(sel)
                    if el:
                        txt = await el.inner_text()
                        txt = txt.strip()
                        if txt:
                            return txt
                except Exception:
                    continue
            return ""

        for card in cards[:10]:  # cap at 10 to parse; we return top 5
            try:
                price_text = await first_text(card, price_selectors)
                dep_text = await first_text(card, time_selectors)
                arr_text = await first_text(card, arrival_selectors)
                dur_text = await first_text(card, duration_selectors)
                stops_text = await first_text(card, stops_selectors)
                fn_text = await first_text(card, flight_num_selectors)

                # Fallback: grab all text and try to parse it
                if not price_text:
                    all_text = await card.inner_text()
                    price_text = _extract_price_from_text(all_text)

                price_eur = parse_price(price_text) if price_text else None

                stops = 0
                if stops_text:
                    m = re.search(r"(\d+)\s*stop", stops_text, re.IGNORECASE)
                    if m:
                        stops = int(m.group(1))
                    elif "nonstop" in stops_text.lower() or "direct" in stops_text.lower():
                        stops = 0

                flight = Flight(
                    flight_number=fn_text or "LH-???",
                    origin=origin,
                    destination=destination,
                    departure=_clean_time(dep_text),
                    arrival=_clean_time(arr_text),
                    duration=dur_text or "N/A",
                    stops=stops,
                    price_eur=price_eur,
                    price_display=price_text or "N/A",
                    cabin=cabin,
                    date=date,
                )
                flights.append(flight)
            except Exception as e:
                print(f"[Scraper] Error parsing card: {e}")
                continue

        if not flights:
            print("[Scraper] Cards found but could not parse — falling back to JSON extraction.")
            flights = await self._extract_from_page_source(page, date, origin, destination, cabin)

        return flights[:5]

    async def _extract_from_page_source(
        self,
        page: Page,
        date: str,
        origin: str,
        destination: str,
        cabin: str,
    ) -> list[Flight]:
        """
        Last-resort: attempt to extract flight data from JSON embedded in the page
        (Next.js / React hydration data, window.__INITIAL_STATE__, etc.).
        """
        flights: list[Flight] = []
        try:
            html = await page.content()
            # Save raw HTML for post-analysis
            debug_html = Path("output/debug_page.html")
            debug_html.write_text(html, encoding="utf-8")
            print(f"[Scraper] Raw HTML saved to {debug_html} ({len(html)} chars).")

            # Look for JSON blobs containing price data
            patterns = [
                r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});',
                r'window\.__NEXT_DATA__\s*=\s*(\{.*?\})',
                r'"flights"\s*:\s*(\[.*?\])',
                r'"offers"\s*:\s*(\[.*?\])',
            ]
            for pattern in patterns:
                matches = re.findall(pattern, html, re.DOTALL)
                for m in matches:
                    try:
                        obj = json.loads(m)
                        extracted = _parse_json_blob(obj, date, origin, destination, cabin)
                        if extracted:
                            print(f"[Scraper] Extracted {len(extracted)} flights from JSON blob.")
                            flights.extend(extracted)
                    except Exception:
                        continue
                if flights:
                    break
        except Exception as e:
            print(f"[Scraper] JSON extraction error: {e}")
        return flights


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _clean_time(raw: str) -> str:
    """Normalise time strings: '08:45', '08:45 AM', '845' → '08:45'."""
    if not raw:
        return "N/A"
    m = re.search(r"(\d{1,2}):(\d{2})", raw)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.search(r"(\d{3,4})", raw)
    if m:
        t = m.group(1).zfill(4)
        return f"{t[:2]}:{t[2:]}"
    return raw.strip()[:8] or "N/A"


def _extract_price_from_text(text: str) -> str:
    """Extract first price-like string from raw card text."""
    patterns = [
        r"€\s*[\d,]+(?:\.\d{2})?",
        r"EUR\s*[\d,]+(?:\.\d{2})?",
        r"\$\s*[\d,]+(?:\.\d{2})?",
        r"[\d,]+(?:\.\d{2})?\s*€",
        r"[\d,]+(?:\.\d{2})?\s*EUR",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(0).strip()
    return ""


def _parse_json_blob(
    obj,
    date: str,
    origin: str,
    destination: str,
    cabin: str,
) -> list[Flight]:
    """
    Recursively walk a JSON object looking for flight-shaped dicts.
    Very best-effort — Lufthansa may change schema at any time.
    """
    flights: list[Flight] = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            # Does this node look like a flight offer?
            if any(k in node for k in ("flightNumber", "flight_number", "segments")):
                try:
                    fn = (
                        node.get("flightNumber")
                        or node.get("flight_number")
                        or node.get("id", "LH???")
                    )
                    price_raw = (
                        node.get("price", {}).get("amount")
                        or node.get("totalPrice")
                        or node.get("price")
                        or ""
                    )
                    dep = (
                        node.get("departureTime", "")
                        or node.get("departure", {}).get("time", "")
                        or ""
                    )
                    arr = (
                        node.get("arrivalTime", "")
                        or node.get("arrival", {}).get("time", "")
                        or ""
                    )
                    dur = node.get("duration", "N/A")
                    stops = node.get("stops", node.get("numberOfStops", 0))
                    flights.append(
                        Flight(
                            flight_number=str(fn),
                            origin=origin,
                            destination=destination,
                            departure=_clean_time(str(dep)),
                            arrival=_clean_time(str(arr)),
                            duration=str(dur),
                            stops=int(stops) if stops else 0,
                            price_eur=parse_price(str(price_raw)),
                            price_display=str(price_raw),
                            cabin=cabin,
                            date=date,
                        )
                    )
                except Exception:
                    pass
            for v in node.values():
                walk(v)

    walk(obj)
    return flights


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------

def parse_price(price_str: str) -> Optional[float]:
    """Extract numeric price from strings like '€1,234' or 'EUR 1234.00'."""
    cleaned = re.sub(r"[^\d.,]", "", str(price_str)).replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def compare_options(
    return_result: SearchResult,
    outbound_result: SearchResult,
    inbound_result: SearchResult,
    fx_rate: float,
    fx_pair: str,
) -> dict:
    """
    Compare return ticket vs. two one-ways.
    Returns a structured comparison dict for output.
    """
    comparison: dict = {
        "return_best": None,
        "ow_outbound_best": None,
        "ow_inbound_best": None,
        "combined_ow_total": None,
        "return_total": None,
        "savings": None,
        "recommendation": "",
        "fx_rate": fx_rate,
        "fx_pair": fx_pair,
    }

    # Best return option (lowest price)
    return_flights = [f for f in return_result.flights if f.price_eur is not None]
    if return_flights:
        best_return = min(return_flights, key=lambda f: f.price_eur)
        comparison["return_best"] = best_return
        comparison["return_total"] = best_return.price_eur

    # Best outbound one-way
    ob_flights = [f for f in outbound_result.flights if f.price_eur is not None]
    if ob_flights:
        comparison["ow_outbound_best"] = min(ob_flights, key=lambda f: f.price_eur)

    # Best inbound one-way
    ib_flights = [f for f in inbound_result.flights if f.price_eur is not None]
    if ib_flights:
        comparison["ow_inbound_best"] = min(ib_flights, key=lambda f: f.price_eur)

    # Combined one-way total
    if comparison["ow_outbound_best"] and comparison["ow_inbound_best"]:
        comparison["combined_ow_total"] = (
            comparison["ow_outbound_best"].price_eur
            + comparison["ow_inbound_best"].price_eur
        )

    # Recommendation
    rt = comparison["return_total"]
    ow = comparison["combined_ow_total"]
    if rt and ow:
        diff = ow - rt
        comparison["savings"] = abs(diff)
        pct = abs(diff) / rt * 100
        if diff > 0:
            comparison["recommendation"] = (
                f"✅ Return ticket is cheaper by €{diff:.0f} ({pct:.1f}%). Book as return."
            )
        elif diff < 0:
            comparison["recommendation"] = (
                f"✅ Two one-ways are cheaper by €{abs(diff):.0f} ({pct:.1f}%). Book separately."
            )
        else:
            comparison["recommendation"] = "Price parity between return and two one-ways."
    elif rt and not ow:
        comparison["recommendation"] = (
            "⚠️  Only return prices available — cannot compare with one-ways."
        )
    elif ow and not rt:
        comparison["recommendation"] = (
            "⚠️  Only one-way prices available — cannot compare with return."
        )
    else:
        comparison["recommendation"] = (
            "⚠️  No price data retrieved. "
            "Check output/debug_screenshot.png and output/debug_page.html."
        )

    return comparison


# ---------------------------------------------------------------------------
# Output formatter
# ---------------------------------------------------------------------------

def format_flight_row(f: Flight, fx_rate: float, fx_pair: str) -> list:
    price_eur = f"€{f.price_eur:.0f}" if f.price_eur else "N/A"
    price_usd = f"${f.price_eur * fx_rate:.0f}" if f.price_eur else "N/A"
    window_flag = "✓" if f.is_preferred_window else "⚠ outside window"
    return [
        f.flight_number,
        f.origin,
        f.destination,
        f.date,
        f.departure,
        f.arrival,
        f.duration,
        f.stops,
        f.cabin,
        price_eur,
        price_usd,
        window_flag,
    ]


def generate_markdown_report(
    comparison: dict,
    outbound_result: SearchResult,
    inbound_result: SearchResult,
    return_result: SearchResult,
    args,
) -> str:
    fx = comparison["fx_rate"]
    fx_pair = comparison["fx_pair"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    headers = [
        "Flight", "From", "To", "Date", "Dep.", "Arr.", "Duration",
        "Stops", "Cabin", "EUR", "USD equiv.", "Time window",
    ]

    lines = [
        "# Lufthansa Flight Price Comparison",
        f"*Generated: {now} | FX rate: 1 EUR = {fx:.4f} USD (frankfurter.app)*",
        "",
        (
            f"**Route:** {args.origin} ↔ {args.destination} | "
            f"**Outbound:** {args.outbound_date} "
            f"{args.outbound_window[0]}–{args.outbound_window[1]} | "
            f"**Inbound:** {args.inbound_date} "
            f"{args.inbound_window[0]}–{args.inbound_window[1]} | "
            f"**Cabin:** {args.cabin}"
        ),
        "",
        "---",
        "",
        "## 🔄 Return Ticket Options (top 5)",
    ]

    if return_result.flights:
        rows = [format_flight_row(f, fx, fx_pair) for f in return_result.flights]
        lines.append(tabulate(rows, headers=headers, tablefmt="pipe"))
    else:
        lines.append(f"*No results found. {return_result.error or ''}*")

    lines += ["", "## ✈️ Outbound One-Way Options (top 5)"]
    if outbound_result.flights:
        rows = [format_flight_row(f, fx, fx_pair) for f in outbound_result.flights]
        lines.append(tabulate(rows, headers=headers, tablefmt="pipe"))
    else:
        lines.append(f"*No results found. {outbound_result.error or ''}*")

    lines += ["", "## ✈️ Inbound One-Way Options (top 5)"]
    if inbound_result.flights:
        rows = [format_flight_row(f, fx, fx_pair) for f in inbound_result.flights]
        lines.append(tabulate(rows, headers=headers, tablefmt="pipe"))
    else:
        lines.append(f"*No results found. {inbound_result.error or ''}*")

    lines += ["", "---", "## 💰 Summary & Recommendation", ""]

    rt = comparison.get("return_total")
    ow = comparison.get("combined_ow_total")
    summary_rows = []
    if rt:
        summary_rows.append(["Best return ticket", f"€{rt:.0f}", f"${rt * fx:.0f}"])
    if ow:
        summary_rows.append(["Two one-ways combined", f"€{ow:.0f}", f"${ow * fx:.0f}"])
    if rt and ow:
        diff = ow - rt
        summary_rows.append(["Difference", f"€{abs(diff):.0f}", f"${abs(diff) * fx:.0f}"])
    if summary_rows:
        lines.append(
            tabulate(summary_rows, headers=["Option", "EUR", "USD equiv."], tablefmt="pipe")
        )

    lines += ["", f"**{comparison.get('recommendation', 'Insufficient data for recommendation.')}**"]

    # Flag flights outside preferred time window
    all_flights = (
        return_result.flights + outbound_result.flights + inbound_result.flights
    )
    cheaper_outside = [
        f for f in all_flights if not f.is_preferred_window and f.price_eur is not None
    ]
    if cheaper_outside:
        lines += ["", "### ⏰ Notable flights outside your preferred time window"]
        rows = [format_flight_row(f, fx, fx_pair) for f in cheaper_outside]
        lines.append(tabulate(rows, headers=headers, tablefmt="pipe"))
        lines.append("")
        lines.append(
            "*Flag: Check if the timing difference is acceptable for the potential savings.*"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def run(args):
    print("[Agent] Starting Lufthansa flight research agent...")

    # 1. Fetch FX rate
    fx_rate = await get_fx_rate("EUR", "USD")

    # 2. Initialise scraper
    scraper = LufthansaScraper(headless=getattr(args, "headless", False))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=scraper.headless,
            slow_mo=80,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-GB",
        )
        # Stealth: hide webdriver flag
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await context.new_page()

        # 3. Search: return ticket
        print(f"[Agent] Searching return ticket {args.origin}↔{args.destination}...")
        return_result = SearchResult(search_type="return")
        try:
            return_result.flights = await scraper.search(
                origin=args.origin,
                destination=args.destination,
                date=args.outbound_date,
                cabin=args.cabin,
                trip_type="return",
                return_date=args.inbound_date,
                page=page,
                time_window=tuple(args.outbound_window),
            )
            print(f"[Agent] Return search: {len(return_result.flights)} flights found.")
        except Exception as e:
            return_result.error = str(e)
            print(f"[Agent] Error in return search: {e}")

        # Brief pause between searches to reduce bot-detection risk
        await page.wait_for_timeout(2000)

        # 4. Search: outbound one-way
        print(f"[Agent] Searching outbound one-way {args.origin}→{args.destination}...")
        outbound_result = SearchResult(search_type="outbound_ow")
        try:
            outbound_result.flights = await scraper.search(
                origin=args.origin,
                destination=args.destination,
                date=args.outbound_date,
                cabin=args.cabin,
                trip_type="oneway",
                page=page,
                time_window=tuple(args.outbound_window),
            )
            print(f"[Agent] Outbound search: {len(outbound_result.flights)} flights found.")
        except Exception as e:
            outbound_result.error = str(e)
            print(f"[Agent] Error in outbound search: {e}")

        await page.wait_for_timeout(2000)

        # 5. Search: inbound one-way
        print(f"[Agent] Searching inbound one-way {args.destination}→{args.origin}...")
        inbound_result = SearchResult(search_type="inbound_ow")
        try:
            inbound_result.flights = await scraper.search(
                origin=args.destination,
                destination=args.origin,
                date=args.inbound_date,
                cabin=args.cabin,
                trip_type="oneway",
                page=page,
                time_window=tuple(args.inbound_window),
            )
            print(f"[Agent] Inbound search: {len(inbound_result.flights)} flights found.")
        except Exception as e:
            inbound_result.error = str(e)
            print(f"[Agent] Error in inbound search: {e}")

        await browser.close()

    # 6. Compare options
    comparison = compare_options(
        return_result, outbound_result, inbound_result,
        fx_rate=fx_rate,
        fx_pair="EUR/USD",
    )

    # 7. Generate report
    report = generate_markdown_report(
        comparison, outbound_result, inbound_result, return_result, args
    )

    # 8. Save output
    output_path = Path("output/flight_comparison.md")
    output_path.parent.mkdir(exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(f"\n[Agent] Report saved to {output_path}")
    print(f"\n{'='*60}")
    print(comparison.get("recommendation", "No recommendation available."))
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Lufthansa flight price research agent"
    )
    parser.add_argument("--origin", default="FRA")
    parser.add_argument("--destination", default="JFK")
    parser.add_argument("--outbound-date", default="2026-04-10")
    parser.add_argument(
        "--outbound-window",
        nargs=2,
        default=["08:00", "12:00"],
        metavar=("START", "END"),
    )
    parser.add_argument("--inbound-date", default="2026-04-17")
    parser.add_argument(
        "--inbound-window",
        nargs=2,
        default=["16:00", "20:00"],
        metavar=("START", "END"),
    )
    parser.add_argument(
        "--cabin",
        default="Business",
        choices=["Economy", "PremiumEconomy", "Business", "First"],
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (higher bot-detection risk)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run(args))
