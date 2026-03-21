import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.config import GoogleDriveSourceConfig
from src.schemas import NewEvent
from src.services import AppServices
from src.utils.google_auth import get_google_credentials
from src.utils.google_drive_sync import (
    DriveDebounceManager,
    DriveDebounceState,
    DriveFileSnapshot,
    DriveTransitionClassifier,
    GoogleDriveEventType,
    DriveTextDiffCalculator,
)

logger = logging.getLogger(__name__)


class GoogleDriveSource:
    FILE_SNAPSHOT_PREFIX = "gdrive:file:"
    DEBOUNCE_PREFIX = "gdrive:debounce:"

    def __init__(self, name: str, config: GoogleDriveSourceConfig, services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.token_file = config.token_file
        self.poll_interval = config.poll_interval
        self.classifier = DriveTransitionClassifier()
        self.debounce = DriveDebounceManager()
        self.diff_calc = DriveTextDiffCalculator(
            max_section_chars=config.max_section_chars,
            max_changed_sections=config.max_changed_sections
        )

    def _get_service(self):
        creds = get_google_credentials(self.token_file, self.name)
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def _snapshot_key(self, file_id: str) -> str:
        return f"{self.FILE_SNAPSHOT_PREFIX}{file_id}"

    def _debounce_key(self, file_id: str) -> str:
        return f"{self.DEBOUNCE_PREFIX}{file_id}"

    def _get_cached_snapshot(self, file_id: str) -> Optional[DriveFileSnapshot]:
        raw = self.services.kv.get(self.source_id, self._snapshot_key(file_id))
        if not isinstance(raw, dict):
            return None
        return DriveFileSnapshot.from_dict(raw)

    def _set_cached_snapshot(self, file_id: str, snapshot: DriveFileSnapshot) -> None:
        self.services.kv.set(self.source_id, self._snapshot_key(file_id), snapshot.to_dict())

    def _delete_cached_snapshot(self, file_id: str) -> None:
        self.services.kv.delete(self.source_id, self._snapshot_key(file_id))

    def _get_debounce_state(self, file_id: str) -> Optional[DriveDebounceState]:
        raw = self.services.kv.get(self.source_id, self._debounce_key(file_id))
        if not isinstance(raw, dict):
            return None
        return DriveDebounceState.from_dict(raw)

    def _set_debounce_state(self, file_id: str, state: DriveDebounceState) -> None:
        self.services.kv.set(self.source_id, self._debounce_key(file_id), state.to_dict())

    def _clear_debounce_state(self, file_id: str) -> None:
        self.services.kv.delete(self.source_id, self._debounce_key(file_id))

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
        
        if current:
            if current.modified_time:
                common["modificationDate"] = current.modified_time
            if current.description:
                common["description"] = current.description
            if current.indexable_text:
                common["indexableText"] = current.indexable_text
            if current.last_modifying_user:
                common["lastModifyingUser"] = current.last_modifying_user
            if current.web_view_link:
                common["webViewLink"] = current.web_view_link
            if current.size:
                common["size"] = current.size

        if event_type == GoogleDriveEventType.FILE_CREATED and current is not None:
            return {
                **common,
                "createdTime": current.created_time,
            }

        if event_type == GoogleDriveEventType.FILE_MOVED and previous is not None and current is not None:
            return {
                **common,
                "parentIds": {
                    "before": previous.parents,
                    "after": current.parents,
                }
            }

        if event_type in {GoogleDriveEventType.FILE_TRASHED, GoogleDriveEventType.FILE_UNTRASHED} and previous is not None and current is not None:
            return {
                **common,
                "trashedBefore": previous.trashed,
                "trashedAfter": current.trashed,
            }

        if event_type == GoogleDriveEventType.FILE_SHARED_WITH_YOU and current is not None:
            return {
                **common,
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

        # Remove version fields if present in common
        common.pop("previousVersion", None)
        common.pop("currentVersion", None)
        
        return common

    def _fetch_file(self, service, file_id: str) -> Optional[dict[str, Any]]:
        fields = "id,name,mimeType,parents,trashed,createdTime,modifiedTime,version,ownedByMe,owners(displayName,emailAddress),sharedWithMeTime,sharingUser(displayName,emailAddress),description,contentHints/indexableText,lastModifyingUser(displayName,emailAddress),webViewLink,size"
        try:
            return service.files().get(fileId=file_id, fields=fields, supportsAllDrives=True).execute()
        except HttpError as e:
            if e.resp.status == 404:
                logger.info(f"File {file_id} is not accessible anymore")
                return None
            raise

    def _fetch_text_content(self, service, file_id: str, mime_type: str) -> Optional[str]:
        # Check if MIME type is eligible
        eligible = self.config.eligible_mime_types_for_content_diff
        is_eligible = any(mime_type.startswith(t.replace("*", "")) for t in eligible)
        if not is_eligible and mime_type not in eligible:
            return None

        try:
            if mime_type.startswith("application/vnd.google-apps."):
                if "document" in mime_type:
                    # Google Docs export as Markdown
                    content = service.files().export(fileId=file_id, mimeType="text/markdown").execute()
                    if content is None:
                        return None
                    # Respect max size
                    if len(content) > self.config.max_diffable_file_bytes:
                        logger.info(f"File {file_id} content too large: {len(content)} bytes")
                        return None
                    return content.decode("utf-8") if isinstance(content, bytes) else str(content)
                return None  # Only Docs for now
            else:
                # Regular file
                # We need to know the size before downloading if possible, but files().get() 
                # doesn't give media size unless we fetch metadata first (which we already did)
                content = service.files().get(fileId=file_id, alt="media").execute()
                if content is None:
                    return None
                if len(content) > self.config.max_diffable_file_bytes:
                    logger.info(f"File {file_id} content too large: {len(content)} bytes")
                    return None
                return content.decode("utf-8") if isinstance(content, bytes) else str(content)
        except Exception as e:
            logger.warning(f"Failed to fetch content for {file_id}: {e}")
            return None

    def _process_change(self, service, change: dict[str, Any], now: datetime) -> list[NewEvent]:
        file_id = change.get("fileId")
        if not file_id:
            return []

        removed = bool(change.get("removed", False))
        change_time = change.get("time")
        occurred_at = now
        if isinstance(change_time, str):
            occurred_at = datetime.fromisoformat(change_time.replace("Z", "+00:00"))

        previous = self._get_cached_snapshot(file_id)
        file_resource = None
        current = None
        if not removed:
            file_resource = self._fetch_file(service, file_id)
            if file_resource:
                current = DriveFileSnapshot.from_file_resource(file_resource)

        events: list[NewEvent] = []
        for event_type in self.classifier.classify(
            previous,
            current,
            removed=removed,
        ):
            event_data = self._build_event_data(
                event_type=event_type,
                file_id=file_id,
                previous=previous,
                current=current,
            )
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

        if self.classifier.has_update_signal(previous, current):
            existing_state = self._get_debounce_state(file_id)
            if not existing_state:
                # Save snapshot before update session starts to detect parent changes
                if previous:
                    self.services.kv.set(self.source_id, f"gdrive:snapshot_before_update:{file_id}", previous.to_dict())
                elif current:
                     # This shouldn't happen with has_update_signal, but for safety
                     pass

            dirty_state = self.debounce.mark_dirty(
                existing_state,
                now=now,
                start_version=previous.version if previous else None,
                latest_version=current.version if current else None,
                start_content_snapshot=previous.content_snapshot if previous else None,
            )
            self._set_debounce_state(file_id, dirty_state)

        if current is not None:
            # If it's a text file and we have an update signal or it's new, we might want to fetch content
            is_text = current.mime_type.startswith("text/") or "document" in current.mime_type
            if is_text and (previous is None or self.classifier.has_update_signal(previous, current)):
                content = self._fetch_text_content(service, file_id, current.mime_type)
                if content is not None:
                    current.content_snapshot = content
                    current.content_hash = self.diff_calc.get_hash(content)
                elif previous:
                    # Preserve old content if fetch failed
                    current.content_snapshot = previous.content_snapshot
                    current.content_hash = previous.content_hash

            self._set_cached_snapshot(file_id, current)
        elif removed:
            self._delete_cached_snapshot(file_id)
            self._clear_debounce_state(file_id)

        return events

    def _flush_debounced_updates(self, now: datetime) -> list[NewEvent]:
        events: list[NewEvent] = []
        for key in self.services.kv.list_keys_with_prefix(self.source_id, self.DEBOUNCE_PREFIX):
            file_id = key.replace(self.DEBOUNCE_PREFIX, "", 1)
            state = self._get_debounce_state(file_id)
            if state is None:
                continue
            if not self.debounce.should_flush(
                state,
                now=now,
                quiet_window_seconds=self.config.update_quiet_window,
                max_session_seconds=self.config.update_max_session,
            ):
                continue

            snapshot = self._get_cached_snapshot(file_id)
            if snapshot is None:
                self._clear_debounce_state(file_id)
                continue

            occurred_at = now.astimezone(timezone.utc)
            
            # Load the current snapshot
            current_snapshot = snapshot
            data = {
                **self._common_fields(file_id, current_snapshot),
                "modificationDate": current_snapshot.modified_time,
            }

            data["session"] = {
                "sessionStartedAt": state.session_started_at,
                "lastChangeSeenAt": state.last_change_seen_at,
                "rawChangeCount": state.raw_change_count,
            }

            if current_snapshot.description:
                data["description"] = current_snapshot.description
            if current_snapshot.indexable_text:
                data["indexableText"] = current_snapshot.indexable_text
            if current_snapshot.last_modifying_user:
                data["lastModifyingUser"] = current_snapshot.last_modifying_user

            # Check for parent changes during the update session
            previous_snapshot_data = self.services.kv.get(self.source_id, f"gdrive:snapshot_before_update:{file_id}")
            if previous_snapshot_data and isinstance(previous_snapshot_data, dict):
                prev_parents = sorted(previous_snapshot_data.get("parents", []) or [])
                if prev_parents != current_snapshot.parents:
                    data["parentIds"] = {
                        "before": prev_parents,
                        "after": current_snapshot.parents,
                    }

            # If it's a text file and we have snapshots, compute diff
            is_text = any(current_snapshot.mime_type.startswith(t.replace("*", "")) for t in self.config.eligible_mime_types_for_content_diff) or current_snapshot.mime_type in self.config.eligible_mime_types_for_content_diff
            if is_text and current_snapshot.content_snapshot:
                diff = self.diff_calc.compute_diff(
                    state.start_content_snapshot,
                    current_snapshot.content_snapshot
                )
                data["session"].update(diff)

            events.append(
                NewEvent(
                    event_id=f"drive-{file_id}-{GoogleDriveEventType.FILE_UPDATED}-{state.latest_version or occurred_at.isoformat()}",
                    event_type=GoogleDriveEventType.FILE_UPDATED,
                    entity_id=file_id,
                    occurred_at=occurred_at,
                    data=data,
                )
            )
            self._clear_debounce_state(file_id)
            self.services.kv.delete(self.source_id, f"gdrive:snapshot_before_update:{file_id}")
        return events

    def _bootstrap_repository(self, service):
        """Populates the local cache with the current state of files without emitting events."""
        logger.info(f"Bootstrapping Google Drive source: {self.name} (mode: {self.config.bootstrap_mode})")
        
        query = "trashed = false"
        if self.config.restrict_to_my_drive:
            query += " and 'me' in owners"

        next_page_token = None
        count = 0
        while True:
            results = service.files().list(
                q=query,
                pageSize=100,
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, version, parents, owners(displayName, emailAddress), trashed, sharedWithMeTime)",
                pageToken=next_page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()

            for file_resource in results.get("files", []):
                file_id = file_resource.get("id")
                snapshot = DriveFileSnapshot.from_file_resource(file_resource)
                
                # In full_snapshot mode, we also fetch text content
                if self.config.bootstrap_mode == "full_snapshot":
                    is_text = snapshot.mime_type.startswith("text/") or "document" in snapshot.mime_type
                    if is_text:
                        content = self._fetch_text_content(service, file_id, snapshot.mime_type)
                        if content is not None:
                            snapshot.content_snapshot = content
                            snapshot.content_hash = self.diff_calc.get_hash(content)

                self._set_cached_snapshot(file_id, snapshot)
                count += 1

            next_page_token = results.get("nextPageToken")
            if not next_page_token:
                break
        
        logger.info(f"Finished bootstrapping {self.name}: cached {count} files")

    async def fetch_and_publish(self):
        try:
            service = self._get_service()
            page_token = self.services.cursor.get_last_cursor(self.source_id)

            if not page_token:
                # First time initialization
                if self.config.bootstrap_mode != "off":
                    self._bootstrap_repository(service)

                response = service.changes().getStartPageToken().execute()
                page_token = response.get('startPageToken')
                logger.info(f"Initialized Google Drive startPageToken: {page_token} for {self.name}")
                self.services.cursor.set_cursor(self.source_id, page_token)
                return

            next_page_token = page_token
            new_start_page_token = None
            while next_page_token:
                response = service.changes().list(
                    pageToken=next_page_token,
                    spaces="drive",
                    includeRemoved=self.config.include_removed,
                    includeCorpusRemovals=self.config.include_corpus_removals,
                    restrictToMyDrive=self.config.restrict_to_my_drive,
                    supportsAllDrives=True,
                ).execute()

                now = datetime.now(timezone.utc)
                page_events: list[NewEvent] = []
                for change in response.get("changes", []):
                    page_events.extend(self._process_change(service, change, now))
                if page_events:
                    self.services.writer.write_events(self.source_id, page_events)

                maybe_next = response.get("nextPageToken")
                if maybe_next:
                    next_page_token = maybe_next
                    continue

                new_start_page_token = response.get("newStartPageToken")
                next_page_token = None

            if new_start_page_token:
                self.services.cursor.set_cursor(self.source_id, new_start_page_token)

            flush_events = self._flush_debounced_updates(datetime.now(timezone.utc))
            if flush_events:
                self.services.writer.write_events(self.source_id, flush_events)

        except HttpError as error:
            logger.error(f"An error occurred in Google Drive source {self.name}: {error}")
        except Exception as e:
            logger.error(f"Unexpected error in Google Drive source {self.name}: {e}", exc_info=True)

    async def run(self):
        logger.info(f"Starting Google Drive source: {self.name} polling every {self.poll_interval}")
        while True:
            await self.fetch_and_publish()
            await asyncio.sleep(self.poll_interval)
