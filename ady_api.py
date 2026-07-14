"""Client for the ADY ticket API (ticket.ady.az)."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

import requests
from curl_cffi import requests as cffi_requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

import config

logger = logging.getLogger(__name__)

SAFARI_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# Do NOT use --single-process or --disable-web-resources — they break reCAPTCHA.
CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--mute-audio",
    "--no-first-run",
    "--disable-default-apps",
    "--js-flags=--max-old-space-size=192",
]


@dataclass(frozen=True)
class AvailableTrip:
    trip_date: str
    min_amount: str
    min_coefficient: str


class AdyApiError(Exception):
    """Raised when the ADY API returns an unexpected or fatal error."""


class AdyApiClient:
    """
    Talks to ADY using curl_cffi for HTTP (Cloudflare-safe) and Playwright /
    optional CAPTCHA API for reCAPTCHA v3 g_token.
    """

    def __init__(self) -> None:
        self.session = cffi_requests.Session(impersonate="safari17_0")

    def _bootstrap_session(self) -> None:
        """Load the search page with curl_cffi to obtain CF/XSRF cookies."""
        response = self.session.get(
            config.ADY_SEARCH_PAGE,
            timeout=60,
            headers={"User-Agent": SAFARI_USER_AGENT},
        )
        response.raise_for_status()
        if "Just a moment" in response.text and "Purchase train tickets" not in response.text:
            raise AdyApiError("Cloudflare challenge page received during session bootstrap")
        logger.debug("Session cookies: %s", list(self.session.cookies.keys()))

    def _transfer_browser_cookies_to_session(self, browser_cookies: list) -> None:
        for cookie_data in browser_cookies:
            self.session.cookies.set(
                cookie_data["name"],
                cookie_data["value"],
                domain=cookie_data.get("domain", ""),
                path=cookie_data.get("path", "/"),
            )
        logger.debug(
            "Transferred cookies from browser: %s",
            [c["name"] for c in browser_cookies],
        )

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

    def _solve_g_token_via_2captcha(self) -> str:
        """Solve reCAPTCHA v3 via 2Captcha when CAPTCHA_API_KEY is set."""
        api_key = os.getenv("CAPTCHA_API_KEY", "").strip()
        if not api_key:
            raise AdyApiError("CAPTCHA_API_KEY is not set")

        create = requests.post(
            "https://api.2captcha.com/createTask",
            json={
                "clientKey": api_key,
                "task": {
                    "type": "RecaptchaV3TaskProxyless",
                    "websiteURL": config.ADY_SEARCH_PAGE,
                    "websiteKey": config.RECAPTCHA_SITE_KEY,
                    "minScore": 0.3,
                    "pageAction": "ticket_api",
                    "isEnterprise": False,
                },
            },
            timeout=60,
        )
        create.raise_for_status()
        created = create.json()
        if created.get("errorId"):
            raise AdyApiError(f"2Captcha createTask error: {created}")

        task_id = created["taskId"]
        logger.info("2Captcha task created: %s", task_id)

        for _ in range(40):
            time.sleep(5)
            poll = requests.post(
                "https://api.2captcha.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=60,
            )
            poll.raise_for_status()
            result = poll.json()
            if result.get("errorId"):
                raise AdyApiError(f"2Captcha getTaskResult error: {result}")
            if result.get("status") == "ready":
                token = result["solution"]["gRecaptchaResponse"]
                logger.info("2Captcha solved g_token (%d chars)", len(token))
                return token
            logger.debug("2Captcha still processing...")

        raise AdyApiError("2Captcha timed out waiting for solution")

    def _wait_for_search_page(self, page) -> None:
        page.wait_for_function(
            "() => document.title.indexOf('Just a moment') === -1",
            timeout=120_000,
        )
        time.sleep(2)
        page.wait_for_function(
            """
            () => typeof grecaptcha !== 'undefined'
              && typeof grecaptcha.execute === 'function'
              && typeof grecaptcha.ready === 'function'
            """,
            timeout=90_000,
        )
        # Wait until reCAPTCHA internal client registry is populated.
        try:
            page.wait_for_function(
                """
                () => !!(window.___grecaptcha_cfg
                  && window.___grecaptcha_cfg.clients
                  && Object.keys(window.___grecaptcha_cfg.clients).length > 0)
                """,
                timeout=30_000,
            )
        except PlaywrightTimeoutError:
            logger.warning("___grecaptcha_cfg clients not ready; continuing anyway")
        time.sleep(2)

    def _extract_g_token_from_page(self, page) -> tuple[str, list]:
        logger.info("Requesting g_token (url=%s title=%s)", page.url, page.title())
        token = page.evaluate(
            f"""
            () => new Promise((resolve, reject) => {{
                const timer = setTimeout(
                    () => reject(new Error('grecaptcha.execute timeout')),
                    45000
                );
                try {{
                    grecaptcha.ready(() => {{
                        grecaptcha
                            .execute('{config.RECAPTCHA_SITE_KEY}', {{ action: 'ticket_api' }})
                            .then(t => {{
                                clearTimeout(timer);
                                resolve(t);
                            }})
                            .catch(err => {{
                                clearTimeout(timer);
                                reject(err);
                            }});
                    }});
                }} catch (err) {{
                    clearTimeout(timer);
                    reject(err);
                }}
            }})
            """
        )
        if not token or not isinstance(token, str):
            raise AdyApiError("Received an empty g_token from reCAPTCHA")
        cookies = page.context.cookies()
        logger.info("Obtained g_token (%d chars), cookies=%d", len(token), len(cookies))
        return token, cookies

    def _launch_browser(self, playwright, browser_name: str, headless: bool):
        if browser_name == "firefox":
            return playwright.firefox.launch(headless=headless)
        return playwright.chromium.launch(
            headless=headless,
            args=CHROMIUM_ARGS,
            ignore_default_args=["--enable-automation"],
        )

    def _obtain_g_token_with_browser(self, browser_name: str, headless: bool) -> tuple[str, list]:
        playwright = None
        browser = None
        context = None
        page = None
        try:
            logger.info(
                "Launching %s (headless=%s) for g_token",
                browser_name,
                headless,
            )
            playwright = sync_playwright().start()
            browser = self._launch_browser(playwright, browser_name, headless)
            context = browser.new_context(
                user_agent=SAFARI_USER_AGENT,
                viewport={"width": 1280, "height": 720},
                locale="en-US",
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )
            page = context.new_page()
            page.goto(
                config.ADY_SEARCH_PAGE,
                wait_until="domcontentloaded",
                timeout=120_000,
            )
            try:
                page.wait_for_load_state("load", timeout=30_000)
            except PlaywrightTimeoutError:
                logger.debug("load state timeout ignored")
            self._wait_for_search_page(page)
            return self._extract_g_token_from_page(page)
        finally:
            for closer, label in ((page, "page"), (context, "context"), (browser, "browser")):
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

    def obtain_g_token(self) -> tuple[str, list]:
        """
        Return (g_token, cookies).

        Order:
          1) CAPTCHA_API_KEY → 2Captcha (cookies=[])
          2) Playwright chromium/firefox (HEADLESS=0 recommended with xvfb-run)
        """
        if os.getenv("CAPTCHA_API_KEY", "").strip():
            token = self._solve_g_token_via_2captcha()
            return token, []

        headless = os.getenv("HEADLESS", "1") != "0"
        browsers = [
            b.strip()
            for b in os.getenv("ADY_BROWSER", "chromium,firefox").split(",")
            if b.strip()
        ]
        last_error: Optional[Exception] = None

        for browser_name in browsers:
            try:
                return self._obtain_g_token_with_browser(browser_name, headless)
            except Exception as exc:
                last_error = exc
                logger.warning("%s g_token failed: %s", browser_name, exc)

        raise AdyApiError(f"Failed to obtain g_token: {last_error}")

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
        g_token, browser_cookies = self.obtain_g_token()
        if browser_cookies:
            self._transfer_browser_cookies_to_session(browser_cookies)
        else:
            self._bootstrap_session()

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
