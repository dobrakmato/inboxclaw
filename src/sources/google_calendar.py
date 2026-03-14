import asyncio
import logging
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, List

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import BaseModel, Field

from src.config import GoogleCalendarSourceConfig
from src.schemas import NewEvent
from src.services import AppServices
from src.utils.google_auth import get_google_credentials

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
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
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
        kwargs = {
            "calendarId": calendar_id,
            "showDeleted": self.config.show_deleted,
            "singleEvents": self.config.single_events,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        if sync_token:
            kwargs["syncToken"] = sync_token
        if time_min:
            kwargs["timeMin"] = time_min
        
        return service.events().list(**kwargs).execute()

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

    def _is_too_old(self, occurred_at: Optional[datetime]) -> bool:
        if occurred_at is None:
            return False

        max_age_days = self.config.max_event_age_days
        if max_age_days is None:
            return False

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        return occurred_at < cutoff

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
        current_event: Optional[dict[str, Any]] = None,
        previous_event: Optional[dict[str, Any]] = None,
        rsvp_changes: Optional[list[RsvpChangeDTO]] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}

        if current_event is not None:
            payload["event"] = deepcopy(current_event)
        if previous_event is not None:
            payload["previous"] = deepcopy(previous_event)
        if current_event is not None and previous_event is not None:
            payload = {
                "before": deepcopy(previous_event),
                "after": deepcopy(current_event),
            }
        if rsvp_changes:
            payload["changes"] = [change.model_dump() for change in rsvp_changes]

        return payload

    def _make_new_event(
        self,
        *,
        event_type: str,
        entity_id: str,
        version: str,
        occurred_at: Optional[datetime],
        data: dict[str, Any],
    ) -> NewEvent:
        event_name = event_type.split(".")[-1]
        return NewEvent(
            event_id=f"gcal:{entity_id}:{version}:{event_name}",
            event_type=event_type,
            entity_id=entity_id,
            data=data,
            occurred_at=occurred_at,
        )

    def _classify_event_change(self, calendar_id: str, event_item: dict[str, Any]) -> list[NewEvent]:
        entity_id = event_item.get("id")
        if not isinstance(entity_id, str) or not entity_id:
            return []

        previous_event = self.get_cached(calendar_id, entity_id)
        version = self._event_version(event_item)
        occurred_at = self._make_occurred_at(event_item, previous_event)

        if self._is_too_old(occurred_at):
            return []

        status = event_item.get("status")
        if status == "cancelled":
            self.set_cache(calendar_id, entity_id, None)
            return [
                self._make_new_event(
                    event_type=CalendarEventType.DELETED,
                    entity_id=entity_id,
                    version=version,
                    occurred_at=occurred_at,
                    data=self._make_event_payload(
                        current_event=event_item,
                        previous_event=previous_event,
                    ),
                )
            ]

        if previous_event is None:
            self.set_cache(calendar_id, entity_id, event_item)
            return [
                self._make_new_event(
                    event_type=CalendarEventType.CREATED,
                    entity_id=entity_id,
                    version=version,
                    occurred_at=self._make_occurred_at(event_item, prefer_created=True),
                    data=self._make_event_payload(current_event=event_item),
                )
            ]

        emitted: list[NewEvent] = []

        rsvp_changes = self._diff_rsvp(previous_event, event_item)
        if rsvp_changes:
            emitted.append(
                self._make_new_event(
                    event_type=CalendarEventType.RSVP_CHANGED,
                    entity_id=entity_id,
                    version=version,
                    occurred_at=occurred_at,
                    data=self._make_event_payload(
                        current_event=event_item,
                        previous_event=previous_event,
                        rsvp_changes=rsvp_changes,
                    ),
                )
            )

        if self._has_non_rsvp_change(previous_event, event_item):
            emitted.append(
                self._make_new_event(
                    event_type=CalendarEventType.UPDATED,
                    entity_id=entity_id,
                    version=version,
                    occurred_at=occurred_at,
                    data=self._make_event_payload(
                        current_event=event_item,
                        previous_event=previous_event,
                    ),
                )
            )

        self.set_cache(calendar_id, entity_id, event_item)
        return emitted

    def get_cached(self, calendar_id: str, event_id: str) -> Optional[dict[str, Any]]:
        """
        Return the last cached payload for this event ID, or None if missing.
        """
        key = f"snap:{calendar_id}:{event_id}"
        val = self.services.kv.get(self.source_id, key)
        if val:
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                return None
        return None

    def set_cache(self, calendar_id: str, event_id: str, event_payload: Optional[dict[str, Any]]) -> None:
        """
        Store the latest payload for this event ID.
        """
        key = f"snap:{calendar_id}:{event_id}"
        if event_payload is None:
            self.services.kv.delete(self.source_id, key)
        else:
            self.services.kv.set(self.source_id, key, json.dumps(event_payload))

    def _rebuild_sync_baseline(self, service, calendar_id: str) -> bool:
        """
        Rebuild the local baseline from current Calendar state, emit nothing,
        and persist a fresh sync token.
        """
        logger.info("Rebuilding calendar sync baseline for %s (calendar: %s)", self.name, calendar_id)

        baseline_time_min = datetime.now(timezone.utc).isoformat()
        page_token: Optional[str] = None
        new_sync_token: Optional[str] = None

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

                self.set_cache(calendar_id, event_id, event_item)

            page_token = result.get("nextPageToken")
            if not page_token:
                new_sync_token = result.get("nextSyncToken")
                break

        if new_sync_token:
            cursor_key = f"sync_token:{calendar_id}"
            self.services.kv.set(self.source_id, cursor_key, str(new_sync_token))
            logger.info("Calendar sync baseline initialized for %s (calendar: %s)", self.name, calendar_id)
            return True

        return False

    async def fetch_and_publish_calendar(self, service, calendar_id: str):
        try:
            cursor_key = f"sync_token:{calendar_id}"
            current_sync_token = self.services.kv.get(self.source_id, cursor_key)

            if not current_sync_token:
                self._rebuild_sync_baseline(service, calendar_id)
                return

            emitted_events: list[NewEvent] = []
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
                        self._rebuild_sync_baseline(service, calendar_id)
                        return
                    raise

                for event_item in result.get("items", []):
                    if not isinstance(event_item, dict):
                        continue
                    emitted_events.extend(self._classify_event_change(calendar_id, event_item))

                page_token = result.get("nextPageToken")
                if not page_token:
                    new_sync_token = result.get("nextSyncToken", new_sync_token)
                    break

            if emitted_events:
                self.services.writer.write_events(self.source_id, emitted_events)

            if str(new_sync_token) != str(current_sync_token):
                self.services.kv.set(self.source_id, cursor_key, str(new_sync_token))

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
        """Periodically remove old events from the KV cache."""
        while True:
            try:
                max_age = self.config.max_event_age_days
                if max_age is not None:
                    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age)
                    self.services.kv.delete_older_than(self.source_id, cutoff)
            except Exception as e:
                logger.error("Error in Calendar cache cleanup loop for %s: %s", self.name, e)
            
            # Run cleanup once a day or every 12 hours
            await asyncio.sleep(12 * 3600)
