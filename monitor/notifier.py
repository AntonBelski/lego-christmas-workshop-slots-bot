from __future__ import annotations

import html
import logging
from collections import defaultdict
from datetime import datetime
from typing import Sequence

import httpx

from monitor.appointedd import AvailableSlot
from monitor.state import SlotDiff

logger = logging.getLogger(__name__)

DAY_NAMES_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _format_slot_line(slot: AvailableSlot, prev_spaces: int | None = None) -> str:
    if slot.interval_type == "AvailableDateFallback":
        time_part = "Date available (check widget for times)"
    elif slot.end_local:
        time_part = f"<b>{slot.start_local.strftime('%H:%M')}–{slot.end_local.strftime('%H:%M')}</b>"
    else:
        time_part = f"<b>{slot.start_local.strftime('%H:%M')}</b>"

    if prev_spaces is not None and slot.remaining_spaces is not None:
        spots = f" ({prev_spaces} → <b>{slot.remaining_spaces} spots</b>)"
    elif slot.remaining_spaces is not None:
        unit = "spot" if slot.remaining_spaces == 1 else "spots"
        spots = f" — <b>{slot.remaining_spaces} {unit}</b>"
    else:
        spots = ""

    return f"• {time_part}{spots}"


def format_alert_message(
    *,
    widget_name: str,
    booking_url: str,
    diff: SlotDiff,
    all_current_slots: Sequence[AvailableSlot],
) -> str:
    lines: list[str] = [
        f"🎄 <b>NEW SLOTS AVAILABLE!</b>",
        f"<b>{html.escape(widget_name)}</b>",
        "",
    ]

    if diff.new_slots:
        by_date: dict[str, list[AvailableSlot]] = defaultdict(list)
        for slot in diff.new_slots:
            by_date[slot.start_local.strftime("%Y-%m-%d")].append(slot)

        for d_str in sorted(by_date.keys()):
            sample_dt: datetime = by_date[d_str][0].start_local
            dow = DAY_NAMES_EN[sample_dt.weekday()]
            lines.append(f"📅 <b>{dow}, {sample_dt.strftime('%d.%m.%Y')}</b>")
            for slot in by_date[d_str]:
                lines.append(_format_slot_line(slot))
            lines.append("")

    if diff.increased_space_slots:
        lines.append("📈 <b>More spots opened on existing slots:</b>")
        for slot, prev_spaces in diff.increased_space_slots:
            dow = DAY_NAMES_EN[slot.start_local.weekday()]
            date_prefix = f"{dow} {slot.start_local.strftime('%d.%m')} "
            lines.append(f"• {date_prefix}{_format_slot_line(slot, prev_spaces=prev_spaces).lstrip('• ')}")
        lines.append("")

    lines.append(f"Total open slots in window: <b>{len(all_current_slots)}</b>")
    lines.append(f'👉 <a href="{html.escape(booking_url, quote=True)}">Book now on Appointedd</a>')
    return "\n".join(lines).strip()


async def send_telegram_message(
    *,
    bot_token: str,
    chat_ids: Sequence[str],
    text: str,
    booking_url: str | None = None,
) -> int:
    """
    Send HTML Telegram notifications via direct Bot API `sendMessage`.
    Push-only (never calls `getUpdates`), so it will never conflict with other bots.
    """
    if not bot_token or bot_token == "your_telegram_bot_token_here":
        logger.warning("TELEGRAM_BOT_TOKEN is not configured; skipping Telegram notification.")
        return 0
    if not chat_ids:
        logger.warning("TELEGRAM_CHAT_ID is not configured; skipping Telegram notification.")
        return 0

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    reply_markup = (
        {
            "inline_keyboard": [
                [{"text": "🎟️ Open Booking Page", "url": booking_url}],
            ]
        }
        if booking_url
        else None
    )

    sent = 0
    async with httpx.AsyncClient(timeout=15.0) as client:
        for chat_id in chat_ids:
            payload: dict = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.error(
                    "Telegram sendMessage failed for chat_id=%s (HTTP %d): %s",
                    chat_id,
                    resp.status_code,
                    resp.text[:300],
                )
            else:
                sent += 1
    return sent
