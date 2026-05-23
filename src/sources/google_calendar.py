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

from src.config import GoogleCalendarSourceConfig
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
    ) -> dict[str, Any]:
        single_events, max_into_future = self._effective_sync_settings(calendar_id)

        # If max_into_future is a string (e.g. from overrides), parse it
        if isinstance(max_into_future, str):
            from src.config import parse_interval
            max_into_future = parse_interval(max_into_future)

        kwargs = {
            "calendarId": calendar_id,
            "showDeleted": True,
            "singleEvents": single_events,
        }

        if sync_token:
            # If we have a sync token, we MUST NOT send timeMax/timeMin
            kwargs["syncToken"] = sync_token
        else:
            # Only add time limits during the initial full sync (no syncToken)
            if max_into_future is not None:
                future_cutoff = datetime.now(timezone.utc) + timedelta(seconds=float(max_into_future))
                kwargs["timeMax"] = future_cutoff.isoformat()

            if time_min:
                kwargs["timeMin"] = time_min

        if page_token:
            kwargs["pageToken"] = page_token

        return service.events().list(**kwargs).execute()

    def _effective_sync_settings(self, calendar_id: str) -> tuple[bool, Any]:
        overrides = self.config.calendar_overrides.get(calendar_id, {})
        single_events = bool(overrides.get("single_events", self.config.single_events))
        max_into_future = overrides.get("max_into_future", self.config.max_into_future)
        return single_events, max_into_future

    def _sync_fingerprint_key(self, calendar_id: str) -> str:
        return f"config_fingerprint:{calendar_id}"

    def _legacy_sync_config_key(self, calendar_id: str) -> str:
        return f"config_max_into_future:{calendar_id}"

    def _sync_fingerprint_payload(self, calendar_id: str) -> dict[str, Any]:
        single_events, max_into_future = self._effective_sync_settings(calendar_id)
        if isinstance(max_into_future, str):
            from src.config import parse_interval
            max_into_future = parse_interval(max_into_future)

        normalized_max = None if max_into_future is None else float(max_into_future)
        return {
            "single_events": single_events,
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

        legacy_key = self._legacy_sync_config_key(calendar_id)
        legacy_raw = self.services.kv.get(self.source_id, legacy_key)
        if legacy_raw is None:
            return None
        try:
            legacy_max = float(legacy_raw)
        except (TypeError, ValueError):
            return None

        single_events, _ = self._effective_sync_settings(calendar_id)
        return {
            "single_events": single_events,
            "max_into_future": legacy_max,
        }

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

    def _event_has_ended(self, event_item: dict[str, Any]) -> bool:
        event_end = self._event_end_time(event_item)
        return event_end is not None and event_end < datetime.now(timezone.utc)

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

        event_dt = self._event_end_time(event_item) or self._event_end_time(fallback_event)
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

    def _normalize_for_general_change(
        self,
        event_item: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if event_item is None:
            return None

        normalized = deepcopy(event_item)
        normalized.pop("etag", None)
        normalized.pop("updated", None)
        normalized.pop("sequence", None)
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
                payload["event"] = deepcopy(current_event)
        
        elif event_type == CalendarEventType.UPDATED:
            # For updates, we provide the diff of changed fields
            if previous_event and current_event:
                # Use common fields for the diff but exclude large/unstable ones
                # to keep it minimal as per instructions.
                # However, the user said "fields which changed with before/after subobjects (computed dynamically with the util class)"
                # We can compute the diff between the two snapshots but exclude very large fields if they didn't change.
                # Actually, the DictDiff only returns what changed.
                exclude = {"etag", "updated", "sequence", "id", "kind"}
                # We also want to exclude attendees from the general update diff 
                # because they are handled by RSVP if they are the only change.
                # If they are part of a general update, they might be included, 
                # but it can be messy.
                # Let's keep it simple for now as requested.
                before_norm = self._normalize_for_general_change(previous_event) or {}
                after_norm = self._normalize_for_general_change(current_event) or {}
                
                payload["changes"] = DictDiff.compute(before_norm, after_norm, exclude=exclude)

        elif event_type == CalendarEventType.DELETED:
            if current_event:
                payload["event"] = deepcopy(current_event)
            if previous_event:
                payload["previous"] = deepcopy(previous_event)

        elif event_type == CalendarEventType.RSVP_CHANGED:
            # RSVP only emits who changed their status and how
            if rsvp_changes:
                # User asked for "rsvp changes (you can make this shape yourself)"
                # Let's keep the existing list of RsvpChangeDTO but it fits the pattern.
                payload["rsvp_changes"] = [change.model_dump() for change in rsvp_changes]

        return payload

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
        if self._should_filter(event_item):
            logger.info(f"Event {entity_id} filtered out because it matches a filter")
            if previous_event is not None:
                cache_mutations.append(self._cache_mutation(calendar_id, entity_id, None))
            return CalendarChangeResult([], cache_mutations)

        version = self._event_version(event_item)
        occurred_at = self._make_occurred_at(event_item, previous_event)

        if self._is_too_old(event_item, previous_event):
            if previous_event is not None:
                cache_mutations.append(self._cache_mutation(calendar_id, entity_id, None))
            return CalendarChangeResult([], cache_mutations)

        _, max_into_future = self._effective_sync_settings(calendar_id)
        if max_into_future is not None:
            if isinstance(max_into_future, str):
                from src.config import parse_interval
                max_into_future = parse_interval(max_into_future)
            if self._is_too_far_future(event_item, float(max_into_future), previous_event):
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
                        )
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
                    value_to_check = organizer.get("email", "") or organizer.get("displayName", "")
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
            if event_item.get("status") == "cancelled":
                continue
            self.set_cache(calendar_id, event_id, event_item)
        self._store_snapshot_state(calendar_id)

    def _rebuild_sync_baseline(self, service, calendar_id: str) -> bool:
        """
        Rebuild the local baseline from current Calendar state, emit nothing,
        and persist a fresh sync token.
        """
        logger.info("Rebuilding calendar sync baseline for %s (calendar: %s)", self.name, calendar_id)

        baseline_time_min = datetime.now(timezone.utc).isoformat()
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
            )

            for event_item in result.get("items", []):
                if not isinstance(event_item, dict):
                    continue

                event_id = event_item.get("id")
                if not isinstance(event_id, str) or not event_id:
                    continue

                if event_item.get("status") == "cancelled":
                    continue

                snapshots[event_id] = event_item

            page_token = result.get("nextPageToken")
            if not page_token:
                new_sync_token = result.get("nextSyncToken")
                break

        if new_sync_token:
            self._replace_calendar_snapshots(calendar_id, snapshots)
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
        baseline_time_min = datetime.now(timezone.utc).isoformat()

        while True:
            result = self._fetch_page(
                service,
                calendar_id=calendar_id,
                sync_token=None,
                page_token=page_token,
                time_min=baseline_time_min,
            )
            for event_item in result.get("items", []):
                if not isinstance(event_item, dict):
                    continue
                event_id = event_item.get("id")
                if isinstance(event_id, str) and event_id:
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
            self.services.kv.set(self.source_id, cursor_key, new_sync_token)
            self._store_sync_fingerprint(calendar_id)

    async def fetch_and_publish_calendar(self, service, calendar_id: str):
        try:
            current_fingerprint = self._sync_fingerprint_payload(calendar_id)
            config_key = self._legacy_sync_config_key(calendar_id)
            fingerprint_key = self._sync_fingerprint_key(calendar_id)
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

                # Identify and emit deletions for events that are now out of range
                if last_fingerprint is not None:
                    try:
                        old_max = last_fingerprint.get("max_into_future")
                        new_max = current_fingerprint.get("max_into_future")
                        if old_max is None or new_max is None:
                            raise ValueError("Missing max_into_future fingerprint value")
                        old_max = float(old_max)
                        new_max = float(new_max)
                        if new_max < old_max:
                            # We decreased the future range, need to cleanup
                            prefix = f"snap:{calendar_id}:"
                            keys = self.services.kv.list_keys_with_prefix(self.source_id, prefix)
                            cleanup_events: list[NewEvent] = []
                            cleanup_mutations: list[CalendarCacheMutation] = []
                            for key in keys:
                                event_id = key.removeprefix(prefix)
                                cached_event = self.get_cached(calendar_id, event_id)
                                if cached_event and self._is_too_far_future(cached_event, new_max):
                                    logger.info("Event %s is now out of range (max_into_future=%s), deleting from cache.", event_id, new_max)
                                    
                                    # Create deletion event before deleting from cache
                                    version = self._event_version(cached_event)
                                    occurred_at = self._make_occurred_at(cached_event)
                                    cleanup_events.append(
                                        self._make_new_event(
                                            calendar_id=calendar_id,
                                            event_type=CalendarEventType.DELETED,
                                            entity_id=event_id,
                                            version=version,
                                            occurred_at=occurred_at,
                                            data=self._make_event_payload(
                                                calendar_id=calendar_id,
                                                event_type=CalendarEventType.DELETED,
                                                current_event=None,
                                                previous_event=cached_event,
                                            ),
                                        )
                                    )
                                    cleanup_mutations.append(self._cache_mutation(calendar_id, event_id, None))
                            
                            if cleanup_events:
                                logger.info("Emitting %d deletion events due to max_into_future change.", len(cleanup_events))
                                self.services.writer.write_events(self.source_id, cleanup_events)
                            self._apply_cache_mutations(calendar_id, cleanup_mutations)

                    except Exception as e:
                        logger.error("Error during max_into_future cleanup: %s", e)
                        raise

                self.services.kv.delete(self.source_id, cursor_key)
                self.services.kv.delete(self.source_id, config_key)
                self.services.kv.delete(self.source_id, fingerprint_key)
                current_sync_token = None

            if not current_sync_token:
                self._rebuild_sync_baseline(service, calendar_id)
                return

            if not self._snapshot_cache_is_trusted(calendar_id):
                logger.warning(
                    "Snapshot cache for %s (calendar: %s) is missing or incomplete; rebuilding baseline without emitted events.",
                    self.name,
                    calendar_id,
                )
                self._rebuild_sync_baseline(service, calendar_id)
                return

            emitted_events: list[NewEvent] = []
            cache_mutations: list[CalendarCacheMutation] = []
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
                    change_result = self._classify_event_change_result(calendar_id, event_item)
                    emitted_events.extend(change_result.events)
                    cache_mutations.extend(change_result.cache_mutations)

                page_token = result.get("nextPageToken")
                if not page_token:
                    new_sync_token = result.get("nextSyncToken", new_sync_token)
                    break

            if emitted_events:
                self.services.writer.write_events(self.source_id, emitted_events)

            self._apply_cache_mutations(calendar_id, cache_mutations)

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
        except Exception as e:
            logger.error(
                "Unexpected error in Calendar source %s (calendar: %s): %s",
                self.name,
                calendar_id,
                e,
                exc_info=True,
            )

    async def fetch_and_publish(self):
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
            await self.fetch_and_publish()
            await asyncio.sleep(self.config.poll_interval)

    async def _cleanup_loop(self):
        """Periodically remove stale snapshots for events too far in the past or future."""
        while True:
            try:
                for calendar_id in self.config.calendar_ids:
                    prefix = f"snap:{calendar_id}:"
                    _, max_into_future = self._effective_sync_settings(calendar_id)
                    max_future_secs: Optional[float] = None
                    if max_into_future is not None:
                        if isinstance(max_into_future, str):
                            from src.config import parse_interval
                            max_future_secs = float(parse_interval(max_into_future))
                        else:
                            max_future_secs = float(max_into_future)

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
                        if max_future_secs and self._is_too_far_future(cached, max_future_secs):
                            self.services.kv.delete(self.source_id, key)
                            deleted_any = True
                    if deleted_any:
                        self._store_snapshot_state(calendar_id)
            except Exception as e:
                logger.error("Error in cleanup loop for %s: %s", self.name, e)
            await asyncio.sleep(12 * 3600)
