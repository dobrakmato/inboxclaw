import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.config import GoogleDriveSourceConfig
from src.schemas import NewEvent
from src.services import AppServices
from src.utils.google_auth import get_google_credentials
from src.utils.filtering import matches_filter
from src.utils.google_drive_sync import (
    DriveFileSnapshot,
    DriveTransitionClassifier,
    GoogleDriveEventType,
    DriveTextDiffCalculator,  # re-exported alias for TextDiffCalculator
)

logger = logging.getLogger(__name__)


@dataclass
class DriveCacheMutation:
    action: str
    file_id: str
    snapshot: Optional[DriveFileSnapshot] = None


class GoogleDriveSource:
    FILE_SNAPSHOT_PREFIX = "gdrive:file:"
    BASELINE_KEY = "gdrive:baseline"
    FILE_FIELDS = (
        "id,name,mimeType,parents,trashed,createdTime,modifiedTime,version,"
        "ownedByMe,owners(displayName,emailAddress),sharedWithMeTime,"
        "sharingUser(displayName,emailAddress),"
        "permissions(type,emailAddress,domain,allowFileDiscovery,permissionDetails),"
        "description,contentHints/indexableText,lastModifyingUser(displayName,emailAddress),"
        "webViewLink,size"
    )

    def __init__(self, name: str, config: GoogleDriveSourceConfig, services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.token_file = config.token_file
        self.poll_interval = config.poll_interval
        self.classifier = DriveTransitionClassifier()
        self.diff_calc = DriveTextDiffCalculator(
            max_section_chars=config.max_section_chars,
            max_changed_sections=config.max_changed_sections
        )

    def _get_service(self):
        creds = get_google_credentials(self.token_file, self.name)
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def _snapshot_key(self, file_id: str) -> str:
        return f"{self.FILE_SNAPSHOT_PREFIX}{file_id}"

    def _get_cached_snapshot(self, file_id: str) -> Optional[DriveFileSnapshot]:
        raw = self.services.kv.get(self.source_id, self._snapshot_key(file_id))
        if not isinstance(raw, dict):
            return None
        return DriveFileSnapshot.from_dict(raw)

    def _set_cached_snapshot(self, file_id: str, snapshot: DriveFileSnapshot) -> None:
        self.services.kv.set(self.source_id, self._snapshot_key(file_id), snapshot.to_dict())

    def _delete_cached_snapshot(self, file_id: str) -> None:
        self.services.kv.delete(self.source_id, self._snapshot_key(file_id))

    def _config_fingerprint(self) -> str:
        filters: list[dict[str, dict[str, Any]]] = []
        for filter_dict in self.config.filters:
            filters.append({
                name: item.model_dump(by_alias=True, exclude_none=True)
                for name, item in filter_dict.items()
            })
        payload = {
            "filters": filters,
            "restrict_to_my_drive": self.config.restrict_to_my_drive,
        }
        return json.dumps(payload, sort_keys=True)

    def _get_baseline_state(self) -> Optional[dict[str, Any]]:
        raw = self.services.kv.get(self.source_id, self.BASELINE_KEY)
        return raw if isinstance(raw, dict) else None

    def _set_baseline_state(self, established_at: datetime, *, trusted: bool = True) -> None:
        self.services.kv.set(
            self.source_id,
            self.BASELINE_KEY,
            {
                "trusted": trusted,
                "established_at": established_at.astimezone(timezone.utc).isoformat(),
                "config_fingerprint": self._config_fingerprint(),
            },
        )

    def _ensure_baseline_state(self, now: datetime) -> None:
        state = self._get_baseline_state()
        if state and state.get("config_fingerprint") == self._config_fingerprint():
            return
        self._set_baseline_state(now)

    @staticmethod
    def _parse_drive_time(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _occurred_at(self, change_time: Optional[str], now: datetime) -> datetime:
        parsed = self._parse_drive_time(change_time)
        return parsed or now

    def _is_after_trusted_baseline(self, value: Optional[str]) -> bool:
        state = self._get_baseline_state()
        if not state or not state.get("trusted"):
            return False

        baseline_at = self._parse_drive_time(state.get("established_at"))
        value_at = self._parse_drive_time(value)
        if baseline_at is None or value_at is None:
            return False
        return value_at > baseline_at

    def _can_emit_created(self, current: DriveFileSnapshot) -> bool:
        if not current.owned_by_me:
            return False
        return self._is_after_trusted_baseline(current.created_time)

    def _can_emit_first_seen_shared(self, current: DriveFileSnapshot) -> bool:
        if current.owned_by_me:
            return False
        return self._is_after_trusted_baseline(current.shared_with_me_time) or self._is_after_trusted_baseline(
            current.created_time
        )

    def _should_filter(self, file_id: str, name: str, parents: Optional[list[str]] = None) -> bool:
        """Check if file matches any configured filters."""
        if not self.config.filters:
            return False

        for filter_dict in self.config.filters:
            for filter_name, f in filter_dict.items():
                if f.in_field == "parent_id":
                    for parent in (parents or []):
                        if matches_filter(parent, f, filter_name):
                            logger.info(f"Filtering out file {file_id} ('{name}') because it matched filter '{filter_name}'")
                            return True
                else:
                    value_to_check = ""
                    if f.in_field == "file_id":
                        value_to_check = file_id
                    elif f.in_field == "name":
                        value_to_check = name

                    if matches_filter(value_to_check, f, filter_name):
                        logger.info(f"Filtering out file {file_id} ('{name}') because it matched filter '{filter_name}'")
                        return True

        return False

    def _is_diffable_mime(self, mime_type: str) -> bool:
        """Check if mime type is eligible for content diffing based on config."""
        eligible = self.config.eligible_mime_types_for_content_diff
        return any(
            mime_type == t or (t.endswith("*") and mime_type.startswith(t.rstrip("*")))
            for t in eligible
        )

    def _apply_cache_mutations(self, mutations: list[DriveCacheMutation]) -> None:
        for mutation in mutations:
            if mutation.action == "set":
                if mutation.snapshot is None:
                    raise ValueError(f"Missing Google Drive snapshot for cache set: {mutation.file_id}")
                self._set_cached_snapshot(mutation.file_id, mutation.snapshot)
            elif mutation.action == "delete":
                self._delete_cached_snapshot(mutation.file_id)
            else:
                raise ValueError(f"Unknown Google Drive cache mutation: {mutation.action}")

    def _content_size_bytes(self, content: Any) -> int:
        if isinstance(content, bytes):
            return len(content)
        return len(str(content).encode("utf-8"))

    def _size_exceeds_diff_limit(self, size: Optional[str]) -> bool:
        if not size:
            return False
        try:
            return int(size) > self.config.max_diffable_file_bytes
        except ValueError:
            return False

    def _prepare_content_snapshot(
        self,
        service,
        file_id: str,
        previous: Optional[DriveFileSnapshot],
        current: DriveFileSnapshot,
    ) -> None:
        if not self._is_diffable_mime(current.mime_type):
            return

        should_fetch = previous is None or self.classifier.has_update_signal(previous, current)
        if should_fetch:
            content = self._fetch_text_content(service, file_id, current.mime_type, current.size)
            if content is not None:
                current.content_snapshot = content
                current.content_hash = self.diff_calc.get_hash(content)
            elif previous:
                current.content_unavailable = True
                current.content_snapshot = previous.content_snapshot
                current.content_hash = previous.content_hash
        elif previous:
            current.content_snapshot = previous.content_snapshot
            current.content_hash = previous.content_hash

    def _event_metadata_fields(self, snapshot: DriveFileSnapshot, *, include_content_hints: bool = False) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if snapshot.modified_time:
            fields["modificationDate"] = snapshot.modified_time
        if snapshot.description:
            fields["description"] = snapshot.description
        if include_content_hints and snapshot.indexable_text:
            fields["indexableText"] = snapshot.indexable_text
        if snapshot.last_modifying_user:
            fields["lastModifyingUser"] = snapshot.last_modifying_user
        if snapshot.web_view_link:
            fields["webViewLink"] = snapshot.web_view_link
        if snapshot.size:
            fields["size"] = snapshot.size
        return fields

    def _build_event(
        self,
        *,
        event_type: str,
        file_id: str,
        occurred_at: datetime,
        change_time: Optional[str] = None,
        event_data: dict[str, Any],
        event_unique: Optional[str] = None,
    ) -> NewEvent:
        unique = event_unique or change_time or occurred_at.isoformat()
        return NewEvent(
            event_id=f"drive-{file_id}-{event_type}-{unique}",
            event_type=event_type,
            entity_id=file_id,
            occurred_at=occurred_at,
            data=event_data,
        )

    @staticmethod
    def _common_fields(file_id: str, snapshot: Optional[DriveFileSnapshot]) -> dict[str, Any]:
        return {
            "fileId": file_id,
            "name": snapshot.name if snapshot else None,
            "mimeType": snapshot.mime_type if snapshot else None,
            "parentIds": snapshot.parents if snapshot else [],
            "owners": snapshot.owners if snapshot and snapshot.owners else [],
        }

    def _build_event_data(
        self,
        *,
        event_type: str,
        file_id: str,
        previous: Optional[DriveFileSnapshot],
        current: Optional[DriveFileSnapshot],
    ) -> dict[str, Any]:
        common = self._common_fields(file_id, current or previous)

        if event_type == GoogleDriveEventType.FILE_CREATED and current is not None:
            return {
                **common,
                **self._event_metadata_fields(current, include_content_hints=True),
                "createdTime": current.created_time,
            }

        if event_type == GoogleDriveEventType.FILE_UPDATED and current is not None:
            data = {
                **common,
                **self._event_metadata_fields(current),
            }
            if previous:
                changes = self.classifier.changed_update_fields(previous, current)
                if changes:
                    data["changes"] = changes
            if (
                previous
                and previous.content_snapshot is not None
                and current.content_snapshot is not None
                and previous.content_hash != current.content_hash
            ):
                try:
                    diff = self.diff_calc.compute_diff(previous.content_snapshot, current.content_snapshot)
                    data["contentDiff"] = diff
                except Exception as e:
                    logger.warning(f"Failed to compute diff for {file_id}, emitting update without diff: {e}")
            return data

        if event_type == GoogleDriveEventType.FILE_MOVED and previous is not None and current is not None:
            return {
                **common,
                **self._event_metadata_fields(current),
                "parentIds": {
                    "before": previous.parents,
                    "after": current.parents,
                }
            }

        if event_type in {GoogleDriveEventType.FILE_TRASHED, GoogleDriveEventType.FILE_UNTRASHED} and previous is not None and current is not None:
            return {
                **common,
                **self._event_metadata_fields(current),
                "trashedBefore": previous.trashed,
                "trashedAfter": current.trashed,
            }

        if event_type == GoogleDriveEventType.FILE_SHARED_WITH_YOU and current is not None:
            return {
                **common,
                **self._event_metadata_fields(current),
                "sharedWithMeTime": current.shared_with_me_time,
                "sharingUser": current.sharing_user,
            }

        if event_type == GoogleDriveEventType.FILE_REMOVED:
            return {
                "fileId": file_id,
                "lastKnownName": previous.name if previous else None,
                "lastKnownMimeType": previous.mime_type if previous else None,
                "lastKnownParentIds": previous.parents if previous else [],
            }

        return common

    def _fetch_file(self, service, file_id: str) -> Optional[dict[str, Any]]:
        try:
            return service.files().get(fileId=file_id, fields=self.FILE_FIELDS, supportsAllDrives=True).execute()
        except HttpError as e:
            if e.resp.status == 404:
                logger.info(f"File {file_id} is not accessible anymore")
                return None
            raise

    def _fetch_text_content(self, service, file_id: str, mime_type: str, size: Optional[str] = None) -> Optional[str]:
        if not self._is_diffable_mime(mime_type):
            return None

        if self._size_exceeds_diff_limit(size):
            logger.info(f"File {file_id} content too large: {size} bytes")
            return None

        try:
            if mime_type.startswith("application/vnd.google-apps."):
                if "document" in mime_type:
                    content = service.files().export(fileId=file_id, mimeType="text/markdown").execute()
                    if content is None:
                        return None
                    content_size = self._content_size_bytes(content)
                    if content_size > self.config.max_diffable_file_bytes:
                        logger.info(f"File {file_id} content too large: {content_size} bytes")
                        return None
                    return content.decode("utf-8") if isinstance(content, bytes) else str(content)
                return None  # Only Docs for now
            else:
                content = service.files().get(fileId=file_id, alt="media").execute()
                if content is None:
                    return None
                content_size = self._content_size_bytes(content)
                if content_size > self.config.max_diffable_file_bytes:
                    logger.info(f"File {file_id} content too large: {content_size} bytes")
                    return None
                return content.decode("utf-8") if isinstance(content, bytes) else str(content)
        except HttpError:
            raise
        except UnicodeDecodeError as e:
            logger.warning(f"Failed to fetch content for {file_id}: {e}")
            return None

    def _process_change_result(
        self,
        service,
        change: dict[str, Any],
        now: datetime,
        *,
        allow_created: Optional[bool] = True,
        allow_first_seen_shared: Optional[bool] = True,
    ) -> tuple[list[NewEvent], list[DriveCacheMutation]]:
        file_id = change.get("fileId")
        if not file_id:
            return [], []

        removed = bool(change.get("removed", False))
        change_time = change.get("time")
        occurred_at = self._occurred_at(change_time, now)

        previous = self._get_cached_snapshot(file_id)
        cache_mutations: list[DriveCacheMutation] = []
        if removed and self._should_filter(
            file_id,
            previous.name if previous else "",
            previous.parents if previous else [],
        ):
            if previous:
                cache_mutations.append(DriveCacheMutation("delete", file_id))
            return [], cache_mutations

        file_resource = None
        current = None
        if not removed:
            file_resource = self._fetch_file(service, file_id)
            if file_resource:
                name = file_resource.get("name", "")
                parents = file_resource.get("parents") or []
                if self._should_filter(file_id, name, parents):
                    if previous:
                        cache_mutations.append(DriveCacheMutation("delete", file_id))
                    return [], cache_mutations
                current = DriveFileSnapshot.from_file_resource(file_resource)
            elif previous:
                # File no longer accessible (404) but was previously tracked — treat as removal
                cache_mutations.append(DriveCacheMutation("delete", file_id))
                return self._build_removed_events(file_id, previous, occurred_at, change_time), cache_mutations

        if current is not None:
            # Filter out non-intentionally shared files
            if not self.classifier.is_intentionally_shared(current):
                # If we previously had it, we should probably delete it from cache
                if previous:
                    cache_mutations.append(DriveCacheMutation("delete", file_id))
                return [], cache_mutations

            self._prepare_content_snapshot(service, file_id, previous, current)

        effective_allow_created = allow_created
        effective_allow_first_seen_shared = allow_first_seen_shared
        if current is not None:
            if effective_allow_created is None:
                effective_allow_created = self._can_emit_created(current)
            if effective_allow_first_seen_shared is None:
                effective_allow_first_seen_shared = self._can_emit_first_seen_shared(current)

        events: list[NewEvent] = []
        for event_type in self.classifier.classify(
            previous,
            current,
            removed=removed,
            allow_created=bool(effective_allow_created),
            allow_first_seen_shared=bool(effective_allow_first_seen_shared),
        ):
            event_data = self._build_event_data(
                event_type=event_type,
                file_id=file_id,
                previous=previous,
                current=current,
            )
            if (
                event_type == GoogleDriveEventType.FILE_UPDATED
                and "changes" not in event_data
                and "contentDiff" not in event_data
            ):
                continue
            unique = change_time or (current.version if current else None)
            events.append(
                self._build_event(
                    event_type=event_type,
                    file_id=file_id,
                    occurred_at=occurred_at,
                    change_time=change_time,
                    event_data=event_data,
                    event_unique=unique,
                )
            )

        if current is not None:
            cache_mutations.append(DriveCacheMutation("set", file_id, current))
        elif removed:
            cache_mutations.append(DriveCacheMutation("delete", file_id))

        return events, cache_mutations

    def _process_change(self, service, change: dict[str, Any], now: datetime) -> list[NewEvent]:
        events, cache_mutations = self._process_change_result(service, change, now)
        self._apply_cache_mutations(cache_mutations)
        return events

    def _build_removed_events(
        self,
        file_id: str,
        previous: DriveFileSnapshot,
        occurred_at: datetime,
        change_time: Optional[str] = None,
    ) -> list[NewEvent]:
        events: list[NewEvent] = []
        for event_type in self.classifier.classify(previous, None, removed=True):
            events.append(
                self._build_event(
                    event_type=event_type,
                    file_id=file_id,
                    occurred_at=occurred_at,
                    change_time=change_time,
                    event_data=self._build_event_data(
                        event_type=event_type,
                        file_id=file_id,
                        previous=previous,
                        current=None,
                    ),
                    event_unique=change_time or previous.version,
                )
            )
        return events

    def _reset_drive_cursor(self, service) -> None:
        response = service.changes().getStartPageToken().execute()
        fresh_page_token = response.get("startPageToken")
        if fresh_page_token:
            self.services.cursor.set_cursor(self.source_id, fresh_page_token)
            self._set_baseline_state(datetime.now(timezone.utc))

    async def fetch_and_publish(self):
        try:
            service = self._get_service()
            page_token = self.services.cursor.get_last_cursor(self.source_id)
            baseline_now = datetime.now(timezone.utc)

            if not page_token:
                self._reset_drive_cursor(service)
                logger.info("Initialized Google Drive startPageToken for %s", self.name)
                return

            self._ensure_baseline_state(baseline_now)

            next_page_token = page_token
            new_start_page_token = None
            while next_page_token:
                try:
                    response = service.changes().list(
                        pageToken=next_page_token,
                        spaces="drive",
                        includeRemoved=True,
                        includeCorpusRemovals=self.config.include_corpus_removals,
                        restrictToMyDrive=self.config.restrict_to_my_drive,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    ).execute()
                except HttpError as error:
                    if error.resp.status == 410:
                        logger.warning(
                            "Google Drive page token expired for %s; resetting change tracking without full Drive scan.",
                            self.name,
                        )
                        self._reset_drive_cursor(service)
                        return
                    raise

                now = datetime.now(timezone.utc)
                page_events: list[NewEvent] = []
                page_cache_mutations: list[DriveCacheMutation] = []
                page_had_processing_error = False
                for change in response.get("changes", []):
                    try:
                        events, cache_mutations = self._process_change_result(
                            service,
                            change,
                            now,
                            allow_created=None,
                            allow_first_seen_shared=None,
                        )
                        page_events.extend(events)
                        page_cache_mutations.extend(cache_mutations)
                    except Exception as e:
                        page_had_processing_error = True
                        file_id = change.get("fileId", "unknown")
                        logger.error(
                            "Failed to process change for file %s in source %s: %s",
                            file_id,
                            self.name,
                            e,
                            exc_info=True,
                        )
                if page_had_processing_error:
                    logger.warning(
                        "Not advancing Google Drive cursor for %s because at least one change failed.",
                        self.name,
                    )
                    return
                if page_events:
                    self.services.writer.write_events(self.source_id, page_events)
                self._apply_cache_mutations(page_cache_mutations)

                maybe_next = response.get("nextPageToken")
                if maybe_next:
                    next_page_token = maybe_next
                    continue

                new_start_page_token = response.get("newStartPageToken")
                next_page_token = None

            if new_start_page_token:
                self.services.cursor.set_cursor(self.source_id, new_start_page_token)

        except HttpError as error:
            logger.error(f"An error occurred in Google Drive source {self.name}: {error}")
        except Exception as e:
            logger.error(f"Unexpected error in Google Drive source {self.name}: {e}", exc_info=True)

    async def run(self):
        logger.info(f"Starting Google Drive source: {self.name} polling every {self.poll_interval}")
        while True:
            await self.fetch_and_publish()
            await asyncio.sleep(self.poll_interval)
