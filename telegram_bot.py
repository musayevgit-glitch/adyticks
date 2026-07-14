"""Send Telegram notifications for newly available ADY tickets."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import requests

import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TripDateInfo:
    trip_date: str
    min_amount: str


class TelegramNotifier:
    """Thin wrapper around the Telegram Bot HTTP API."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.bot_token = bot_token or config.BOT_TOKEN
        self.chat_id = chat_id or config.CHAT_ID
        self.timeout = timeout

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    def send_message(self, text: str) -> None:
        if not self.bot_token or not self.chat_id:
            raise ValueError("BOT_TOKEN and CHAT_ID must be configured (env var or config)")

        response = requests.post(
            self._api_url("sendMessage"),
            json={
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()

        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")

        logger.info("Telegram notification sent")

    @staticmethod
    def _format_checked_at(checked_at: datetime | None = None) -> str:
        moment = checked_at or datetime.now()
        return moment.strftime("%Y-%m-%d %H:%M:%S")

    def build_ticket_message(
        self,
        trips: Iterable[TripDateInfo],
        checked_at: datetime | None = None,
    ) -> str:
        trip_list = list(trips)
        if not trip_list:
            raise ValueError("At least one trip date is required")

        dates_block = "\n".join(trip.trip_date for trip in trip_list)
        min_price = min(float(trip.min_amount) for trip in trip_list)

        return (
            "Bilet var brat\n\n"
            "Route:\n"
            f"{config.FROM_STATION_NAME} → {config.TO_STATION_NAME}\n\n"
            "Available dates:\n"
            f"{dates_block}\n\n"
            "Minimum price:\n"
            f"{min_price:.2f} AZN\n\n"
            "Open ADY booking page:\n"
            f"{config.ADY_LOGIN_PAGE}\n\n"
            "Time checked:\n"
            f"{self._format_checked_at(checked_at)}"
        )

    def notify_new_trips(
        self,
        trips: Iterable[TripDateInfo],
        checked_at: datetime | None = None,
    ) -> None:
        message = self.build_ticket_message(trips, checked_at=checked_at)
        self.send_message(message)
