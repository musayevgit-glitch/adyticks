"""Persist notified trip dates to avoid duplicate Telegram alerts."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Set

import config

logger = logging.getLogger(__name__)


class NotifiedDatesStorage:
    """Tracks which trip dates have already triggered a notification."""

    def __init__(self, file_path: str | Path | None = None) -> None:
        self.file_path = Path(file_path or config.NOTIFIED_DATES_FILE)

    def _load(self) -> Set[str]:
        if not self.file_path.exists():
            return set()

        try:
            with self.file_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read %s: %s", self.file_path, exc)
            return set()

        dates = payload.get("notified_dates", [])
        if not isinstance(dates, list):
            logger.warning("Invalid notified_dates format in %s", self.file_path)
            return set()

        return {str(date) for date in dates}

    def _save(self, dates: Set[str]) -> None:
        payload = {"notified_dates": sorted(dates)}
        with self.file_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)

    def get_notified_dates(self) -> Set[str]:
        return self._load()

    def mark_notified(self, dates: Set[str]) -> None:
        if not dates:
            return

        current = self._load()
        current.update(dates)
        self._save(current)
        logger.debug("Persisted notified dates: %s", sorted(dates))

    def filter_new_dates(self, dates: Set[str]) -> Set[str]:
        already_notified = self._load()
        return {date for date in dates if date not in already_notified}
