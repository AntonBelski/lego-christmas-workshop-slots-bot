from __future__ import annotations

from datetime import date

from monitor.appointedd import (
    BookingService,
    build_utc_range,
    parse_availability_response,
)
from monitor.notifier import format_alert_message
from monitor.state import build_updated_active_slots, diff_slots

SAMPLE_SERVICE = BookingService(
    service_id="6af491d258a51703b9fb80b0",
    organisation_id="6af491d258a51703b9fb80ae",
    name="Świąteczna Pracownia LEGO®",
    duration_minutes=60,
    occupancy=60,
    max_participants=5,
)


def test_build_utc_range_warsaw_winter_time() -> None:
    r = build_utc_range(date(2026, 12, 4), date(2026, 12, 6), "Europe/Warsaw")
    # CET in December is UTC+1
    assert r == {
        "start": "2026-12-03T23:00:00.000Z",
        "end": "2026-12-06T23:00:00.000Z",
    }


def test_parse_group_and_available_intervals() -> None:
    payload = {
        "data": {
            "organisation": {
                "_id": "6af491d258a51703b9fb80ae",
                "availabilityDates": [{"date": "2026-12-05T00:00:00.000Z"}],
                "availabilityIntervals": [
                    {
                        "__typename": "GroupBookingInterval",
                        "start": "2026-12-05T09:00:00.000Z",
                        "end": "2026-12-05T10:00:00.000Z",
                        "remainingSpaces": 4,
                        "booking": {
                            "_id": "bk_123",
                            "start": "2026-12-05T09:00:00.000Z",
                            "end": "2026-12-05T10:00:00.000Z",
                            "price": 0,
                            "remainingSpaces": 4,
                        },
                    },
                    {
                        "__typename": "AvailableInterval",
                        "start": "2026-12-04T14:00:00.000Z",
                        "end": "2026-12-04T15:00:00.000Z",
                        "resourceIds": ["6af491d258a51703b9fb80b1"],
                        "remainingSpaces": 2,
                    },
                    # Outside Dec 4-6 -> should be filtered out
                    {
                        "__typename": "AvailableInterval",
                        "start": "2026-12-07T10:00:00.000Z",
                        "end": "2026-12-07T11:00:00.000Z",
                        "resourceIds": ["6af491d258a51703b9fb80b1"],
                        "remainingSpaces": 10,
                    },
                ],
            }
        }
    }

    slots, raw_dates = parse_availability_response(
        payload=payload,
        service=SAMPLE_SERVICE,
        date_from=date(2026, 12, 4),
        date_to=date(2026, 12, 6),
        tz_name="Europe/Warsaw",
    )
    assert raw_dates == ["2026-12-05T00:00:00.000Z"]
    assert len(slots) == 2

    # Sorted chronologically: Dec 4 15:00-16:00 CET, then Dec 5 10:00-11:00 CET
    assert slots[0].start_local.strftime("%Y-%m-%d %H:%M") == "2026-12-04 15:00"
    assert slots[0].end_local.strftime("%H:%M") == "16:00"
    assert slots[0].remaining_spaces == 2
    assert slots[0].interval_type == "AvailableInterval"

    assert slots[1].start_local.strftime("%Y-%m-%d %H:%M") == "2026-12-05 10:00"
    assert slots[1].end_local.strftime("%H:%M") == "11:00"
    assert slots[1].remaining_spaces == 4
    assert slots[1].interval_type == "GroupBookingInterval"
    assert slots[1].booking_id == "bk_123"


def test_parse_dates_fallback_when_intervals_empty() -> None:
    payload = {
        "data": {
            "organisation": {
                "_id": "6af491d258a51703b9fb80ae",
                "availabilityDates": [{"date": "2026-12-06T00:00:00.000Z"}],
                "availabilityIntervals": [],
            }
        }
    }
    slots, _ = parse_availability_response(
        payload=payload,
        service=SAMPLE_SERVICE,
        date_from=date(2026, 12, 4),
        date_to=date(2026, 12, 6),
        tz_name="Europe/Warsaw",
    )
    assert len(slots) == 1
    assert slots[0].interval_type == "AvailableDateFallback"
    assert slots[0].start_local.date() == date(2026, 12, 6)


def test_diff_slots_new_unchanged_increased_and_reopened() -> None:
    payload_v1 = {
        "data": {
            "organisation": {
                "availabilityDates": [],
                "availabilityIntervals": [
                    {
                        "__typename": "GroupBookingInterval",
                        "start": "2026-12-05T09:00:00.000Z",
                        "end": "2026-12-05T10:00:00.000Z",
                        "remainingSpaces": 2,
                        "booking": {"_id": "bk_1", "remainingSpaces": 2},
                    }
                ],
            }
        }
    }
    slots_v1, _ = parse_availability_response(
        payload=payload_v1,
        service=SAMPLE_SERVICE,
        date_from=date(2026, 12, 4),
        date_to=date(2026, 12, 6),
        tz_name="Europe/Warsaw",
    )

    # 1. First appearance -> alerted as new
    d1 = diff_slots({}, slots_v1)
    assert d1.has_alerts
    assert len(d1.new_slots) == 1
    assert len(d1.increased_space_slots) == 0

    state_v1 = build_updated_active_slots(slots_v1)

    # 2. Unchanged -> no alert
    d2 = diff_slots(state_v1, slots_v1)
    assert not d2.has_alerts

    # 3. Spaces increased from 2 -> 5 -> alerted as increased
    payload_v3 = {
        "data": {
            "organisation": {
                "availabilityDates": [],
                "availabilityIntervals": [
                    {
                        "__typename": "GroupBookingInterval",
                        "start": "2026-12-05T09:00:00.000Z",
                        "end": "2026-12-05T10:00:00.000Z",
                        "remainingSpaces": 5,
                        "booking": {"_id": "bk_1", "remainingSpaces": 5},
                    }
                ],
            }
        }
    }
    slots_v3, _ = parse_availability_response(
        payload=payload_v3,
        service=SAMPLE_SERVICE,
        date_from=date(2026, 12, 4),
        date_to=date(2026, 12, 6),
        tz_name="Europe/Warsaw",
    )
    d3 = diff_slots(state_v1, slots_v3)
    assert d3.has_alerts
    assert len(d3.new_slots) == 0
    assert len(d3.increased_space_slots) == 1
    assert d3.increased_space_slots[0][1] == 2

    # 4. Booked out (empty), then reopened -> alerted as new again
    state_empty = build_updated_active_slots([])
    d4 = diff_slots(state_empty, slots_v1)
    assert d4.has_alerts
    assert len(d4.new_slots) == 1

    # 5. Message formatting sanity check
    msg = format_alert_message(
        widget_name="LEGO Poland Christmas Workshop",
        booking_url="https://swiateczna-pracownia.appointedd.com/booking-widget/6af497ce58a51703b9fb8202",
        diff=d4,
        all_current_slots=slots_v1,
    )
    assert "NEW SLOTS AVAILABLE" in msg
    assert "05.12.2026" in msg
    assert "10:00–11:00" in msg
    assert "2 spots" in msg
