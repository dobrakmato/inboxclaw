import asyncio
import logging
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import BaseModel

from src.config import GoogleCalendarSourceConfig, parse_interval
from src.schemas import NewEvent
from src.services import AppServices
from src.utils.google_auth import get_google_credentials
from src.utils.diff import DictDiff
from src.utils.filtering import matches_filter

logger = logging.getLogger(__name__)


class CalendarEventType:
    CREATED = "google.calendar.event.created"
    UPDATED = "google.calendar.event.updated"
    DELETED = "google.calendar.event.deleted"
    RSVP_CHANGED = "google.calendar.event.rsvp_changed"


class RsvpChangeDTO(BaseModel):
    attendee: str
    before: Optional[str] = None
    after: Optional[str] = None


@dataclass(frozen=True)
class CalendarCacheMutation:
    calendar_id: str
    event_id: str
    event_payload: Optional[dict[str, Any]]


@dataclass(frozen=True)
class CalendarChangeResult:
    events: list[NewEvent]
    cache_mutations: list[CalendarCacheMutation]


_PREVIOUS_EVENT_UNSET = object()


class GoogleCalendarSource:
    _EVENT_PAYLOAD_EXCLUDED_FIELDS = frozenset({"sequence", "iCalUID", "etag", "reminders"})
    _PAYLOAD_TIME_KEY_ALIASES = {"dateTime": "dt", "timeZone": "tz"}

    def __init__(
        self,
        name: str,
        config: GoogleCalendarSourceConfig,
        services: AppServices,
        source_id: int,
    ):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.health = services.health.reporter(name)
        self._poll_had_errors = False

    def _get_service(self):
        creds = get_google_credentials(self.config.token_file, self.name)
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    @staticmethod
    def _parse_rfc3339(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            if len(value) == 10 and value[4] == "-" and value[7] == "-":
                return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
            if len(value) == 8 and value.isdigit():
                return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
            if len(value) == 16 and value.endswith("Z") and value[8] == "T":
                return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            if len(value) == 15 and value[8] == "T":
                return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None

    @staticmethod
    def _event_version(event_item: dict[str, Any]) -> str:
        etag = event_item.get("etag")
        if isinstance(etag, str) and etag:
            return etag.strip('"')

        updated = event_item.get("updated")
        if isinstance(updated, str) and updated:
            return updated

        sequence = event_item.get("sequence")
        if sequence is not None:
            return f"seq-{sequence}"

        created = event_item.get("created")
        if isinstance(created, str) and created:
            return created

        return "snapshot"

    @staticmethod
    def _attendee_key(attendee: dict[str, Any], index: int) -> str:
        email = attendee.get("email")
        if isinstance(email, str) and email:
            return email.lower()

        attendee_id = attendee.get("id")
        if attendee_id is not None:
            return str(attendee_id)

        if attendee.get("self") is True:
            return "self"

        display_name = attendee.get("displayName")
        if isinstance(display_name, str) and display_name:
            return f"name:{display_name}"

        return f"attendee:{index}"

    def _fetch_page(
        self,
        service,
        calendar_id: str,
        sync_token: Optional[str] = None,
        page_token: Optional[str] = None,
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
    ) -> dict[str, Any]:
        max_into_future = self._effective_max_into_future(calendar_id)
        max_future_secs = self._parse_optional_interval(max_into_future)

        kwargs = {
            "calendarId": calendar_id,
            "showDeleted": True,
            "singleEvents": True,
        }

        if sync_token:
            # If we have a sync token, we MUST NOT send timeMax/timeMin
            kwargs["syncToken"] = sync_token
        else:
            # Time limits are allowed only on non-incremental listings.
            if time_max:
                kwargs["timeMax"] = time_max
            elif max_future_secs is not None:
                future_cutoff = datetime.now(timezone.utc) + timedelta(seconds=max_future_secs)
                kwargs["timeMax"] = future_cutoff.isoformat()

            if time_min:
                kwargs["timeMin"] = time_min

        if page_token:
            kwargs["pageToken"] = page_token

        return service.events().list(**kwargs).execute()

    def _effective_max_into_future(self, calendar_id: str) -> Any:
        override = self.config.calendar_overrides.get(calendar_id)
        if override is not None:
            return override.max_into_future
        return self.config.max_into_future

    @staticmethod
    def _parse_optional_interval(value: Any) -> Optional[float]:
        if value is None:
            return None
        return float(parse_interval(value))

    def _sync_fingerprint_key(self, calendar_id: str) -> str:
        return f"config_fingerprint:{calendar_id}"

    def _legacy_sync_config_key(self, calendar_id: str) -> str:
        return f"config_max_into_future:{calendar_id}"

    def _lookahead_cursor_key(self, calendar_id: str) -> str:
        return f"lookahead_cursor:{calendar_id}"

    def _future_cutoff(self, calendar_id: str) -> Optional[datetime]:
        max_into_future = self._effective_max_into_future(calendar_id)
        max_future_secs = self._parse_optional_interval(max_into_future)
        if max_future_secs is None:
            return None
        return datetime.now(timezone.utc) + timedelta(seconds=max_future_secs)

    def _load_lookahead_cursor(self, calendar_id: str) -> Optional[datetime]:
        raw = self.services.kv.get(self.source_id, self._lookahead_cursor_key(calendar_id))
        if isinstance(raw, datetime):
            if raw.tzinfo is None:
                return raw.replace(tzinfo=timezone.utc)
            return raw
        if isinstance(raw, str):
            return self._parse_rfc3339(raw)
        return None

    def _store_lookahead_cursor(self, calendar_id: str, cutoff: Optional[datetime]) -> None:
        key = self._lookahead_cursor_key(calendar_id)
        if cutoff is None:
            self.services.kv.delete(self.source_id, key)
            return
        self.services.kv.set(self.source_id, key, cutoff.isoformat())

    def _sync_fingerprint_payload(self, calendar_id: str) -> dict[str, Any]:
        max_into_future = self._effective_max_into_future(calendar_id)
        normalized_max = self._parse_optional_interval(max_into_future)
        return {
            "max_into_future": normalized_max,
        }

    def _load_sync_fingerprint(self, calendar_id: str) -> Optional[dict[str, Any]]:
        fingerprint_key = self._sync_fingerprint_key(calendar_id)
        raw = self.services.kv.get(self.source_id, fingerprint_key)
        if raw is not None:
            try:
                if isinstance(raw, str):
                    parsed = json.loads(raw)
                elif isinstance(raw, dict):
                    parsed = raw
                else:
                    return None
                if isinstance(parsed, dict):
                    return parsed
            except (TypeError, ValueError, json.JSONDecodeError):
                return None

        return None

    def _store_sync_fingerprint(self, calendar_id: str) -> None:
        payload = self._sync_fingerprint_payload(calendar_id)
        self.services.kv.set(self.source_id, self._sync_fingerprint_key(calendar_id), json.dumps(payload, sort_keys=True))
        if payload["max_into_future"] is not None:
            self.services.kv.set(self.source_id, self._legacy_sync_config_key(calendar_id), payload["max_into_future"])

    def _clear_calendar_snapshots(self, calendar_id: str) -> None:
        prefix = f"snap:{calendar_id}:"
        for key in self.services.kv.list_keys_with_prefix(self.source_id, prefix):
            self.services.kv.delete(self.source_id, key)
        self.services.kv.delete(self.source_id, self._snapshot_state_key(calendar_id))

    def _snapshot_state_key(self, calendar_id: str) -> str:
        return f"snapshot_state:{calendar_id}"

    def _load_snapshot_state(self, calendar_id: str) -> Optional[dict[str, Any]]:
        raw = self.services.kv.get(self.source_id, self._snapshot_state_key(calendar_id))
        if raw is None:
            return None
        try:
            if isinstance(raw, str):
                parsed = json.loads(raw)
            elif isinstance(raw, dict):
                parsed = raw
            else:
                return None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _snapshot_keys(self, calendar_id: str) -> list[str]:
        return self.services.kv.list_keys_with_prefix(self.source_id, f"snap:{calendar_id}:")

    def _store_snapshot_state(self, calendar_id: str) -> None:
        self.services.kv.set(
            self.source_id,
            self._snapshot_state_key(calendar_id),
            {
                "complete": True,
                "count": len(self._snapshot_keys(calendar_id)),
            },
        )

    def _snapshot_cache_is_trusted(self, calendar_id: str) -> bool:
        keys = self._snapshot_keys(calendar_id)
        state = self._load_snapshot_state(calendar_id)

        if state and state.get("complete") is True:
            expected_count = state.get("count")
            return not isinstance(expected_count, int) or expected_count == len(keys)

        if keys:
            # Existing installations predate the marker. Non-empty snapshots are
            # trusted once and marked so future cache wipes can be detected.
            self._store_snapshot_state(calendar_id)
            return True

        return False

    @staticmethod
    def _is_cancelled_recurring_instance(event_item: dict[str, Any]) -> bool:
        return event_item.get("status") == "cancelled" and (
            event_item.get("recurringEventId") is not None or event_item.get("originalStartTime") is not None
        )

    def _make_occurred_at(
        self,
        current_event: dict[str, Any],
        previous_event: Optional[dict[str, Any]] = None,
        *,
        prefer_created: bool = False,
    ) -> Optional[datetime]:
        fields = ["created", "updated"] if prefer_created else ["updated", "created"]

        for field in fields:
            value = current_event.get(field)
            parsed = self._parse_rfc3339(value if isinstance(value, str) else None)
            if parsed is not None:
                return parsed

        if previous_event is not None:
            for field in fields:
                value = previous_event.get(field)
                parsed = self._parse_rfc3339(value if isinstance(value, str) else None)
                if parsed is not None:
                    return parsed

        return None

    @staticmethod
    def _calendar_time_value(event_item: Optional[dict[str, Any]], field: str) -> Optional[str]:
        if not event_item:
            return None
        value = event_item.get(field)
        if not isinstance(value, dict):
            return None
        calendar_time = value.get("dateTime") or value.get("date")
        return calendar_time if isinstance(calendar_time, str) else None

    def _event_start_time(self, event_item: Optional[dict[str, Any]]) -> Optional[datetime]:
        start_value = self._calendar_time_value(event_item, "start")
        if start_value:
            return self._parse_rfc3339(start_value)

        original_start = self._calendar_time_value(event_item, "originalStartTime")
        if original_start:
            return self._parse_rfc3339(original_start)

        return None

    def _event_end_time(self, event_item: Optional[dict[str, Any]]) -> Optional[datetime]:
        end_value = self._calendar_time_value(event_item, "end")
        if end_value:
            return self._parse_rfc3339(end_value)

        return self._event_start_time(event_item)

    @staticmethod
    def _is_recurring_master(event_item: Optional[dict[str, Any]]) -> bool:
        if not event_item:
            return False
        return (
            isinstance(event_item.get("recurrence"), list)
            and event_item.get("recurringEventId") is None
            and event_item.get("originalStartTime") is None
        )

    def _event_retention_end_time(self, event_item: Optional[dict[str, Any]]) -> Optional[datetime]:
        if self._is_recurring_master(event_item):
            return self._recurring_master_end_time(event_item)
        return self._event_end_time(event_item)

    @staticmethod
    def _parse_rrule(rule: str) -> dict[str, str]:
        if rule.startswith("RRULE:"):
            rule = rule.removeprefix("RRULE:")
        parsed: dict[str, str] = {}
        for part in rule.split(";"):
            key, separator, value = part.partition("=")
            if separator:
                parsed[key.upper()] = value
        return parsed

    @staticmethod
    def _add_months(value: datetime, months: int) -> datetime:
        month_index = value.month - 1 + months
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        days_in_month = [
            31,
            29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31,
            30,
            31,
            30,
            31,
            31,
            30,
            31,
            30,
            31,
        ][month - 1]
        return value.replace(year=year, month=month, day=min(value.day, days_in_month))

    def _recurring_master_end_time(self, event_item: Optional[dict[str, Any]]) -> Optional[datetime]:
        if not event_item:
            return None

        recurrence = event_item.get("recurrence")
        if not isinstance(recurrence, list):
            return None

        start_dt = self._event_start_time(event_item)
        event_end = self._event_end_time(event_item)
        duration = None
        if start_dt is not None and event_end is not None:
            duration = event_end - start_dt

        known_end_times: list[datetime] = []
        for rule in recurrence:
            if not isinstance(rule, str) or not rule.startswith("RRULE:"):
                continue

            parts = self._parse_rrule(rule)
            until = self._parse_rfc3339(parts.get("UNTIL"))
            if until is not None:
                known_end_times.append(until + duration if duration is not None else until)
                continue

            count_value = parts.get("COUNT")
            if not count_value or start_dt is None:
                continue
            try:
                count = int(count_value)
                interval = int(parts.get("INTERVAL", "1"))
            except ValueError:
                continue
            if count < 1 or interval < 1:
                continue

            increments = (count - 1) * interval
            freq = parts.get("FREQ")
            if freq == "DAILY":
                last_start = start_dt + timedelta(days=increments)
            elif freq == "WEEKLY":
                last_start = start_dt + timedelta(weeks=increments)
            elif freq == "MONTHLY":
                last_start = self._add_months(start_dt, increments)
            elif freq == "YEARLY":
                last_start = self._add_months(start_dt, increments * 12)
            else:
                continue

            known_end_times.append(last_start + duration if duration is not None else last_start)

        return max(known_end_times) if known_end_times else None

    def _event_has_ended(self, event_item: dict[str, Any]) -> bool:
        event_end = self._event_retention_end_time(event_item)
        return event_end is not None and event_end < datetime.now(timezone.utc)

    def _event_has_own_schedule_time(self, event_item: dict[str, Any]) -> bool:
        return (
            self._event_start_time(event_item) is not None
            or self._calendar_time_value(event_item, "end") is not None
        )

    def _change_is_for_ended_event(
        self,
        event_item: dict[str, Any],
        previous_event: Optional[dict[str, Any]],
    ) -> bool:
        if self._event_has_ended(event_item):
            return True
        if self._event_has_own_schedule_time(event_item):
            return False
        return previous_event is not None and self._event_has_ended(previous_event)

    def _is_too_old(
        self,
        event_item: dict[str, Any],
        fallback_event: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        Check if the event schedule is too far in the past to keep tracking.
        Future or ongoing events must remain eligible even if their created/updated
        metadata is old.
        """
        max_age_days = self.config.max_event_age_days
        if max_age_days is None:
            return False

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=max_age_days)

        event_dt = self._event_retention_end_time(event_item) or self._event_retention_end_time(fallback_event)
        if event_dt and event_dt < cutoff:
            logger.debug(
                "Event %s ignored because it ended at %s, which is older than %s days.",
                event_item.get("id"),
                event_dt,
                max_age_days
            )
            return True

        return False

    def _is_too_far_future(
        self,
        event_item: dict[str, Any],
        max_into_future: float,
        fallback_event: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        Check if the event is too far in the future based on max_into_future.
        """
        start_dt = self._event_start_time(event_item) or self._event_start_time(fallback_event)
        if not start_dt:
            return False

        cutoff = datetime.now(timezone.utc) + timedelta(seconds=max_into_future)
        return start_dt > cutoff

    def _extract_rsvp_map(
        self,
        event_item: Optional[dict[str, Any]],
    ) -> dict[str, Optional[str]]:
        if event_item is None:
            return {}

        attendees = event_item.get("attendees", [])
        if not isinstance(attendees, list):
            return {}

        result: dict[str, Optional[str]] = {}
        for idx, attendee in enumerate(attendees):
            if not isinstance(attendee, dict):
                continue
            result[self._attendee_key(attendee, idx)] = attendee.get("responseStatus")
        return result

    def _diff_rsvp(
        self,
        previous_event: Optional[dict[str, Any]],
        current_event: dict[str, Any],
    ) -> list[RsvpChangeDTO]:
        before = self._extract_rsvp_map(previous_event)
        after = self._extract_rsvp_map(current_event)

        changes: list[RsvpChangeDTO] = []
        for attendee_key in sorted(set(before) | set(after)):
            old_status = before.get(attendee_key)
            new_status = after.get(attendee_key)
            if old_status != new_status:
                changes.append(
                    RsvpChangeDTO(
                        attendee=attendee_key,
                        before=old_status,
                        after=new_status,
                    )
                )
        return changes

    def _attendee_state_summary(self, attendees: list[Any]) -> dict[str, Any]:
        by_state: dict[str, int] = {}
        for attendee in attendees:
            state = None
            if isinstance(attendee, dict):
                state = attendee.get("responseStatus")
            if not isinstance(state, str) or not state:
                state = "unknown"
            by_state[state] = by_state.get(state, 0) + 1

        return {
            "total": len(attendees),
            "by_state": dict(sorted(by_state.items())),
        }

    def _attendees_exceed_detail_limit(self, event_item: Optional[dict[str, Any]]) -> bool:
        if event_item is None:
            return False
        attendees = event_item.get("attendees")
        return isinstance(attendees, list) and len(attendees) > self.config.attendee_detail_limit

    @classmethod
    def _compact_payload_time_keys(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                cls._PAYLOAD_TIME_KEY_ALIASES.get(key, key): cls._compact_payload_time_keys(item)
                for key, item in value.items()
            }

        if isinstance(value, list):
            return [cls._compact_payload_time_keys(item) for item in value]

        return value

    def _event_for_payload(self, event_item: dict[str, Any]) -> dict[str, Any]:
        event_copy = deepcopy(event_item)
        for field in self._EVENT_PAYLOAD_EXCLUDED_FIELDS:
            event_copy.pop(field, None)

        attendees = event_copy.get("attendees")
        if isinstance(attendees, list) and len(attendees) > self.config.attendee_detail_limit:
            event_copy["attendees"] = self._attendee_state_summary(attendees)
        return event_copy

    def _event_for_update_payload(
        self,
        original_event: dict[str, Any],
        normalized_event: dict[str, Any],
        *,
        summarize_attendees: bool,
    ) -> dict[str, Any]:
        event_copy = deepcopy(normalized_event)
        for field in self._EVENT_PAYLOAD_EXCLUDED_FIELDS:
            event_copy.pop(field, None)

        attendees = original_event.get("attendees")
        if isinstance(attendees, list) and (
            summarize_attendees or len(attendees) > self.config.attendee_detail_limit
        ):
            event_copy["attendees"] = self._attendee_state_summary(attendees)
        return event_copy

    def _rsvp_summary_for_payload(
        self,
        previous_event: Optional[dict[str, Any]],
        current_event: Optional[dict[str, Any]],
        rsvp_changes: list[RsvpChangeDTO],
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "changed": len(rsvp_changes),
        }

        if previous_event is not None:
            attendees = previous_event.get("attendees")
            if isinstance(attendees, list):
                summary["before"] = self._attendee_state_summary(attendees)

        if current_event is not None:
            attendees = current_event.get("attendees")
            if isinstance(attendees, list):
                summary["after"] = self._attendee_state_summary(attendees)

        return summary

    def _normalize_for_general_change(
        self,
        event_item: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if event_item is None:
            return None

        normalized = deepcopy(event_item)
        for field in self._EVENT_PAYLOAD_EXCLUDED_FIELDS:
            normalized.pop(field, None)

        normalized.pop("updated", None)
        normalized.pop("kind", None)

        attendees = normalized.get("attendees")
        if isinstance(attendees, list):
            normalized_attendees: list[dict[str, Any]] = []

            for idx, attendee in enumerate(attendees):
                if not isinstance(attendee, dict):
                    continue

                attendee_copy = dict(attendee)
                attendee_copy.pop("responseStatus", None)
                attendee_copy["_sort_key"] = self._attendee_key(attendee_copy, idx)
                normalized_attendees.append(attendee_copy)

            normalized_attendees.sort(key=lambda item: item["_sort_key"])
            for attendee_copy in normalized_attendees:
                attendee_copy.pop("_sort_key", None)

            normalized["attendees"] = normalized_attendees

        return normalized

    def _has_non_rsvp_change(
        self,
        previous_event: Optional[dict[str, Any]],
        current_event: dict[str, Any],
    ) -> bool:
        return self._normalize_for_general_change(previous_event) != self._normalize_for_general_change(current_event)

    def _make_event_payload(
        self,
        *,
        calendar_id: str,
        event_type: str,
        current_event: Optional[dict[str, Any]] = None,
        previous_event: Optional[dict[str, Any]] = None,
        rsvp_changes: Optional[list[RsvpChangeDTO]] = None,
    ) -> dict[str, Any]:
        # Always include the event ID and minimal context in the payload root
        event_id = None
        summary = None
        start = None
        if current_event:
            event_id = current_event.get("id")
            summary = current_event.get("summary")
            start = current_event.get("start")
        elif previous_event:
            event_id = previous_event.get("id")
            summary = previous_event.get("summary")
            start = previous_event.get("start")

        payload: dict[str, Any] = {
            "calendar_id": calendar_id,
            "event_id": event_id,
            "summary": summary,
            "start": start,
            "recurrence": (current_event or previous_event or {}).get("recurrence", []),
            "recurring_event_id": (current_event or previous_event or {}).get("recurringEventId"),
        }

        if event_type == CalendarEventType.CREATED:
            if current_event:
                payload["event"] = self._event_for_payload(current_event)

        elif event_type == CalendarEventType.UPDATED:
            if previous_event and current_event:
                exclude = {*self._EVENT_PAYLOAD_EXCLUDED_FIELDS, "updated", "id", "kind"}
                before_norm = self._normalize_for_general_change(previous_event) or {}
                after_norm = self._normalize_for_general_change(current_event) or {}
                summarize_attendees = self._attendees_exceed_detail_limit(
                    previous_event
                ) or self._attendees_exceed_detail_limit(current_event)
                before_norm = self._event_for_update_payload(
                    previous_event,
                    before_norm,
                    summarize_attendees=summarize_attendees,
                )
                after_norm = self._event_for_update_payload(
                    current_event,
                    after_norm,
                    summarize_attendees=summarize_attendees,
                )

                payload["changes"] = DictDiff.compute(
                    before_norm,
                    after_norm,
                    exclude=exclude,
                )

        elif event_type == CalendarEventType.DELETED:
            if current_event:
                payload["event"] = self._event_for_payload(current_event)
            if previous_event:
                payload["previous"] = self._event_for_payload(previous_event)

        elif event_type == CalendarEventType.RSVP_CHANGED:
            if rsvp_changes:
                if self._attendees_exceed_detail_limit(
                    current_event
                ) or self._attendees_exceed_detail_limit(previous_event):
                    payload["rsvp_changes"] = self._rsvp_summary_for_payload(
                        previous_event,
                        current_event,
                        rsvp_changes,
                    )
                else:
                    payload["rsvp_changes"] = [
                        change.model_dump() for change in rsvp_changes
                    ]

        return self._compact_payload_time_keys(payload)

    def _make_new_event(
        self,
        *,
        calendar_id: str,
        event_type: str,
        entity_id: str,
        version: str,
        occurred_at: Optional[datetime],
        data: dict[str, Any],
    ) -> NewEvent:
        event_name = event_type.split(".")[-1]
        scoped_entity_id = f"{calendar_id}:{entity_id}"
        return NewEvent(
            event_id=f"gcal:{scoped_entity_id}:{version}:{event_name}",
            event_type=event_type,
            entity_id=scoped_entity_id,
            data=data,
            occurred_at=occurred_at,
        )

    def _cache_mutation(
        self,
        calendar_id: str,
        event_id: str,
        event_payload: Optional[dict[str, Any]],
    ) -> CalendarCacheMutation:
        return CalendarCacheMutation(calendar_id, event_id, event_payload)

    def _apply_cache_mutations(self, calendar_id: str, mutations: list[CalendarCacheMutation]) -> None:
        for mutation in mutations:
            self.set_cache(mutation.calendar_id, mutation.event_id, mutation.event_payload)
        if mutations:
            self._store_snapshot_state(calendar_id)

    def _classify_event_change_result(
        self,
        calendar_id: str,
        event_item: dict[str, Any],
        previous_event: Any = _PREVIOUS_EVENT_UNSET,
    ) -> CalendarChangeResult:
        entity_id = event_item.get("id")
        if not isinstance(entity_id, str) or not entity_id:
            return CalendarChangeResult([], [])

        if previous_event is _PREVIOUS_EVENT_UNSET:
            previous_event = self.get_cached(calendar_id, entity_id)

        cache_mutations: list[CalendarCacheMutation] = []
        if self._should_filter(event_item) or (
            isinstance(previous_event, dict) and self._should_filter(previous_event)
        ):
            logger.info(f"Event {entity_id} filtered out because it matches a filter")
            if previous_event is not None:
                cache_mutations.append(self._cache_mutation(calendar_id, entity_id, None))
            return CalendarChangeResult([], cache_mutations)

        version = self._event_version(event_item)
        occurred_at = self._make_occurred_at(event_item, previous_event)

        if (
            self._event_has_ended(event_item)
            and previous_event is not None
            and not self._event_has_ended(previous_event)
        ):
            cache_mutations.append(self._cache_mutation(calendar_id, entity_id, None))
            return CalendarChangeResult(
                [
                    self._make_new_event(
                        calendar_id=calendar_id,
                        event_type=CalendarEventType.DELETED,
                        entity_id=entity_id,
                        version=version,
                        occurred_at=occurred_at,
                        data=self._make_event_payload(
                            calendar_id=calendar_id,
                            event_type=CalendarEventType.DELETED,
                            current_event=event_item,
                            previous_event=previous_event,
                        ),
                    ),
                ],
                cache_mutations,
            )

        if self._is_too_old(event_item, previous_event):
            if previous_event is not None:
                cache_mutations.append(self._cache_mutation(calendar_id, entity_id, None))
            return CalendarChangeResult([], cache_mutations)

        if self._change_is_for_ended_event(event_item, previous_event):
            if previous_event is not None:
                payload = None if event_item.get("status") == "cancelled" else event_item
                cache_mutations.append(self._cache_mutation(calendar_id, entity_id, payload))
            return CalendarChangeResult([], cache_mutations)

        max_into_future = self._effective_max_into_future(calendar_id)
        max_future_secs = self._parse_optional_interval(max_into_future)
        if max_future_secs is not None:
            if self._is_too_far_future(event_item, max_future_secs, previous_event):
                if previous_event is not None:
                    # Event was previously in range but moved out - emit deletion
                    cache_mutations.append(self._cache_mutation(calendar_id, entity_id, None))
                    return CalendarChangeResult(
                        [
                            self._make_new_event(
                                calendar_id=calendar_id,
                                event_type=CalendarEventType.DELETED,
                                entity_id=entity_id,
                                version=version,
                                occurred_at=occurred_at,
                                data=self._make_event_payload(
                                    calendar_id=calendar_id,
                                    event_type=CalendarEventType.DELETED,
                                    current_event=event_item,
                                    previous_event=previous_event,
                                ),
                            ),
                        ],
                        cache_mutations,
                    )
                return CalendarChangeResult([], [])

        status = event_item.get("status")
        if status == "cancelled":
            if self._is_cancelled_recurring_instance(event_item):
                cache_mutations.append(self._cache_mutation(calendar_id, entity_id, event_item))
            else:
                cache_mutations.append(self._cache_mutation(calendar_id, entity_id, None))
            if previous_event is not None and previous_event.get("status") == "cancelled":
                return CalendarChangeResult([], cache_mutations)
            return CalendarChangeResult(
                [
                    self._make_new_event(
                        calendar_id=calendar_id,
                        event_type=CalendarEventType.DELETED,
                        entity_id=entity_id,
                        version=version,
                        occurred_at=occurred_at,
                        data=self._make_event_payload(
                            calendar_id=calendar_id,
                            event_type=CalendarEventType.DELETED,
                            current_event=event_item,
                            previous_event=previous_event,
                        ),
                    ),
                ],
                cache_mutations,
            )

        if previous_event is None:
            cache_mutations.append(self._cache_mutation(calendar_id, entity_id, event_item))
            return CalendarChangeResult(
                [
                    self._make_new_event(
                        calendar_id=calendar_id,
                        event_type=CalendarEventType.CREATED,
                        entity_id=entity_id,
                        version=version,
                        occurred_at=self._make_occurred_at(event_item, prefer_created=True),
                        data=self._make_event_payload(
                            calendar_id=calendar_id,
                            event_type=CalendarEventType.CREATED,
                            current_event=event_item,
                        ),
                    ),
                ],
                cache_mutations,
            )

        emitted: list[NewEvent] = []

        rsvp_changes = self._diff_rsvp(previous_event, event_item)
        if rsvp_changes:
            emitted.append(
                self._make_new_event(
                    calendar_id=calendar_id,
                    event_type=CalendarEventType.RSVP_CHANGED,
                    entity_id=entity_id,
                    version=version,
                    occurred_at=occurred_at,
                    data=self._make_event_payload(
                        calendar_id=calendar_id,
                        event_type=CalendarEventType.RSVP_CHANGED,
                        current_event=event_item,
                        previous_event=previous_event,
                        rsvp_changes=rsvp_changes,
                    ),
                )
            )

        if self._has_non_rsvp_change(previous_event, event_item):
            emitted.append(
                self._make_new_event(
                    calendar_id=calendar_id,
                    event_type=CalendarEventType.UPDATED,
                    entity_id=entity_id,
                    version=version,
                    occurred_at=occurred_at,
                    data=self._make_event_payload(
                        calendar_id=calendar_id,
                        event_type=CalendarEventType.UPDATED,
                        current_event=event_item,
                        previous_event=previous_event,
                    ),
                )
            )

        cache_mutations.append(self._cache_mutation(calendar_id, entity_id, event_item))
        return CalendarChangeResult(emitted, cache_mutations)

    def _classify_event_change(self, calendar_id: str, event_item: dict[str, Any]) -> list[NewEvent]:
        return self._classify_event_change_result(calendar_id, event_item).events

    def _should_filter(self, event_item: dict[str, Any]) -> bool:
        if not self.config.filters:
            return False

        for filter_dict in self.config.filters:
            for name, f in filter_dict.items():
                value_to_check = ""
                if f.in_field == "summary":
                    value_to_check = event_item.get("summary", "")
                elif f.in_field == "description":
                    value_to_check = event_item.get("description", "")
                elif f.in_field == "location":
                    value_to_check = event_item.get("location", "")
                elif f.in_field == "organizer":
                    organizer = event_item.get("organizer", {})
                    if isinstance(organizer, dict):
                        value_to_check = " ".join(
                            value
                            for value in [
                                organizer.get("email", ""),
                                organizer.get("displayName", ""),
                            ]
                            if isinstance(value, str) and value
                        )
                elif f.in_field == "attendees":
                    attendees = event_item.get("attendees", [])
                    value_to_check = " ".join([a.get("email", "") for a in attendees if a.get("email")])

                if matches_filter(value_to_check, f, name):
                    logger.info(f"Filtering out event {event_item.get('id')} because it matched filter '{name}'")
                    return True
        return False

    def get_cached(self, calendar_id: str, event_id: str) -> Optional[dict[str, Any]]:
        """
        Return the last cached payload for this event ID, or None if missing.
        """
        key = f"snap:{calendar_id}:{event_id}"
        val = self.services.kv.get(self.source_id, key)
        if isinstance(val, dict):
            return val
        return None

    def set_cache(self, calendar_id: str, event_id: str, event_payload: Optional[dict[str, Any]]) -> None:
        """
        Store the latest payload for this event ID.
        """
        key = f"snap:{calendar_id}:{event_id}"
        if event_payload is None:
            self.services.kv.delete(self.source_id, key)
        else:
            self.services.kv.set(self.source_id, key, event_payload)

    def _replace_calendar_snapshots(self, calendar_id: str, snapshots: dict[str, dict[str, Any]]) -> None:
        self._clear_calendar_snapshots(calendar_id)
        for event_id, event_item in snapshots.items():
            if not self._should_store_baseline_snapshot(calendar_id, event_item):
                continue
            self.set_cache(calendar_id, event_id, event_item)
        self._store_snapshot_state(calendar_id)

    def _prune_snapshots_outside_future_window(
        self,
        calendar_id: str,
        max_into_future: Optional[float],
    ) -> None:
        if max_into_future is None:
            return

        prefix = f"snap:{calendar_id}:"
        deleted_any = False
        for key in self._snapshot_keys(calendar_id):
            event_id = key.removeprefix(prefix)
            cached_event = self.get_cached(calendar_id, event_id)
            if cached_event and self._is_too_far_future(cached_event, max_into_future):
                logger.info(
                    "Dropping cached event %s because it is outside max_into_future=%s.",
                    event_id,
                    max_into_future,
                )
                self.services.kv.delete(self.source_id, key)
                deleted_any = True

        if deleted_any:
            self._store_snapshot_state(calendar_id)

    def _full_sync_time_min(self, calendar_id: str) -> Optional[str]:
        return datetime.now(timezone.utc).isoformat()

    def _should_store_baseline_snapshot(self, calendar_id: str, event_item: dict[str, Any]) -> bool:
        if event_item.get("status") == "cancelled":
            return False
        if self._should_filter(event_item):
            return False
        if self._change_is_for_ended_event(event_item, None):
            return False

        max_into_future = self._effective_max_into_future(calendar_id)
        max_future_secs = self._parse_optional_interval(max_into_future)
        if max_future_secs is not None and self._is_too_far_future(event_item, max_future_secs):
            return False

        return True

    def _should_skip_unbounded_reconcile_item(
        self,
        event_item: dict[str, Any],
        previous_event: Optional[dict[str, Any]],
    ) -> bool:
        if previous_event is not None:
            return False
        if event_item.get("status") == "cancelled":
            return True
        return self._change_is_for_ended_event(event_item, None)

    def _rebuild_sync_baseline(self, service, calendar_id: str) -> bool:
        """
        Rebuild the local baseline from current Calendar state, emit nothing,
        and persist a fresh sync token.
        """
        logger.info("Rebuilding calendar sync baseline for %s (calendar: %s)", self.name, calendar_id)

        baseline_time_min = self._full_sync_time_min(calendar_id)
        baseline_time_max_dt = self._future_cutoff(calendar_id)
        baseline_time_max = baseline_time_max_dt.isoformat() if baseline_time_max_dt else None
        page_token: Optional[str] = None
        new_sync_token: Optional[str] = None
        snapshots: dict[str, dict[str, Any]] = {}

        while True:
            result = self._fetch_page(
                service,
                calendar_id=calendar_id,
                sync_token=None,
                page_token=page_token,
                time_min=baseline_time_min,
                time_max=baseline_time_max,
            )

            for event_item in result.get("items", []):
                if not isinstance(event_item, dict):
                    continue

                event_id = event_item.get("id")
                if not isinstance(event_id, str) or not event_id:
                    continue

                if not self._should_store_baseline_snapshot(calendar_id, event_item):
                    continue

                snapshots[event_id] = event_item

            page_token = result.get("nextPageToken")
            if not page_token:
                new_sync_token = result.get("nextSyncToken")
                break

        if new_sync_token:
            self._replace_calendar_snapshots(calendar_id, snapshots)
            self._store_lookahead_cursor(calendar_id, baseline_time_max_dt)
            cursor_key = f"sync_token:{calendar_id}"
            self.services.kv.set(self.source_id, cursor_key, new_sync_token)
            self._store_sync_fingerprint(calendar_id)
            
            logger.info("Calendar sync baseline initialized for %s (calendar: %s)", self.name, calendar_id)
            return True

        return False

    def _reconcile_expired_sync_token(self, service, calendar_id: str, cursor_key: str) -> None:
        snapshot_trusted = self._snapshot_cache_is_trusted(calendar_id)
        prefix = f"snap:{calendar_id}:"
        old_snapshots: dict[str, dict[str, Any]] = {}
        for key in self._snapshot_keys(calendar_id):
            event_id = key.removeprefix(prefix)
            cached_event = self.get_cached(calendar_id, event_id)
            if cached_event is not None:
                old_snapshots[event_id] = cached_event

        current_snapshots: dict[str, dict[str, Any]] = {}
        emitted_events: list[NewEvent] = []
        page_token: Optional[str] = None
        new_sync_token: Optional[str] = None
        baseline_time_min = self._full_sync_time_min(calendar_id)
        baseline_time_max_dt = self._future_cutoff(calendar_id)
        baseline_time_max = baseline_time_max_dt.isoformat() if baseline_time_max_dt else None

        while True:
            result = self._fetch_page(
                service,
                calendar_id=calendar_id,
                sync_token=None,
                page_token=page_token,
                time_min=baseline_time_min,
                time_max=baseline_time_max,
            )
            for event_item in result.get("items", []):
                if not isinstance(event_item, dict):
                    continue
                event_id = event_item.get("id")
                if isinstance(event_id, str) and event_id:
                    previous_event = old_snapshots.get(event_id)
                    if (
                        baseline_time_min is None
                        and self._should_skip_unbounded_reconcile_item(event_item, previous_event)
                    ):
                        continue
                    current_snapshots[event_id] = event_item

            page_token = result.get("nextPageToken")
            if not page_token:
                new_sync_token = result.get("nextSyncToken")
                break

        if not snapshot_trusted:
            logger.warning(
                "Snapshot cache for %s (calendar: %s) is missing or incomplete; rebuilding baseline without emitted events.",
                self.name,
                calendar_id,
            )
            self._replace_calendar_snapshots(calendar_id, current_snapshots)
            if new_sync_token:
                self._store_lookahead_cursor(calendar_id, baseline_time_max_dt)
                self.services.kv.set(self.source_id, cursor_key, new_sync_token)
                self._store_sync_fingerprint(calendar_id)
            return

        cache_mutations: list[CalendarCacheMutation] = []
        for event_id, old_event in old_snapshots.items():
            if event_id not in current_snapshots:
                # Skip events that already ended - they are absent from the
                # fresh listing because timeMin filters by end time, not
                # because they were deleted.
                if self._event_has_ended(old_event):
                    cache_mutations.append(self._cache_mutation(calendar_id, event_id, None))
                    continue
                if baseline_time_min is not None and self._is_recurring_master(old_event):
                    # A timeMin-bounded refresh can omit active recurring masters
                    # whose first occurrence ended long ago.
                    continue
                emitted_events.append(
                    self._make_new_event(
                        calendar_id=calendar_id,
                        event_type=CalendarEventType.DELETED,
                        entity_id=event_id,
                        version=self._event_version(old_event),
                        occurred_at=self._make_occurred_at(old_event),
                        data=self._make_event_payload(
                            calendar_id=calendar_id,
                            event_type=CalendarEventType.DELETED,
                            current_event=None,
                            previous_event=old_event,
                        ),
                    )
                )
                cache_mutations.append(self._cache_mutation(calendar_id, event_id, None))

        for event_id, event_item in current_snapshots.items():
            result = self._classify_event_change_result(
                calendar_id,
                event_item,
                previous_event=old_snapshots.get(event_id),
            )
            emitted_events.extend(result.events)
            cache_mutations.extend(result.cache_mutations)

        if emitted_events:
            self.services.writer.write_events(self.source_id, emitted_events)
        self._apply_cache_mutations(calendar_id, cache_mutations)
        if new_sync_token:
            self._store_lookahead_cursor(calendar_id, baseline_time_max_dt)
            self.services.kv.set(self.source_id, cursor_key, new_sync_token)
            self._store_sync_fingerprint(calendar_id)

    def _collect_rolling_lookahead_changes(
        self,
        service,
        calendar_id: str,
        start_cutoff: datetime,
        end_cutoff: datetime,
        processed_event_ids: set[str],
    ) -> CalendarChangeResult:
        emitted_events: list[NewEvent] = []
        cache_mutations: list[CalendarCacheMutation] = []
        page_token: Optional[str] = None

        while True:
            result = self._fetch_page(
                service,
                calendar_id=calendar_id,
                sync_token=None,
                page_token=page_token,
                time_min=start_cutoff.isoformat(),
                time_max=end_cutoff.isoformat(),
            )

            for event_item in result.get("items", []):
                if not isinstance(event_item, dict):
                    continue
                event_id = event_item.get("id")
                if not isinstance(event_id, str) or not event_id:
                    continue
                if event_id in processed_event_ids:
                    continue

                processed_event_ids.add(event_id)
                change_result = self._classify_event_change_result(calendar_id, event_item)
                emitted_events.extend(change_result.events)
                cache_mutations.extend(change_result.cache_mutations)

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        return CalendarChangeResult(emitted_events, cache_mutations)

    async def fetch_and_publish_calendar(self, service, calendar_id: str):
        try:
            current_fingerprint = self._sync_fingerprint_payload(calendar_id)
            config_key = self._legacy_sync_config_key(calendar_id)
            fingerprint_key = self._sync_fingerprint_key(calendar_id)
            lookahead_cursor_key = self._lookahead_cursor_key(calendar_id)
            last_fingerprint = self._load_sync_fingerprint(calendar_id)
            cursor_key = f"sync_token:{calendar_id}"
            current_sync_token = self.services.kv.get(self.source_id, cursor_key)

            config_changed = False
            if last_fingerprint is not None:
                config_changed = last_fingerprint != current_fingerprint
            elif current_sync_token:
                config_changed = True

            if config_changed:
                logger.info(
                    "Configuration changed for calendar %s, resetting sync token.",
                    calendar_id
                )

                # Drop out-of-range snapshots silently. A horizon/config change is
                # not a real calendar deletion and can otherwise create large fake
                # cascades after reducing an overly broad lookahead.
                if last_fingerprint is not None:
                    self._prune_snapshots_outside_future_window(
                        calendar_id,
                        current_fingerprint.get("max_into_future"),
                    )

                self.services.kv.delete(self.source_id, cursor_key)
                self.services.kv.delete(self.source_id, config_key)
                self.services.kv.delete(self.source_id, fingerprint_key)
                self.services.kv.delete(self.source_id, lookahead_cursor_key)
                current_sync_token = None

            if not current_sync_token:
                if not self._rebuild_sync_baseline(service, calendar_id):
                    self._poll_had_errors = True
                return

            if not self._snapshot_cache_is_trusted(calendar_id):
                logger.warning(
                    "Snapshot cache for %s (calendar: %s) is missing or incomplete; rebuilding baseline without emitted events.",
                    self.name,
                    calendar_id,
                )
                if not self._rebuild_sync_baseline(service, calendar_id):
                    self._poll_had_errors = True
                return

            emitted_events: list[NewEvent] = []
            cache_mutations: list[CalendarCacheMutation] = []
            processed_event_ids: set[str] = set()
            page_token: Optional[str] = None
            new_sync_token = current_sync_token

            while True:
                try:
                    result = self._fetch_page(
                        service,
                        calendar_id=calendar_id,
                        sync_token=current_sync_token,
                        page_token=page_token,
                        time_min=None,
                    )
                except HttpError as e:
                    if e.resp.status == 410:
                        logger.warning(
                            "syncToken %s expired for %s (calendar: %s), rebuilding baseline",
                            current_sync_token,
                            self.name,
                            calendar_id
                        )
                        self._reconcile_expired_sync_token(service, calendar_id, cursor_key)
                        return
                    raise

                for event_item in result.get("items", []):
                    if not isinstance(event_item, dict):
                        continue
                    event_id = event_item.get("id")
                    if isinstance(event_id, str) and event_id:
                        processed_event_ids.add(event_id)
                    change_result = self._classify_event_change_result(calendar_id, event_item)
                    emitted_events.extend(change_result.events)
                    cache_mutations.extend(change_result.cache_mutations)

                page_token = result.get("nextPageToken")
                if not page_token:
                    new_sync_token = result.get("nextSyncToken", new_sync_token)
                    break

            should_store_lookahead_cursor = False
            lookahead_cutoff_to_store: Optional[datetime] = None
            lookahead_cutoff = self._future_cutoff(calendar_id)
            if lookahead_cutoff is not None:
                current_lookahead_cursor = self._load_lookahead_cursor(calendar_id)
                should_store_lookahead_cursor = True
                lookahead_cutoff_to_store = lookahead_cutoff

                if current_lookahead_cursor is not None and current_lookahead_cursor < lookahead_cutoff:
                    lookahead_result = self._collect_rolling_lookahead_changes(
                        service,
                        calendar_id,
                        current_lookahead_cursor,
                        lookahead_cutoff,
                        processed_event_ids,
                    )
                    emitted_events.extend(lookahead_result.events)
                    cache_mutations.extend(lookahead_result.cache_mutations)

            if emitted_events:
                self.services.writer.write_events(self.source_id, emitted_events)

            self._apply_cache_mutations(calendar_id, cache_mutations)

            if should_store_lookahead_cursor:
                self._store_lookahead_cursor(calendar_id, lookahead_cutoff_to_store)

            if str(new_sync_token) != str(current_sync_token):
                self.services.kv.set(self.source_id, cursor_key, new_sync_token)
                self._store_sync_fingerprint(calendar_id)

        except HttpError as error:
            logger.error(
                "An error occurred in Calendar source %s (calendar: %s): %s",
                self.name,
                calendar_id,
                error,
                exc_info=True,
            )
            self._poll_had_errors = True
        except Exception as e:
            logger.error(
                "Unexpected error in Calendar source %s (calendar: %s): %s",
                self.name,
                calendar_id,
                e,
                exc_info=True,
            )
            self._poll_had_errors = True

    async def fetch_and_publish(self):
        self._poll_had_errors = False
        service = self._get_service()
        for calendar_id in self.config.calendar_ids:
            await self.fetch_and_publish_calendar(service, calendar_id)

    async def run(self):
        logger.info(
            "Starting Calendar source: %s polling every %s",
            self.name,
            self.config.poll_interval,
        )
        self.services.add_task(self._cleanup_loop())
        while True:
            self.health.checking()
            try:
                await self.fetch_and_publish()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.health.unhealthy_from_exception(error)
            else:
                if self._poll_had_errors:
                    self.health.unhealthy(
                        "partial_failure",
                        "One or more configured calendars could not be synchronized.",
                    )
                else:
                    self.health.healthy()
            await asyncio.sleep(self.config.poll_interval)

    async def _cleanup_loop(self):
        """Periodically remove stale snapshots for events too far in the past or future."""
        while True:
            try:
                for calendar_id in self.config.calendar_ids:
                    prefix = f"snap:{calendar_id}:"
                    max_into_future = self._effective_max_into_future(calendar_id)
                    max_future_secs = self._parse_optional_interval(max_into_future)

                    deleted_any = False
                    for key in self.services.kv.list_keys_with_prefix(self.source_id, prefix):
                        event_id = key.removeprefix(prefix)
                        cached = self.get_cached(calendar_id, event_id)
                        if not cached:
                            continue
                        if self._is_too_old(cached):
                            self.services.kv.delete(self.source_id, key)
                            deleted_any = True
                            continue
                        if max_future_secs is not None and self._is_too_far_future(cached, max_future_secs):
                            self.services.kv.delete(self.source_id, key)
                            deleted_any = True
                    if deleted_any:
                        self._store_snapshot_state(calendar_id)
            except Exception as e:
                logger.error("Error in cleanup loop for %s: %s", self.name, e)
            await asyncio.sleep(12 * 3600)
