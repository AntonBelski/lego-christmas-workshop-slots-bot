from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_ids: tuple[str, ...]
    widget_id: str
    booking_url: str
    date_from: date
    date_to: date
    timezone: str
    min_spaces: int
    check_interval_minutes: int
    state_path: Path
    run_log_path: Path


def load_settings() -> Settings:
    raw_chat_ids = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    chat_ids = tuple(c.strip() for c in raw_chat_ids.split(",") if c.strip())

    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_ids=chat_ids,
        widget_id=os.getenv("APPOINTEDD_WIDGET_ID", "6af497ce58a51703b9fb8202").strip(),
        booking_url=os.getenv(
            "APPOINTEDD_BOOKING_URL",
            "https://swiateczna-pracownia.appointedd.com/booking-widget/6af497ce58a51703b9fb8202",
        ).strip(),
        date_from=date.fromisoformat(os.getenv("DATE_FROM", "2026-12-04").strip()),
        date_to=date.fromisoformat(os.getenv("DATE_TO", "2026-12-06").strip()),
        timezone=os.getenv("TIMEZONE", "Europe/Warsaw").strip(),
        min_spaces=max(1, int(os.getenv("MIN_SPACES", "1"))),
        check_interval_minutes=max(1, int(os.getenv("CHECK_INTERVAL_MINUTES", "1"))),
        state_path=Path(os.getenv("STATE_PATH", "data/slots_state.json")),
        run_log_path=Path(os.getenv("RUN_LOG_PATH", "data/monitor_runs.log")),
    )
