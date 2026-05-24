from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


class GoogleDriveEventType:
    FILE_CREATED = "google.drive.file_created"
    FILE_UPDATED = "google.drive.file_updated"
    FILE_MOVED = "google.drive.file_moved"
    FILE_TRASHED = "google.drive.file_trashed"
    FILE_UNTRASHED = "google.drive.file_untrashed"
    FILE_SHARED_WITH_YOU = "google.drive.file_shared_with_you"
    FILE_REMOVED = "google.drive.file_removed"


@dataclass
class DriveFileSnapshot:
    file_id: str
    name: str
    mime_type: str
    parents: list[str]
    trashed: bool
    created_time: Optional[str]
    modified_time: Optional[str]
    owned_by_me: bool
    owners: Optional[list[dict[str, str]]] = None
    shared_with_me_time: Optional[str] = None
    sharing_user: Optional[dict[str, str]] = None
    permissions: Optional[list[dict[str, Any]]] = None
    description: Optional[str] = None
    indexable_text: Optional[str] = None
    last_modifying_user: Optional[dict[str, str]] = None
    web_view_link: Optional[str] = None
    size: Optional[str] = None
    version: Optional[str] = None
    content_hash: Optional[str] = None
    content_snapshot: Optional[str] = None
    content_unavailable: bool = False

    @classmethod
    def from_file_resource(cls, file_resource: dict[str, Any]) -> "DriveFileSnapshot":
        content_hints = file_resource.get("contentHints", {})
        return cls(
            file_id=file_resource.get("id", ""),
            name=file_resource.get("name", ""),
            mime_type=file_resource.get("mimeType", ""),
            parents=sorted(file_resource.get("parents", []) or []),
            trashed=bool(file_resource.get("trashed", False)),
            created_time=file_resource.get("createdTime"),
            modified_time=file_resource.get("modifiedTime"),
            owned_by_me=bool(file_resource.get("ownedByMe", False)),
            owners=file_resource.get("owners"),
            shared_with_me_time=file_resource.get("sharedWithMeTime"),
            sharing_user=file_resource.get("sharingUser"),
            permissions=file_resource.get("permissions"),
            description=file_resource.get("description"),
            indexable_text=content_hints.get("indexableText"),
            last_modifying_user=file_resource.get("lastModifyingUser"),
            web_view_link=file_resource.get("webViewLink"),
            size=file_resource.get("size"),
            version=file_resource.get("version"),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DriveFileSnapshot":
        return cls(
            file_id=str(data.get("file_id", "")),
            name=str(data.get("name", "")),
            mime_type=str(data.get("mime_type", "")),
            parents=list(data.get("parents", [])),
            trashed=bool(data.get("trashed", False)),
            created_time=data.get("created_time"),
            modified_time=data.get("modified_time"),
            owned_by_me=bool(data.get("owned_by_me", False)),
            owners=data.get("owners"),
            shared_with_me_time=data.get("shared_with_me_time"),
            sharing_user=data.get("sharing_user"),
            permissions=data.get("permissions"),
            description=data.get("description"),
            indexable_text=data.get("indexable_text"),
            last_modifying_user=data.get("last_modifying_user"),
            web_view_link=data.get("web_view_link"),
            size=data.get("size"),
            version=data.get("version"),
            content_hash=data.get("content_hash"),
            content_snapshot=data.get("content_snapshot"),
            content_unavailable=False,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "mime_type": self.mime_type,
            "parents": self.parents,
            "trashed": self.trashed,
            "created_time": self.created_time,
            "modified_time": self.modified_time,
            "owned_by_me": self.owned_by_me,
            "owners": self.owners,
            "shared_with_me_time": self.shared_with_me_time,
            "sharing_user": self.sharing_user,
            "permissions": self.permissions,
            "description": self.description,
            "indexable_text": self.indexable_text,
            "last_modifying_user": self.last_modifying_user,
            "web_view_link": self.web_view_link,
            "size": self.size,
            "version": self.version,
            "content_hash": self.content_hash,
            "content_snapshot": self.content_snapshot,
        }


@dataclass
class DriveDebounceState:
    dirty: bool
    session_started_at: str
    last_change_seen_at: str
    raw_change_count: int
    start_content_snapshot: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DriveDebounceState":
        return cls(
            dirty=bool(data.get("dirty", False)),
            session_started_at=str(data.get("session_started_at", "")),
            last_change_seen_at=str(data.get("last_change_seen_at", "")),
            raw_change_count=int(data.get("raw_change_count", 0)),
            start_content_snapshot=data.get("start_content_snapshot"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dirty": self.dirty,
            "session_started_at": self.session_started_at,
            "last_change_seen_at": self.last_change_seen_at,
            "raw_change_count": self.raw_change_count,
            "start_content_snapshot": self.start_content_snapshot,
        }


class DriveTransitionClassifier:
    UPDATE_FIELD_MAP = (
        ("name", "name"),
        ("mimeType", "mime_type"),
        ("modificationDate", "modified_time"),
        ("description", "description"),
        ("lastModifyingUser", "last_modifying_user"),
        ("webViewLink", "web_view_link"),
        ("size", "size"),
    )

    def classify(
        self,
        previous: Optional[DriveFileSnapshot],
        current: Optional[DriveFileSnapshot],
        *,
        removed: bool,
        allow_created: bool = True,
        allow_first_seen_shared: bool = True,
    ) -> list[str]:
        event_types: list[str] = []

        if removed:
            event_types.append(GoogleDriveEventType.FILE_REMOVED)
            return event_types

        if current is None:
            return event_types
        
        # Filter out non-intentionally shared files
        if not self.is_intentionally_shared(current):
            return event_types

        if previous is None:
            if allow_first_seen_shared and self._is_shared_with_you(current):
                event_types.append(GoogleDriveEventType.FILE_SHARED_WITH_YOU)
            elif allow_created and current.owned_by_me:
                event_types.append(GoogleDriveEventType.FILE_CREATED)
            return event_types

        if previous.parents != current.parents:
            event_types.append(GoogleDriveEventType.FILE_MOVED)

        if not previous.trashed and current.trashed:
            event_types.append(GoogleDriveEventType.FILE_TRASHED)
        if previous.trashed and not current.trashed:
            event_types.append(GoogleDriveEventType.FILE_UNTRASHED)

        if self._shared_with_you_changed(previous, current):
            event_types.append(GoogleDriveEventType.FILE_SHARED_WITH_YOU)

        has_structural_event = any(
            event_type
            in {
                GoogleDriveEventType.FILE_MOVED,
                GoogleDriveEventType.FILE_TRASHED,
                GoogleDriveEventType.FILE_UNTRASHED,
                GoogleDriveEventType.FILE_SHARED_WITH_YOU,
            }
            for event_type in event_types
        )
        if self.has_update_signal(
            previous,
            current,
            allow_modified_time_only=not has_structural_event,
        ):
            event_types.append(GoogleDriveEventType.FILE_UPDATED)

        return event_types

    def is_intentionally_shared(self, snapshot: DriveFileSnapshot) -> bool:
        """Determines if the file is intentionally shared with the user."""
        if snapshot.owned_by_me:
            return True
        
        if snapshot.shared_with_me_time or snapshot.sharing_user:
            return True
         
        if self._has_user_or_group_permission(snapshot):
            return True
         
        return False
 
    def has_update_signal(
        self,
        previous: Optional[DriveFileSnapshot],
        current: Optional[DriveFileSnapshot],
        *,
        allow_modified_time_only: bool = True,
    ) -> bool:
        if previous is None or current is None:
            return False
         
        # Don't emit updates for folders
        if current.mime_type == "application/vnd.google-apps.folder":
            return False
            
        changes = self.changed_update_fields(previous, current)
        non_time_changes = {key: value for key, value in changes.items() if key != "modificationDate"}
        if non_time_changes:
            return True
 
        if previous.content_hash is not None and current.content_hash is not None:
            if previous.content_hash != current.content_hash:
                return True
            if not getattr(current, "content_unavailable", False):
                return False
 
        return allow_modified_time_only and previous.modified_time != current.modified_time

    def changed_update_fields(self, previous: DriveFileSnapshot, current: DriveFileSnapshot) -> dict[str, dict[str, Any]]:
        changes: dict[str, dict[str, Any]] = {}
        for event_field, snapshot_field in self.UPDATE_FIELD_MAP:
            before = getattr(previous, snapshot_field)
            after = getattr(current, snapshot_field)
            if before != after:
                changes[event_field] = {"before": before, "after": after}
        return changes

    @staticmethod
    def _shared_with_you_changed(previous: DriveFileSnapshot, current: DriveFileSnapshot) -> bool:
        if current.owned_by_me:
            return False
        if not DriveTransitionClassifier._is_shared_with_you(current):
            return False
        if previous.owned_by_me:
            return True
        if current.shared_with_me_time:
            if not previous.shared_with_me_time:
                return True
            return current.shared_with_me_time > previous.shared_with_me_time
        if current.sharing_user and not previous.sharing_user:
            return True
        return not DriveTransitionClassifier._is_shared_with_you(previous)
 
    @staticmethod
    def _is_shared_with_you(snapshot: DriveFileSnapshot) -> bool:
        return not snapshot.owned_by_me and bool(
            snapshot.shared_with_me_time
            or snapshot.sharing_user
            or DriveTransitionClassifier._has_user_or_group_permission(snapshot)
        )

    @staticmethod
    def _has_user_or_group_permission(snapshot: DriveFileSnapshot) -> bool:
        if not snapshot.permissions:
            return False

        for perm in snapshot.permissions:
            p_type = perm.get("type")
            if p_type in ("user", "group"):
                return True

            details = perm.get("permissionDetails", [])
            for detail in details:
                if detail.get("permissionType") in ("user", "group"):
                    return True

        return False


class DriveDebounceManager:
    def mark_dirty(
        self,
        existing: Optional[DriveDebounceState],
        *,
        now: datetime,
        start_content_snapshot: Optional[str] = None,
    ) -> DriveDebounceState:
        now_iso = now.astimezone(timezone.utc).isoformat()
        session_started_at = existing.session_started_at if existing else now_iso
        first_content = existing.start_content_snapshot if existing else start_content_snapshot
        return DriveDebounceState(
            dirty=True,
            session_started_at=session_started_at,
            last_change_seen_at=now_iso,
            raw_change_count=(existing.raw_change_count + 1) if existing else 1,
            start_content_snapshot=first_content,
        )

    def should_flush(self, state: DriveDebounceState, *, now: datetime, quiet_window_seconds: float, max_session_seconds: float) -> bool:
        if not state.dirty:
            return False

        now_utc = now.astimezone(timezone.utc)
        last_change_seen = datetime.fromisoformat(state.last_change_seen_at)
        session_started = datetime.fromisoformat(state.session_started_at)
        return (now_utc - last_change_seen).total_seconds() >= quiet_window_seconds or (now_utc - session_started).total_seconds() >= max_session_seconds


# Backward-compatible alias – the implementation now lives in src.utils.text_diff
from src.utils.text_diff import TextDiffCalculator as DriveTextDiffCalculator  # noqa: F401
