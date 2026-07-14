"""ADY ticket availability monitor for Tbilisi → Baku."""

from __future__ import annotations

import argparse
import gc
import logging
import sys
import time
from datetime import datetime

import schedule

import config
from ady_api import AdyApiClient, AdyApiError, AvailableTrip
from storage import NotifiedDatesStorage
from telegram_bot import TelegramNotifier, TripDateInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M",
)
logger = logging.getLogger(__name__)


def validate_config() -> None:
    if not config.BOT_TOKEN:
        raise ValueError("BOT_TOKEN is empty. Set it in config.py or as an env var")
    if not config.CHAT_ID:
        raise ValueError("CHAT_ID is empty. Set it in config.py or as an env var")


def log_check_header() -> None:
    logger.info("Checking ADY...")


def log_no_tickets() -> None:
    logger.info("No tickets.")


def log_found_tickets(trips: list[AvailableTrip]) -> None:
    for trip in trips:
        logger.info("Ticket found:\n%s", trip.trip_date)


def is_target_trip(trip: AvailableTrip) -> bool:
    """Return True when trip's minimum price is strictly less than 115 AZN."""
    try:
        min_amount = float(trip.min_amount)
    except (TypeError, ValueError):
        return False
    return min_amount < 115.0


def check_tickets(
    api_client: AdyApiClient,
    storage: NotifiedDatesStorage,
    notifier: TelegramNotifier,
) -> None:
    log_check_header()
    checked_at = datetime.now()

    try:
        available_trips = api_client.get_trip_dates()
    except AdyApiError as exc:
        logger.error("ADY check failed: %s", exc)
        return
    except Exception:
        logger.exception("Unexpected error during ADY check")
        return

    target_trips = [trip for trip in available_trips if is_target_trip(trip)]
    if not target_trips:
        log_no_tickets()
        return

    log_found_tickets(target_trips)

    target_dates = {trip.trip_date for trip in target_trips}
    new_dates = storage.filter_new_dates(target_dates)
    if not new_dates:
        logger.info("Tickets exist, but all dates were already notified.")
        return

    new_trips = [
        TripDateInfo(trip_date=trip.trip_date, min_amount=trip.min_amount)
        for trip in target_trips
        if trip.trip_date in new_dates
    ]

    try:
        notifier.notify_new_trips(new_trips, checked_at=checked_at)
    except Exception:
        logger.exception("Failed to send Telegram notification")
        return

    storage.mark_notified(new_dates)
    logger.info("Notified for new dates: %s", ", ".join(sorted(new_dates)))


def run_once() -> None:
    validate_config()
    api_client = AdyApiClient()
    storage = NotifiedDatesStorage()
    notifier = TelegramNotifier()
    try:
        check_tickets(api_client, storage, notifier)
    finally:
        gc.collect()


def run_forever() -> None:
    validate_config()
    logger.info(
        "Starting ADY monitor: %s → %s (every %s seconds)",
        config.FROM_STATION_NAME,
        config.TO_STATION_NAME,
        config.CHECK_INTERVAL,
    )

    def scheduled_check() -> None:
        api_client = AdyApiClient()
        storage = NotifiedDatesStorage()
        notifier = TelegramNotifier()
        try:
            check_tickets(api_client, storage, notifier)
        finally:
            gc.collect()

    scheduled_check()
    schedule.every(config.CHECK_INTERVAL).seconds.do(scheduled_check)

    while True:
        try:
            schedule.run_pending()
        except Exception:
            logger.exception("Scheduler loop error")
        time.sleep(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor ADY tickets for Tbilisi → Baku")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one check and exit (for cron)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        if args.once:
            run_once()
        else:
            run_forever()
    except KeyboardInterrupt:
        logger.info("Stopped by user")
        sys.exit(0)
