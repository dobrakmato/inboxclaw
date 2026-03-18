from __future__ import annotations
import difflib
import hashlib
import re
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
    version: Optional[str]
    owned_by_me: bool
    owners: Optional[list[dict[str, str]]] = None
    shared_with_me_time: Optional[str] = None
    sharing_user: Optional[dict[str, str]] = None
    description: Optional[str] = None
    indexable_text: Optional[str] = None
    last_modifying_user: Optional[dict[str, str]] = None
    content_hash: Optional[str] = None
    content_snapshot: Optional[str] = None

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
            version=str(file_resource.get("version")) if file_resource.get("version") is not None else None,
            owned_by_me=bool(file_resource.get("ownedByMe", False)),
            owners=file_resource.get("owners"),
            shared_with_me_time=file_resource.get("sharedWithMeTime"),
            sharing_user=file_resource.get("sharingUser"),
            description=file_resource.get("description"),
            indexable_text=content_hints.get("indexableText"),
            last_modifying_user=file_resource.get("lastModifyingUser"),
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
            version=data.get("version"),
            owned_by_me=bool(data.get("owned_by_me", False)),
            owners=data.get("owners"),
            shared_with_me_time=data.get("shared_with_me_time"),
            sharing_user=data.get("sharing_user"),
            description=data.get("description"),
            indexable_text=data.get("indexable_text"),
            last_modifying_user=data.get("last_modifying_user"),
            content_hash=data.get("content_hash"),
            content_snapshot=data.get("content_snapshot"),
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
            "version": self.version,
            "owned_by_me": self.owned_by_me,
            "owners": self.owners,
            "shared_with_me_time": self.shared_with_me_time,
            "sharing_user": self.sharing_user,
            "description": self.description,
            "indexable_text": self.indexable_text,
            "last_modifying_user": self.last_modifying_user,
            "content_hash": self.content_hash,
            "content_snapshot": self.content_snapshot,
        }


@dataclass
class DriveDebounceState:
    dirty: bool
    session_started_at: str
    last_change_seen_at: str
    raw_change_count: int
    start_version: Optional[str]
    latest_version: Optional[str]
    start_content_snapshot: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DriveDebounceState":
        return cls(
            dirty=bool(data.get("dirty", False)),
            session_started_at=str(data.get("session_started_at", "")),
            last_change_seen_at=str(data.get("last_change_seen_at", "")),
            raw_change_count=int(data.get("raw_change_count", 0)),
            start_version=data.get("start_version"),
            latest_version=data.get("latest_version"),
            start_content_snapshot=data.get("start_content_snapshot"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dirty": self.dirty,
            "session_started_at": self.session_started_at,
            "last_change_seen_at": self.last_change_seen_at,
            "raw_change_count": self.raw_change_count,
            "start_version": self.start_version,
            "latest_version": self.latest_version,
            "start_content_snapshot": self.start_content_snapshot,
        }


class DriveTransitionClassifier:
    def classify(self, previous: Optional[DriveFileSnapshot], current: Optional[DriveFileSnapshot], *, removed: bool) -> list[str]:
        event_types: list[str] = []

        if removed:
            event_types.append(GoogleDriveEventType.FILE_REMOVED)
            return event_types

        if current is None:
            return event_types

        if previous is None:
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

        return event_types

    def has_update_signal(self, previous: Optional[DriveFileSnapshot], current: Optional[DriveFileSnapshot]) -> bool:
        if previous is None or current is None:
            return False
        
        # Don't emit updates for folders
        if current.mime_type == "application/vnd.google-apps.folder":
            return False
            
        return previous.modified_time != current.modified_time

    @staticmethod
    def _shared_with_you_changed(previous: DriveFileSnapshot, current: DriveFileSnapshot) -> bool:
        if current.owned_by_me:
            return False
        if not current.shared_with_me_time:
            return False
        if not previous.shared_with_me_time:
            return True
        return current.shared_with_me_time > previous.shared_with_me_time


class DriveDebounceManager:
    def mark_dirty(
        self,
        existing: Optional[DriveDebounceState],
        *,
        now: datetime,
        start_version: Optional[str],
        latest_version: Optional[str],
        start_content_snapshot: Optional[str] = None,
    ) -> DriveDebounceState:
        now_iso = now.astimezone(timezone.utc).isoformat()
        session_started_at = existing.session_started_at if existing else now_iso
        first_version = existing.start_version if existing else start_version
        first_content = existing.start_content_snapshot if existing else start_content_snapshot
        return DriveDebounceState(
            dirty=True,
            session_started_at=session_started_at,
            last_change_seen_at=now_iso,
            raw_change_count=(existing.raw_change_count + 1) if existing else 1,
            start_version=first_version,
            latest_version=latest_version,
            start_content_snapshot=first_content,
        )

    def should_flush(self, state: DriveDebounceState, *, now: datetime, quiet_window_seconds: float, max_session_seconds: float) -> bool:
        if not state.dirty:
            return False

        now_utc = now.astimezone(timezone.utc)
        last_change_seen = datetime.fromisoformat(state.last_change_seen_at)
        session_started = datetime.fromisoformat(state.session_started_at)
        return (now_utc - last_change_seen).total_seconds() >= quiet_window_seconds or (now_utc - session_started).total_seconds() >= max_session_seconds


class DriveTextDiffCalculator:
    def __init__(self, max_section_chars: int = 300, max_changed_sections: int = 5):
        self.max_section_chars = max_section_chars
        self.max_changed_sections = max_changed_sections

    def normalize(self, text: str) -> list[str]:
        if not text:
            return []
        # decode to Unicode (already assumed)
        # convert \r\n and \r to \n
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # strip trailing whitespace at line ends
        lines = [line.rstrip() for line in text.split("\n")]
        
        # split on blank-line boundaries (paragraphs)
        content = "\n".join(lines)
        paragraphs = re.split(r"\n\s*\n", content)
        
        # discard only empty trailing blocks
        return [p.strip() for p in paragraphs if p.strip()]

    def compute_diff(self, old_text: Optional[str], new_text: Optional[str]) -> dict[str, Any]:
        old_blocks = self.normalize(old_text or "")
        new_blocks = self.normalize(new_text or "")
        
        matcher = difflib.SequenceMatcher(None, old_blocks, new_blocks)
        
        changed_sections_count = 0
        added_char_count = 0
        removed_char_count = 0
        
        changes = []
        
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            
            # Count each changed block as a section
            changed_sections_count += max(i2 - i1, j2 - j1)
            
            # Sum added/removed chars
            old_part = "\n\n".join(old_blocks[i1:i2])
            new_part = "\n\n".join(new_blocks[j1:j2])
            
            removed_char_count += len(old_part)
            added_char_count += len(new_part)
            
            # Collect changed blocks
            if len(changes) < self.max_changed_sections:
                # Capture blocks from this change
                # We can pair them up or just list them. The issue says "report array of found changes"
                # Let's emit objects with before/after for each changed block
                max_to_add = self.max_changed_sections - len(changes)
                num_blocks = max(i2 - i1, j2 - j1)
                for idx in range(min(num_blocks, max_to_add)):
                    before = old_blocks[i1 + idx] if (i1 + idx) < i2 else None
                    after = new_blocks[j1 + idx] if (j1 + idx) < j2 else None
                    
                    if before and len(before) > self.max_section_chars:
                        before = before[:self.max_section_chars] + " (truncated)"
                    if after and len(after) > self.max_section_chars:
                        after = after[:self.max_section_chars] + " (truncated)"
                    
                    changes.append({
                        "before": before,
                        "after": after
                    })

        return {
            "totalChangedSections": changed_sections_count,
            "changes": changes,
            "addedCharCount": added_char_count,
            "removedCharCount": removed_char_count,
        }

    @staticmethod
    def get_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
