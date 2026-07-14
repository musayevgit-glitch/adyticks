"""Client for the ADY ticket API (ticket.ady.az).

Working approach (verified earlier locally):
  1) curl_cffi (Safari TLS) bootstraps CF/XSRF cookies
  2) Those cookies are seeded into Playwright
  3) Playwright only runs grecaptcha.execute for g_token
  4) API call uses curl_cffi session + g_token
"""

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

# Minimal flags — keep close to the original working Mac version.
# Avoid --single-process and --disable-web-resources (break reCAPTCHA).
CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]


@dataclass(frozen=True)
class AvailableTrip:
    trip_date: str
    min_amount: str
    min_coefficient: str


class AdyApiError(Exception):
    """Raised when the ADY API returns an unexpected or fatal error."""


class AdyApiClient:
    def __init__(self) -> None:
        self.session = cffi_requests.Session(impersonate="safari17_0")

    def _bootstrap_session(self) -> None:
        """Load the search page to obtain Cloudflare + XSRF cookies."""
        response = self.session.get(
            config.ADY_SEARCH_PAGE,
            timeout=60,
            headers={"User-Agent": SAFARI_USER_AGENT},
        )
        response.raise_for_status()

        # Accept page if it looks like the real ticket app (CF can appear in HTML comments).
        if "Just a moment" in response.text and "Purchase train tickets" not in response.text:
            raise AdyApiError("Cloudflare challenge page received during session bootstrap")

        logger.info("Bootstrapped ADY session; cookies=%s", list(self.session.cookies.keys()))

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

    def obtain_g_token(self) -> str:
        """
        Execute Google reCAPTCHA v3 using Playwright with curl_cffi cookies seeded in.

        Always closes Playwright resources in finally (OOM protection on VPS).
        """
        cookie_seed = [
            {
                "name": name,
                "value": value,
                "domain": ".ady.az",
                "path": "/",
            }
            for name, value in self.session.cookies.items()
        ]

        playwright = None
        browser = None
        context = None
        page = None
        token = None

        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(
                headless=True,
                args=CHROMIUM_ARGS,
            )
            context = browser.new_context(
                user_agent=SAFARI_USER_AGENT,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
            if cookie_seed:
                context.add_cookies(cookie_seed)

            page = context.new_page()
            page.goto(
                config.ADY_SEARCH_PAGE,
                wait_until="domcontentloaded",
                timeout=120_000,
            )
            page.wait_for_function(
                "() => document.title.indexOf('Just a moment') === -1",
                timeout=90_000,
            )
            page.wait_for_function(
                "() => typeof grecaptcha !== 'undefined' && !!grecaptcha.execute",
                timeout=60_000,
            )
            time.sleep(1)

            logger.info("Requesting g_token (url=%s title=%s)", page.url, page.title())
            token = page.evaluate(
                f"""
                () => new Promise((resolve, reject) => {{
                    grecaptcha.ready(() => {{
                        grecaptcha
                            .execute('{config.RECAPTCHA_SITE_KEY}', {{ action: 'ticket_api' }})
                            .then(resolve)
                            .catch(reject);
                    }});
                }})
                """
            )
        except PlaywrightTimeoutError as exc:
            raise AdyApiError(f"Timed out while obtaining g_token: {exc}") from exc
        except Exception as exc:
            raise AdyApiError(f"Failed to obtain g_token: {exc}") from exc
        finally:
            for closer, label in (
                (page, "page"),
                (context, "context"),
                (browser, "browser"),
            ):
                if closer is None:
                    continue
                try:
                    closer.close()
                except Exception as cleanup_exc:
                    logger.debug("Error closing %s: %s", label, cleanup_exc)
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception as cleanup_exc:
                    logger.debug("Error stopping playwright: %s", cleanup_exc)

        if not token or not isinstance(token, str):
            raise AdyApiError("Received an empty g_token")

        logger.info("Obtained g_token (%d chars)", len(token))
        return token

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
        """Fetch available trip dates for the configured route."""
        self._bootstrap_session()
        g_token = self.obtain_g_token()

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
            for value in raw_data.values():
                trips.extend(AdyApiClient._extract_trips_from_group(value))
        elif isinstance(raw_data, list):
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
