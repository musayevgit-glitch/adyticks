"""Client for the ADY ticket API (ticket.ady.az)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List
from urllib.parse import unquote

from curl_cffi import requests as cffi_requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

import config

logger = logging.getLogger(__name__)

SAFARI_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


@dataclass(frozen=True)
class AvailableTrip:
    trip_date: str
    min_amount: str
    min_coefficient: str


class AdyApiError(Exception):
    """Raised when the ADY API returns an unexpected or fatal error."""


class AdyApiClient:
    """
    Talks to ADY using curl_cffi for HTTP (Cloudflare-safe) and Playwright only
    to execute reCAPTCHA v3, which cannot be obtained with plain requests.
    """

    def __init__(self) -> None:
        self.session = cffi_requests.Session(impersonate="safari17_0")

    def _transfer_browser_cookies_to_session(self, browser_cookies: list) -> None:
        """Transfer cookies from Playwright browser to curl_cffi session."""
        for cookie_data in browser_cookies:
            self.session.cookies.set(
                cookie_data["name"],
                cookie_data["value"],
                domain=cookie_data.get("domain", ""),
                path=cookie_data.get("path", "/"),
            )
        logger.debug("Transferred cookies from browser to session: %s", [c["name"] for c in browser_cookies])

    def _build_api_headers(self) -> Dict[str, str]:
        xsrf_token = self.session.cookies.get("XSRF-TOKEN")
        if not xsrf_token:
            raise AdyApiError("Missing XSRF-TOKEN cookie")

        return {
            "User-Agent": SAFARI_USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": config.ADY_BASE_URL,
            "Referer": config.ADY_SEARCH_PAGE,
            "X-XSRF-TOKEN": unquote(xsrf_token),
        }

    def _extract_g_token_from_page(self, page) -> tuple[str, list]:
        """Extract g_token and page cookies from a ready page. Returns (token, cookies)."""
        # Allow reCAPTCHA and any late redirects to settle.
        time.sleep(10)

        # Store the token in the window object to ensure it survives context destruction
        token = page.evaluate(
            f"""
            () => new Promise((resolve, reject) => {{
                grecaptcha.ready(() => {{
                    grecaptcha
                        .execute('{config.RECAPTCHA_SITE_KEY}', {{ action: 'ticket_api' }})
                        .then(t => {{
                            window._g_token = t;
                            resolve(t);
                        }})
                        .catch(reject);
                }});
            }})
            """
        )
        
        # Verify token was captured
        if token:
            verify_token = page.evaluate("() => window._g_token")
            if verify_token != token:
                logger.warning("Token mismatch detected, using window value")
                token = verify_token
        
        if not token or not isinstance(token, str):
            raise AdyApiError("Received an empty g_token from reCAPTCHA")
        
        # Get cookies from the page context
        cookies = page.context.cookies()
        logger.debug("Extracted g_token (%d chars) and %d cookies", len(token), len(cookies))
        return token, cookies

    def obtain_g_token(self) -> tuple[str, list]:
        """
        Execute Google reCAPTCHA v3 in a headless browser context with retry logic.

        Returns (g_token, cookies) where cookies are from the browser context.
        If extraction fails, the page is hard-refreshed and reattempted once.
        """
        max_retries = 2
        for attempt in range(max_retries):
            token = None
            cookies = []
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(
                        headless=True,
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--disable-web-resources",
                        ],
                    )
                    context = browser.new_context(
                        user_agent=SAFARI_USER_AGENT,
                        viewport={"width": 1440, "height": 900},
                        locale="en-US",
                    )

                    page = context.new_page()
                    page.on("close", lambda: logger.debug("Page closed"))
                    
                    page.goto(
                        config.ADY_SEARCH_PAGE,
                        wait_until="domcontentloaded",
                        timeout=120_000,
                    )
                    page.wait_for_load_state("networkidle", timeout=90_000)
                    page.wait_for_function(
                        "() => document.title.indexOf('Just a moment') === -1",
                        timeout=90_000,
                    )
                    page.wait_for_function(
                        "() => typeof grecaptcha !== 'undefined' && !!grecaptcha.execute",
                        timeout=60_000,
                    )
                    
                    # Hard refresh on retry
                    if attempt > 0:
                        logger.info("Hard-refreshing page after g_token extraction failure (attempt %d)", attempt + 1)
                        page.keyboard.press("Control+Shift+R")
                        page.wait_for_load_state("networkidle", timeout=90_000)
                        page.wait_for_function(
                            "() => document.title.indexOf('Just a moment') === -1",
                            timeout=90_000,
                        )
                        page.wait_for_function(
                            "() => typeof grecaptcha !== 'undefined' && !!grecaptcha.execute",
                            timeout=60_000,
                        )
                    
                    token, cookies = self._extract_g_token_from_page(page)
                    
                    page.close()
                    context.close()
                    browser.close()
                    
                    return token, cookies
            except (PlaywrightTimeoutError, AdyApiError) as exc:
                if attempt < max_retries - 1:
                    logger.warning("g_token extraction failed on attempt %d, will retry: %s", attempt + 1, exc)
                else:
                    if isinstance(exc, PlaywrightTimeoutError):
                        raise AdyApiError(f"Timed out while obtaining g_token: {exc}") from exc
                    else:
                        raise exc
            except Exception as exc:
                if attempt < max_retries - 1:
                    logger.warning("g_token extraction failed on attempt %d, will retry: %s", attempt + 1, exc)
                else:
                    raise AdyApiError(f"Failed to obtain g_token: {exc}") from exc

        raise AdyApiError("Failed to obtain g_token after all retries")

    def _trip_dates_payload(self, g_token: str) -> Dict[str, Any]:
        return {
            "from_station": config.FROM_STATION_ID,
            "to_station": config.TO_STATION_ID,
            "way": 0,
            "two_way": False,
            "is_exclusive": 0,
            "g_token": g_token,
            "action": "ticket_api",
        }

    def get_trip_dates(self) -> List[AvailableTrip]:
        """
        Fetch available trip dates for the configured route.

        Returns an empty list when no tickets are available.
        """
        # Get g_token from browser and use its cookies to initialize session
        g_token, browser_cookies = self.obtain_g_token()
        self._transfer_browser_cookies_to_session(browser_cookies)

        response = self.session.post(
            config.ADY_TRIP_DATES_URL,
            json=self._trip_dates_payload(g_token),
            headers=self._build_api_headers(),
            timeout=60,
        )
        response.raise_for_status()

        if "Just a moment" in response.text:
            raise AdyApiError("Cloudflare challenge page received on trip dates request")

        try:
            payload = response.json()
        except ValueError as exc:
            raise AdyApiError("ADY API returned non-JSON response") from exc

        return self._parse_trip_dates_response(payload)

    @staticmethod
    def _parse_trip_dates_response(payload: Dict[str, Any]) -> List[AvailableTrip]:
        if payload.get("error"):
            data = payload.get("data")
            if isinstance(data, dict) and data.get("data") == "No data":
                return []
            message = payload.get("message") or data
            raise AdyApiError(f"ADY API error: {message}")

        raw_data = payload.get("data")
        if not raw_data:
            return []

        trips: List[AvailableTrip] = []

        if isinstance(raw_data, dict):
            # Example: {"1": [{"trip_date": "...", ...}]}
            for value in raw_data.values():
                trips.extend(AdyApiClient._extract_trips_from_group(value))
        elif isinstance(raw_data, list):
            # Example: [[{"trip_date": "...", ...}]]
            for group in raw_data:
                trips.extend(AdyApiClient._extract_trips_from_group(group))
        else:
            raise AdyApiError(f"Unexpected data format: {type(raw_data).__name__}")

        return trips

    @staticmethod
    def _extract_trips_from_group(group: Any) -> List[AvailableTrip]:
        if not isinstance(group, list):
            return []

        trips: List[AvailableTrip] = []
        for item in group:
            if not isinstance(item, dict):
                continue
            trip_date = item.get("trip_date")
            min_amount = item.get("min_amount")
            if not trip_date or min_amount is None:
                continue

            trips.append(
                AvailableTrip(
                    trip_date=str(trip_date),
                    min_amount=str(min_amount),
                    min_coefficient=str(item.get("min_cofficient", "")),
                )
            )
        return trips
