import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.config import GoogleDriveSourceConfig
from src.schemas import NewEvent
from src.services import AppServices
from src.utils.google_auth import get_google_credentials
from src.utils.filtering import matches_filter
from src.utils.google_drive_client import AsyncGoogleDriveClient, DriveApiError
from src.utils.google_drive_sync import (
    DriveFileSnapshot,
    DriveTransitionClassifier,
    GoogleDriveEventType,
    DriveTextDiffCalculator,  # re-exported alias for TextDiffCalculator
)

logger = logging.getLogger(__name__)

GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
PERMANENT_CONTENT_UNAVAILABLE_REASONS = frozenset({"exportSizeLimitExceeded"})


@dataclass
class DriveCacheMutation:
    action: str
    file_id: str
    snapshot: Optional[DriveFileSnapshot] = None


class GoogleDriveSource:
    FILE_SNAPSHOT_PREFIX = "gdrive:file:"
    SHARED_DRIVE_CURSOR_PREFIX = "gdrive:shared-drive-cursor:"
    BASELINE_KEY = "gdrive:baseline"
    FILE_FIELDS = (
        "id,name,mimeType,parents,trashed,createdTime,modifiedTime,version,"
        "ownedByMe,owners(displayName,emailAddress),sharedWithMeTime,"
        "sharingUser(displayName,emailAddress),"
        "permissions(type,emailAddress,domain,allowFileDiscovery,permissionDetails),"
        "description,contentHints/indexableText,lastModifyingUser(displayName,emailAddress),"
        "webViewLink,size,capabilities(canDownload),driveId"
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
        unsupported_google_mimes = [
            mime_type
            for mime_type in config.eligible_mime_types_for_content_diff
            if mime_type.startswith("application/vnd.google-apps.")
            and mime_type != GOOGLE_DOC_MIME_TYPE
        ]
        if unsupported_google_mimes:
            logger.warning(
                "Google Drive source %s ignores unsupported native MIME types for content diffing: %s",
                self.name,
                ", ".join(unsupported_google_mimes),
            )

    def _get_client(self) -> AsyncGoogleDriveClient:
        def load_credentials(force_refresh: bool):
            return get_google_credentials(
                self.token_file,
                self.name,
                force_refresh=force_refresh,
            )

        return AsyncGoogleDriveClient(load_credentials)

    def _snapshot_key(self, file_id: str) -> str:
        return f"{self.FILE_SNAPSHOT_PREFIX}{file_id}"

    def _shared_drive_cursor_key(self, drive_id: str) -> str:
        return f"{self.SHARED_DRIVE_CURSOR_PREFIX}{drive_id}"

    def _get_shared_drive_cursors(self) -> dict[str, str]:
        cursors: dict[str, str] = {}
        for key in self.services.kv.list_keys_with_prefix(
            self.source_id,
            self.SHARED_DRIVE_CURSOR_PREFIX,
        ):
            drive_id = key[len(self.SHARED_DRIVE_CURSOR_PREFIX):]
            value = self.services.kv.get(self.source_id, key)
            if drive_id and isinstance(value, str) and value:
                cursors[drive_id] = value
        return cursors

    def _get_cached_snapshot(self, file_id: str) -> Optional[DriveFileSnapshot]:
        raw = self.services.kv.get(self.source_id, self._snapshot_key(file_id))
        if not isinstance(raw, dict):
            return None
        return DriveFileSnapshot.from_dict(raw)

    def _set_cached_snapshot(
        self,
        file_id: str,
        snapshot: DriveFileSnapshot,
        *,
        session: Optional[Session] = None,
    ) -> None:
        key = self._snapshot_key(file_id)
        if session is None:
            self.services.kv.set(self.source_id, key, snapshot.to_dict())
        else:
            self.services.kv.set_in_session(session, self.source_id, key, snapshot.to_dict())

    def _delete_cached_snapshot(self, file_id: str, *, session: Optional[Session] = None) -> None:
        key = self._snapshot_key(file_id)
        if session is None:
            self.services.kv.delete(self.source_id, key)
        else:
            self.services.kv.delete_in_session(session, self.source_id, key)

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

    def _set_baseline_state(
        self,
        established_at: datetime,
        *,
        trusted: bool = True,
        session: Optional[Session] = None,
    ) -> None:
        value = {
            "trusted": trusted,
            "established_at": established_at.astimezone(timezone.utc).isoformat(),
            "config_fingerprint": self._config_fingerprint(),
        }
        if session is None:
            self.services.kv.set(self.source_id, self.BASELINE_KEY, value)
        else:
            self.services.kv.set_in_session(session, self.source_id, self.BASELINE_KEY, value)

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
        if mime_type.startswith("application/vnd.google-apps."):
            return mime_type == GOOGLE_DOC_MIME_TYPE and mime_type in self.config.eligible_mime_types_for_content_diff
        eligible = self.config.eligible_mime_types_for_content_diff
        return any(
            mime_type == t or (t.endswith("*") and mime_type.startswith(t.rstrip("*")))
            for t in eligible
        )

    def _apply_cache_mutations(
        self,
        mutations: list[DriveCacheMutation],
        *,
        session: Optional[Session] = None,
    ) -> None:
        for mutation in mutations:
            if mutation.action == "set":
                if mutation.snapshot is None:
                    raise ValueError(f"Missing Google Drive snapshot for cache set: {mutation.file_id}")
                if session is None:
                    self._set_cached_snapshot(mutation.file_id, mutation.snapshot)
                else:
                    self._set_cached_snapshot(mutation.file_id, mutation.snapshot, session=session)
            elif mutation.action == "delete":
                if session is None:
                    self._delete_cached_snapshot(mutation.file_id)
                else:
                    self._delete_cached_snapshot(mutation.file_id, session=session)
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

    async def _prepare_content_snapshot(
        self,
        client: AsyncGoogleDriveClient,
        file_id: str,
        previous: Optional[DriveFileSnapshot],
        current: DriveFileSnapshot,
    ) -> None:
        if not self._is_diffable_mime(current.mime_type):
            return

        should_fetch = previous is None or self.classifier.has_update_signal(previous, current)
        if should_fetch:
            content = await self._fetch_text_content(
                client,
                file_id,
                current.mime_type,
                current.size,
                can_download=current.can_download,
            )
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

    def _events_for_transition(
        self,
        *,
        file_id: str,
        previous: Optional[DriveFileSnapshot],
        current: Optional[DriveFileSnapshot],
        removed: bool,
        occurred_at: datetime,
        change_time: Optional[str],
        allow_created: bool,
        allow_first_seen_shared: bool,
    ) -> list[NewEvent]:
        events: list[NewEvent] = []
        for event_type in self.classifier.classify(
            previous,
            current,
            removed=removed,
            allow_created=allow_created,
            allow_first_seen_shared=allow_first_seen_shared,
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
        return events

    async def _fetch_file(
        self,
        client: AsyncGoogleDriveClient,
        file_id: str,
    ) -> Optional[dict[str, Any]]:
        try:
            return await client.get_file(file_id, fields=self.FILE_FIELDS)
        except DriveApiError as error:
            if error.status == 404:
                logger.info(f"File {file_id} is not accessible anymore")
                return None
            raise

    async def _fetch_text_content(
        self,
        client: AsyncGoogleDriveClient,
        file_id: str,
        mime_type: str,
        size: Optional[str] = None,
        *,
        can_download: Optional[bool] = None,
    ) -> Optional[str]:
        if not self._is_diffable_mime(mime_type):
            return None

        if can_download is False:
            logger.info("File %s content cannot be downloaded by the current user", file_id)
            return None

        if self._size_exceeds_diff_limit(size):
            logger.info(f"File {file_id} content too large: {size} bytes")
            return None

        try:
            if mime_type == GOOGLE_DOC_MIME_TYPE:
                content = await client.export_file(file_id, mime_type="text/markdown")
            else:
                content = await client.download_file(file_id)
        except DriveApiError as error:
            if error.reasons & PERMANENT_CONTENT_UNAVAILABLE_REASONS:
                logger.warning(
                    "Skipping content diff for file %s because Drive cannot export it: %s",
                    file_id,
                    error.message,
                )
                return None
            raise

        content_size = self._content_size_bytes(content)
        if content_size > self.config.max_diffable_file_bytes:
            logger.info(f"File {file_id} content too large: {content_size} bytes")
            return None
        try:
            return content.decode("utf-8") if isinstance(content, bytes) else str(content)
        except UnicodeDecodeError as e:
            logger.warning(f"Failed to fetch content for {file_id}: {e}")
            return None

    async def _process_change_result(
        self,
        client: AsyncGoogleDriveClient,
        change: dict[str, Any],
        now: datetime,
        *,
        allow_created: Optional[bool] = True,
        allow_first_seen_shared: Optional[bool] = True,
        snapshot_overrides: Optional[dict[str, Optional[DriveFileSnapshot]]] = None,
    ) -> tuple[list[NewEvent], list[DriveCacheMutation]]:
        file_id = change.get("fileId")
        if not file_id:
            return [], []

        removed = bool(change.get("removed", False))
        change_time = change.get("time")
        occurred_at = self._occurred_at(change_time, now)

        if snapshot_overrides is not None and file_id in snapshot_overrides:
            previous = snapshot_overrides[file_id]
        else:
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
            file_resource = await self._fetch_file(client, file_id)
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

            await self._prepare_content_snapshot(client, file_id, previous, current)

        effective_allow_created = allow_created
        effective_allow_first_seen_shared = allow_first_seen_shared
        if current is not None:
            if effective_allow_created is None:
                effective_allow_created = self._can_emit_created(current)
            if effective_allow_first_seen_shared is None:
                effective_allow_first_seen_shared = self._can_emit_first_seen_shared(current)

        events = self._events_for_transition(
            file_id=file_id,
            previous=previous,
            current=current,
            removed=removed,
            occurred_at=occurred_at,
            change_time=change_time,
            allow_created=bool(effective_allow_created),
            allow_first_seen_shared=bool(effective_allow_first_seen_shared),
        )

        if current is not None:
            cache_mutations.append(DriveCacheMutation("set", file_id, current))
        elif removed:
            cache_mutations.append(DriveCacheMutation("delete", file_id))

        return events, cache_mutations

    @staticmethod
    def _stage_cache_mutations(
        staged: dict[str, Optional[DriveFileSnapshot]],
        mutations: list[DriveCacheMutation],
    ) -> None:
        for mutation in mutations:
            if mutation.action == "set":
                if mutation.snapshot is None:
                    raise ValueError(
                        f"Missing Google Drive snapshot for staged cache set: {mutation.file_id}"
                    )
                staged[mutation.file_id] = mutation.snapshot
            elif mutation.action == "delete":
                staged[mutation.file_id] = None
            else:
                raise ValueError(f"Unknown Google Drive cache mutation: {mutation.action}")

    async def _process_change(
        self,
        client: AsyncGoogleDriveClient,
        change: dict[str, Any],
        now: datetime,
    ) -> list[NewEvent]:
        events, cache_mutations = await self._process_change_result(client, change, now)
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

    def _commit_page(
        self,
        events: list[NewEvent],
        cache_mutations: list[DriveCacheMutation],
        cursor: Optional[str],
        *,
        baseline_at: Optional[datetime] = None,
        kv_updates: Optional[dict[str, Any]] = None,
        kv_deletes: Optional[list[str]] = None,
    ) -> None:
        with self.services.db_session_maker() as session:
            with session.begin():
                new_event_count = self.services.writer.write_events_in_session(
                    session,
                    self.source_id,
                    events,
                    use_savepoints=False,
                )
                self._apply_cache_mutations(cache_mutations, session=session)
                if cursor is not None:
                    self.services.cursor.set_cursor_in_session(session, self.source_id, cursor)
                for key, value in (kv_updates or {}).items():
                    self.services.kv.set_in_session(session, self.source_id, key, value)
                for key in kv_deletes or []:
                    self.services.kv.delete_in_session(session, self.source_id, key)
                if baseline_at is not None:
                    self._set_baseline_state(baseline_at, session=session)

        if new_event_count > 0:
            self.services.notifier.notify()
            logger.info(
                "Committed %s new Google Drive events for source %s",
                new_event_count,
                self.source_id,
            )

    async def _reset_drive_cursor(self, client: AsyncGoogleDriveClient) -> None:
        fresh_page_token = await client.get_start_page_token()
        shared_drive_cursors: dict[str, str] = {}
        shared_drive_snapshots: list[DriveCacheMutation] = []
        if not self.config.restrict_to_my_drive:
            for drive_id in await self._list_shared_drive_ids(client):
                shared_drive_cursors[self._shared_drive_cursor_key(drive_id)] = (
                    await client.get_start_page_token(drive_id=drive_id)
                )
                for resource in await self._list_drive_files(client, drive_id=drive_id):
                    file_id = str(resource.get("id") or "")
                    if not file_id:
                        continue
                    name = str(resource.get("name") or "")
                    parents = resource.get("parents") or []
                    if self._should_filter(file_id, name, parents):
                        continue
                    shared_drive_snapshots.append(
                        DriveCacheMutation(
                            "set",
                            file_id,
                            DriveFileSnapshot.from_file_resource(resource),
                        )
                    )
        self._commit_page(
            [],
            shared_drive_snapshots,
            fresh_page_token,
            baseline_at=datetime.now(timezone.utc),
            kv_updates=shared_drive_cursors,
        )

    async def _list_drive_files(
        self,
        client: AsyncGoogleDriveClient,
        *,
        drive_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page_token: Optional[str] = None
        while True:
            response = await client.list_files_page(
                fields=self.FILE_FIELDS,
                page_token=page_token,
                drive_id=drive_id,
            )
            if response.get("incompleteSearch"):
                raise RuntimeError(
                    "Google Drive returned an incomplete file listing during reconciliation"
                )
            files.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return files

    async def _list_shared_drive_ids(self, client: AsyncGoogleDriveClient) -> list[str]:
        drive_ids: list[str] = []
        page_token: Optional[str] = None
        while True:
            response = await client.list_drives_page(page_token=page_token)
            drive_ids.extend(
                str(drive["id"])
                for drive in response.get("drives", [])
                if drive.get("id")
            )
            page_token = response.get("nextPageToken")
            if not page_token:
                return drive_ids

    @staticmethod
    def _ids_reachable_from_my_drive_root(
        resources: list[dict[str, Any]],
        root_id: str,
    ) -> set[str]:
        by_id = {
            str(resource.get("id")): resource
            for resource in resources
            if resource.get("id")
        }
        reachable: set[str] = {root_id}
        unresolved = set(by_id) - reachable
        changed = True
        while changed:
            changed = False
            for file_id in list(unresolved):
                resource = by_id[file_id]
                if resource.get("driveId"):
                    unresolved.remove(file_id)
                    continue
                parents = resource.get("parents") or []
                if any(parent in reachable for parent in parents):
                    reachable.add(file_id)
                    unresolved.remove(file_id)
                    changed = True
        return reachable

    async def _reconcile_expired_page_token(
        self,
        client: AsyncGoogleDriveClient,
        *,
        drive_id: Optional[str] = None,
    ) -> None:
        if drive_id:
            fresh_page_token = await client.get_start_page_token(drive_id=drive_id)
            resources = await self._list_drive_files(client, drive_id=drive_id)
        else:
            fresh_page_token = await client.get_start_page_token()
            resources = await self._list_drive_files(client)
        tracked_shared_drive_ids = set(self._get_shared_drive_cursors())
        if drive_id:
            included_ids = {
                str(resource.get("id"))
                for resource in resources
                if resource.get("id")
            }
        elif self.config.restrict_to_my_drive:
            root_resource = await client.get_file("root", fields="id")
            root_id = str(root_resource["id"])
            included_ids = self._ids_reachable_from_my_drive_root(resources, root_id)
        else:
            included_ids = {
                str(resource.get("id"))
                for resource in resources
                if resource.get("id")
                and resource.get("driveId") not in tracked_shared_drive_ids
            }

        previous_by_id: dict[str, DriveFileSnapshot] = {}
        for key in self.services.kv.list_keys_with_prefix(self.source_id, self.FILE_SNAPSHOT_PREFIX):
            file_id = key[len(self.FILE_SNAPSHOT_PREFIX):]
            snapshot = self._get_cached_snapshot(file_id)
            if snapshot is None:
                continue
            if drive_id and snapshot.drive_id != drive_id:
                continue
            if (
                drive_id is None
                and not self.config.restrict_to_my_drive
                and snapshot.drive_id in tracked_shared_drive_ids
            ):
                continue
            previous_by_id[file_id] = snapshot

        now = datetime.now(timezone.utc)
        current_by_id: dict[str, DriveFileSnapshot] = {}
        excluded_ids: set[str] = set()
        for resource in resources:
            file_id = str(resource.get("id") or "")
            if not file_id or file_id not in included_ids:
                if file_id:
                    excluded_ids.add(file_id)
                continue
            name = str(resource.get("name") or "")
            parents = resource.get("parents") or []
            current = DriveFileSnapshot.from_file_resource(resource)
            if self._should_filter(file_id, name, parents) or not self.classifier.is_intentionally_shared(current):
                excluded_ids.add(file_id)
                continue
            current_by_id[file_id] = current

        events: list[NewEvent] = []
        mutations: list[DriveCacheMutation] = []
        for file_id, current in current_by_id.items():
            previous = previous_by_id.get(file_id)
            if previous is not None:
                await self._prepare_content_snapshot(client, file_id, previous, current)
            change_time = current.modified_time or current.created_time
            events.extend(
                self._events_for_transition(
                    file_id=file_id,
                    previous=previous,
                    current=current,
                    removed=False,
                    occurred_at=self._occurred_at(change_time, now),
                    change_time=change_time,
                    allow_created=self._can_emit_created(current),
                    allow_first_seen_shared=self._can_emit_first_seen_shared(current),
                )
            )
            mutations.append(DriveCacheMutation("set", file_id, current))

        for file_id, previous in previous_by_id.items():
            if file_id in current_by_id:
                continue
            if file_id not in excluded_ids and not self._should_filter(
                file_id,
                previous.name,
                previous.parents,
            ):
                events.extend(self._build_removed_events(file_id, previous, now))
            mutations.append(DriveCacheMutation("delete", file_id))

        if drive_id:
            self._commit_page(
                events,
                mutations,
                None,
                kv_updates={self._shared_drive_cursor_key(drive_id): fresh_page_token},
            )
        else:
            self._commit_page(
                events,
                mutations,
                fresh_page_token,
                baseline_at=now,
            )
        logger.info(
            "Reconciled Google Drive source %s log %s after page-token expiry: %s current files, %s events",
            self.name,
            drive_id or "user",
            len(current_by_id),
            len(events),
        )

    async def _initialize_shared_drive(
        self,
        client: AsyncGoogleDriveClient,
        drive_id: str,
    ) -> str:
        fresh_page_token = await client.get_start_page_token(drive_id=drive_id)
        resources = await self._list_drive_files(client, drive_id=drive_id)
        mutations: list[DriveCacheMutation] = []
        for resource in resources:
            file_id = str(resource.get("id") or "")
            if not file_id:
                continue
            name = str(resource.get("name") or "")
            parents = resource.get("parents") or []
            snapshot = DriveFileSnapshot.from_file_resource(resource)
            if self._should_filter(file_id, name, parents):
                continue
            mutations.append(DriveCacheMutation("set", file_id, snapshot))

        self._commit_page(
            [],
            mutations,
            None,
            kv_updates={self._shared_drive_cursor_key(drive_id): fresh_page_token},
        )
        logger.info(
            "Initialized shared Drive %s for source %s with %s cached files",
            drive_id,
            self.name,
            len(mutations),
        )
        return fresh_page_token

    def _remove_shared_drive(self, drive_id: str) -> None:
        now = datetime.now(timezone.utc)
        events: list[NewEvent] = []
        mutations: list[DriveCacheMutation] = []
        for key in self.services.kv.list_keys_with_prefix(
            self.source_id,
            self.FILE_SNAPSHOT_PREFIX,
        ):
            file_id = key[len(self.FILE_SNAPSHOT_PREFIX):]
            previous = self._get_cached_snapshot(file_id)
            if previous is None or previous.drive_id != drive_id:
                continue
            if not self._should_filter(file_id, previous.name, previous.parents):
                events.extend(
                    self._build_removed_events(
                        file_id,
                        previous,
                        now,
                        change_time=now.isoformat(),
                    )
                )
            mutations.append(DriveCacheMutation("delete", file_id))

        self._commit_page(
            events,
            mutations,
            None,
            kv_deletes=[self._shared_drive_cursor_key(drive_id)],
        )
        logger.info(
            "Removed inaccessible shared Drive %s from source %s with %s file deletions",
            drive_id,
            self.name,
            len(events),
        )

    async def _sync_shared_drive_membership(
        self,
        client: AsyncGoogleDriveClient,
    ) -> dict[str, str]:
        current_drive_ids = set(await self._list_shared_drive_ids(client))
        cursors = self._get_shared_drive_cursors()

        for drive_id in sorted(set(cursors) - current_drive_ids):
            self._remove_shared_drive(drive_id)
            cursors.pop(drive_id, None)

        for drive_id in sorted(current_drive_ids - set(cursors)):
            cursors[drive_id] = await self._initialize_shared_drive(client, drive_id)

        return cursors

    async def _drain_change_log(
        self,
        client: AsyncGoogleDriveClient,
        page_token: str,
        *,
        drive_id: Optional[str] = None,
    ) -> bool:
        next_page_token: Optional[str] = page_token
        while next_page_token:
            try:
                list_kwargs: dict[str, Any] = {
                    "include_corpus_removals": self.config.include_corpus_removals,
                    "restrict_to_my_drive": self.config.restrict_to_my_drive,
                }
                if drive_id:
                    list_kwargs["drive_id"] = drive_id
                response = await client.list_changes(next_page_token, **list_kwargs)
            except DriveApiError as error:
                if error.status == 410:
                    logger.warning(
                        "Google Drive page token expired for %s log %s; reconciling current state.",
                        self.name,
                        drive_id or "user",
                    )
                    if drive_id:
                        await self._reconcile_expired_page_token(client, drive_id=drive_id)
                    else:
                        await self._reconcile_expired_page_token(client)
                    return True
                raise

            now = datetime.now(timezone.utc)
            page_events: list[NewEvent] = []
            page_cache_mutations: list[DriveCacheMutation] = []
            staged_snapshots: dict[str, Optional[DriveFileSnapshot]] = {}
            page_had_processing_error = False
            for change in response.get("changes", []):
                try:
                    events, cache_mutations = await self._process_change_result(
                        client,
                        change,
                        now,
                        allow_created=None,
                        allow_first_seen_shared=None,
                        snapshot_overrides=staged_snapshots,
                    )
                    page_events.extend(events)
                    page_cache_mutations.extend(cache_mutations)
                    self._stage_cache_mutations(staged_snapshots, cache_mutations)
                except Exception as error:
                    page_had_processing_error = True
                    file_id = change.get("fileId", "unknown")
                    logger.error(
                        "Failed to process change for file %s in source %s log %s: %s",
                        file_id,
                        self.name,
                        drive_id or "user",
                        error,
                        exc_info=True,
                    )
            if page_had_processing_error:
                logger.warning(
                    "Not advancing Google Drive checkpoint for %s log %s because at least one change failed.",
                    self.name,
                    drive_id or "user",
                )
                return False

            checkpoint = response.get("nextPageToken") or response.get("newStartPageToken")
            if not checkpoint:
                raise RuntimeError("Google Drive changes response did not include a checkpoint token")
            if drive_id:
                self._commit_page(
                    page_events,
                    page_cache_mutations,
                    None,
                    kv_updates={self._shared_drive_cursor_key(drive_id): str(checkpoint)},
                )
            else:
                self._commit_page(page_events, page_cache_mutations, str(checkpoint))
            next_page_token = response.get("nextPageToken")

        return True

    async def fetch_and_publish(self):
        try:
            async with self._get_client() as client:
                page_token = self.services.cursor.get_last_cursor(self.source_id)
                baseline_now = datetime.now(timezone.utc)

                if not page_token:
                    await self._reset_drive_cursor(client)
                    logger.info("Initialized Google Drive startPageToken for %s", self.name)
                    return

                self._ensure_baseline_state(baseline_now)

                if not await self._drain_change_log(client, page_token):
                    return

                if not self.config.restrict_to_my_drive:
                    shared_drive_cursors = await self._sync_shared_drive_membership(client)
                    for drive_id, shared_page_token in sorted(shared_drive_cursors.items()):
                        if not await self._drain_change_log(
                            client,
                            shared_page_token,
                            drive_id=drive_id,
                        ):
                            return
        except DriveApiError as error:
            logger.error(f"An error occurred in Google Drive source {self.name}: {error}")
        except Exception as e:
            logger.error(f"Unexpected error in Google Drive source {self.name}: {e}", exc_info=True)

    async def run(self):
        logger.info(f"Starting Google Drive source: {self.name} polling every {self.poll_interval}")
        while True:
            await self.fetch_and_publish()
            await asyncio.sleep(self.poll_interval)
