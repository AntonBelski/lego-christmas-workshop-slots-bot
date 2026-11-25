from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://graphql.services.appointedd.com"
WIDGET_ORIGIN = "https://booking-tools.appointedd.com"

# Public client Access-Token embedded in Appointedd's production booking-tools bundle.
# If Appointedd ever rotates it, `_refresh_access_token()` fetches a fresh one automatically.
DEFAULT_ACCESS_TOKEN = (
    "9HzWf7vzQS3pnUMF++dT2G9E8yxY+siKWy54TRyUTqaLOXiC34+78WGXbRA1DoJmcxdn5BaNUXZN6jZYHrPU2a4"
    "Zmy5sM8/zWiMZEvT5caH1kbnvZJAozG8zK494s2ttjxwh9p6XNBA9NXpwtjqG3YzrKlINg9G+LCsqvp6vXJA="
)

GET_BOOKING_TOOL_QUERY = """
query GetBookingToolData($id: ObjectID!) {
  bookingTool(id: $id) {
    _id
    name
    forceServiceSelection
    resourceSelectionMode
    services(first: 100, order: ASCENDING, sortBy: ORDER) {
      edges {
        node {
          _id
          name
          durations {
            default
            minutes
            price
          }
          organisation {
            _id
          }
          maxParticipantsPerBooking
          occupancy
          availabilityDisplayMode
        }
      }
    }
  }
}
"""

GET_AVAILABILITY_QUERY = """
query GetOrganisationAvailability(
  $id: ObjectID!
  $bookingToolId: ObjectID
  $serviceId: ObjectID!
  $ranges: [AvailabilityRange!]!
  $duration: Int!
  $spaces: Int!
  $timezone: String!
  $meta: AvailabilityRequestMetaData!
  $requestRangesIndividually: Boolean
) {
  organisation(id: $id) {
    _id
    availabilityDates(
      bookingToolId: $bookingToolId
      ranges: $ranges
      duration: $duration
      serviceId: $serviceId
      spaces: $spaces
      timezone: $timezone
      meta: $meta
      requestRangesIndividually: $requestRangesIndividually
    ) {
      date
    }
    availabilityIntervals(
      bookingToolId: $bookingToolId
      ranges: $ranges
      duration: $duration
      serviceId: $serviceId
      spaces: $spaces
      timezone: $timezone
      meta: $meta
      requestRangesIndividually: $requestRangesIndividually
    ) {
      __typename
      ... on AvailableInterval {
        start
        end
        resourceIds
        remainingSpaces
      }
      ... on GroupBookingInterval {
        start
        end
        remainingSpaces
        booking {
          _id
          start
          end
          price
          remainingSpaces
        }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class BookingService:
    service_id: str
    organisation_id: str
    name: str
    duration_minutes: int
    occupancy: Optional[int] = None
    max_participants: Optional[int] = None


@dataclass(frozen=True)
class AvailableSlot:
    slot_key: str
    service_id: str
    service_name: str
    start_utc: str
    end_utc: Optional[str]
    start_local: datetime
    end_local: Optional[datetime]
    remaining_spaces: Optional[int]
    interval_type: str
    booking_id: Optional[str] = None

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "slot_key": self.slot_key,
            "service_id": self.service_id,
            "service_name": self.service_name,
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "start_local": self.start_local.isoformat(),
            "end_local": self.end_local.isoformat() if self.end_local else None,
            "remaining_spaces": self.remaining_spaces,
            "interval_type": self.interval_type,
            "booking_id": self.booking_id,
        }


@dataclass(frozen=True)
class AvailabilityCheckResult:
    widget_id: str
    widget_name: str
    services_checked: int
    slots: list[AvailableSlot]
    available_dates_raw: list[str]
    checked_at_utc: str


def parse_iso_utc(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_utc_range(date_from: date, date_to: date, tz_name: str) -> dict[str, str]:
    tz = ZoneInfo(tz_name)
    start_local = datetime.combine(date_from, time.min, tzinfo=tz)
    # Inclusive of full `date_to` day -> next day 00:00:00 local time
    end_local = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=tz)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    return {
        "start": start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "end": end_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }


def parse_availability_response(
    *,
    payload: dict[str, Any],
    service: BookingService,
    date_from: date,
    date_to: date,
    tz_name: str,
) -> tuple[list[AvailableSlot], list[str]]:
    """
    Parse Appointedd's GraphQL `organisation.availabilityIntervals` and
    `organisation.availabilityDates` into normalized `AvailableSlot` objects.
    Handles both `AvailableInterval` and `GroupBookingInterval` types.
    """
    tz = ZoneInfo(tz_name)
    org = (payload.get("data") or {}).get("organisation") or {}
    raw_dates = [d["date"] for d in (org.get("availabilityDates") or []) if d and d.get("date")]
    raw_intervals = org.get("availabilityIntervals") or []

    slots_by_key: dict[str, AvailableSlot] = {}

    for item in raw_intervals:
        if not isinstance(item, dict):
            continue
        typename = item.get("__typename") or "AvailableInterval"
        booking = item.get("booking") if isinstance(item.get("booking"), dict) else {}

        start_str = item.get("start") or booking.get("start")
        end_str = item.get("end") or booking.get("end")
        if not start_str:
            continue

        start_dt_utc = parse_iso_utc(start_str)
        start_local = start_dt_utc.astimezone(tz)
        if not (date_from <= start_local.date() <= date_to):
            continue

        end_local: Optional[datetime] = None
        if end_str:
            end_local = parse_iso_utc(end_str).astimezone(tz)

        remaining = item.get("remainingSpaces")
        if remaining is None and booking:
            remaining = booking.get("remainingSpaces")
        if remaining is not None:
            try:
                remaining = int(remaining)
            except (TypeError, ValueError):
                remaining = None

        booking_id = booking.get("_id") if booking else None
        slot_key = f"{service.service_id}|{start_dt_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        if booking_id:
            slot_key = f"{slot_key}|{booking_id}"

        candidate = AvailableSlot(
            slot_key=slot_key,
            service_id=service.service_id,
            service_name=service.name,
            start_utc=start_dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end_utc=parse_iso_utc(end_str).strftime("%Y-%m-%dT%H:%M:%SZ") if end_str else None,
            start_local=start_local,
            end_local=end_local,
            remaining_spaces=remaining,
            interval_type=typename,
            booking_id=booking_id,
        )

        # Keep highest remaining_spaces if duplicate key appears
        existing = slots_by_key.get(slot_key)
        if existing is None or (
            candidate.remaining_spaces is not None
            and (existing.remaining_spaces is None or candidate.remaining_spaces > existing.remaining_spaces)
        ):
            slots_by_key[slot_key] = candidate

    # Fallback: if Appointedd returned available dates in range but no detailed intervals
    if not slots_by_key and raw_dates:
        for d_str in raw_dates:
            try:
                d_utc = parse_iso_utc(d_str)
                d_local = d_utc.astimezone(tz)
            except ValueError:
                continue
            if not (date_from <= d_local.date() <= date_to):
                continue
            slot_key = f"{service.service_id}|date:{d_local.date().isoformat()}"
            slots_by_key[slot_key] = AvailableSlot(
                slot_key=slot_key,
                service_id=service.service_id,
                service_name=service.name,
                start_utc=d_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end_utc=None,
                start_local=d_local,
                end_local=None,
                remaining_spaces=None,
                interval_type="AvailableDateFallback",
            )

    sorted_slots = sorted(slots_by_key.values(), key=lambda s: (s.start_local, s.slot_key))
    return sorted_slots, raw_dates


class AppointeddClient:
    def __init__(self, widget_id: str, access_token: str = DEFAULT_ACCESS_TOKEN, timeout: float = 20.0):
        self.widget_id = widget_id
        self.access_token = access_token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Access-Token": self.access_token,
            "Origin": WIDGET_ORIGIN,
            "Referer": f"{WIDGET_ORIGIN}/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }

    async def _refresh_access_token(self, client: httpx.AsyncClient) -> bool:
        """Self-heal by extracting the latest Access-Token from Appointedd's booking-tools JS bundle."""
        try:
            html_resp = await client.get(f"{WIDGET_ORIGIN}/widget/{self.widget_id}")
            html_resp.raise_for_status()
            m_js = re.search(r'src="(/main\.[^"]+\.js)"', html_resp.text)
            if not m_js:
                return False
            js_resp = await client.get(f"{WIDGET_ORIGIN}{m_js.group(1)}")
            js_resp.raise_for_status()
            m_tok = re.search(r'"production"===e\?"([A-Za-z0-9+/=]{80,})"', js_resp.text)
            if m_tok:
                self.access_token = m_tok.group(1)
                logger.info("Refreshed Appointedd public Access-Token from booking-tools bundle.")
                return True
        except Exception as exc:
            logger.warning("Failed to refresh Appointedd Access-Token: %s", exc)
        return False

    async def _graphql(
        self, client: httpx.AsyncClient, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {"query": query, "variables": variables}
        resp = await client.post(GRAPHQL_URL, json=payload, headers=self._headers())
        if resp.status_code in (401, 403):
            logger.warning("Appointedd returned HTTP %d; attempting Access-Token refresh...", resp.status_code)
            if await self._refresh_access_token(client):
                resp = await client.post(GRAPHQL_URL, json=payload, headers=self._headers())
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"Appointedd GraphQL errors: {data['errors']}")
        return data

    async def check_availability(
        self,
        *,
        date_from: date,
        date_to: date,
        tz_name: str,
        min_spaces: int = 1,
    ) -> AvailabilityCheckResult:
        utc_range = build_utc_range(date_from, date_to, tz_name)
        session_id = f"monitor-{uuid.uuid4().hex[:12]}"

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            tool_resp = await self._graphql(client, GET_BOOKING_TOOL_QUERY, {"id": self.widget_id})
            tool = (tool_resp.get("data") or {}).get("bookingTool")
            if not tool:
                raise RuntimeError(f"Booking tool {self.widget_id} not found in Appointedd response.")

            widget_name = tool.get("name") or self.widget_id
            edges = (tool.get("services") or {}).get("edges") or []
            services: list[BookingService] = []
            for edge in edges:
                node = (edge or {}).get("node") or {}
                org_id = (node.get("organisation") or {}).get("_id")
                durations = node.get("durations") or []
                if not node.get("_id") or not org_id or not durations:
                    continue
                default_dur = next((d["minutes"] for d in durations if d.get("default")), durations[0]["minutes"])
                services.append(
                    BookingService(
                        service_id=node["_id"],
                        organisation_id=org_id,
                        name=node.get("name") or "Workshop",
                        duration_minutes=int(default_dur),
                        occupancy=node.get("occupancy"),
                        max_participants=node.get("maxParticipantsPerBooking"),
                    )
                )

            if not services:
                raise RuntimeError(f"No services returned for booking tool {self.widget_id}.")

            all_slots: list[AvailableSlot] = []
            all_raw_dates: list[str] = []

            for svc in services:
                avail_resp = await self._graphql(
                    client,
                    GET_AVAILABILITY_QUERY,
                    {
                        "id": svc.organisation_id,
                        "bookingToolId": self.widget_id,
                        "serviceId": svc.service_id,
                        "ranges": [utc_range],
                        "duration": svc.duration_minutes,
                        "spaces": min_spaces,
                        "timezone": tz_name,
                        "meta": {"sessionId": session_id},
                        "requestRangesIndividually": False,
                    },
                )
                svc_slots, raw_dates = parse_availability_response(
                    payload=avail_resp,
                    service=svc,
                    date_from=date_from,
                    date_to=date_to,
                    tz_name=tz_name,
                )
                all_slots.extend(svc_slots)
                all_raw_dates.extend(raw_dates)

        all_slots.sort(key=lambda s: (s.start_local, s.slot_key))
        return AvailabilityCheckResult(
            widget_id=self.widget_id,
            widget_name=widget_name,
            services_checked=len(services),
            slots=all_slots,
            available_dates_raw=sorted(set(all_raw_dates)),
            checked_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
