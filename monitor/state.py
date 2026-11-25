from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from monitor.appointedd import AvailableSlot


@dataclass(frozen=True)
class SlotDiff:
    new_slots: list[AvailableSlot]
    increased_space_slots: list[tuple[AvailableSlot, int]]  # (slot, previous_spaces)

    @property
    def has_alerts(self) -> bool:
        return bool(self.new_slots or self.increased_space_slots)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"active_slots": {}, "total_checks": 0, "total_alerts_sent": 0, "last_checked_utc": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("State JSON root must be an object")
        data.setdefault("active_slots", {})
        data.setdefault("total_checks", 0)
        data.setdefault("total_alerts_sent", 0)
        return data
    except Exception:
        return {"active_slots": {}, "total_checks": 0, "total_alerts_sent": 0, "last_checked_utc": None}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def diff_slots(previous_active: dict[str, dict[str, Any]], current_slots: list[AvailableSlot]) -> SlotDiff:
    """
    Compare currently available slots against previously seen active slots.
    - Any slot not in `previous_active` is a brand-new (or newly reopened) slot.
    - Any slot already in `previous_active` whose `remaining_spaces` strictly increased
      also triggers an alert (e.g., someone else cancelled additional spots in that hour).
    """
    new_slots: list[AvailableSlot] = []
    increased: list[tuple[AvailableSlot, int]] = []

    for slot in current_slots:
        prev = previous_active.get(slot.slot_key)
        if prev is None:
            new_slots.append(slot)
            continue
        prev_spaces = prev.get("remaining_spaces")
        if (
            slot.remaining_spaces is not None
            and isinstance(prev_spaces, int)
            and slot.remaining_spaces > prev_spaces
        ):
            increased.append((slot, prev_spaces))

    return SlotDiff(new_slots=new_slots, increased_space_slots=increased)


def build_updated_active_slots(current_slots: list[AvailableSlot]) -> dict[str, dict[str, Any]]:
    return {slot.slot_key: slot.to_state_dict() for slot in current_slots}
