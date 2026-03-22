import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.config import GoogleDriveSourceConfig
from src.sources.google_drive import GoogleDriveSource
from src.utils.google_drive_sync import GoogleDriveEventType, DriveFileSnapshot, DriveTransitionClassifier, DriveTextDiffCalculator

@pytest.fixture
def services():
    mock = MagicMock()
    mock.cursor = MagicMock()
    mock.kv = MagicMock()
    mock.writer = MagicMock()
    return mock

def make_config(**overrides) -> GoogleDriveSourceConfig:
    data = {
        "type": "google_drive",
        "token_file": "token.json",
        "poll_interval": "10s",
    }
    data.update(overrides)
    return GoogleDriveSourceConfig(**data)

@pytest.mark.asyncio
async def test_file_updated_emitted_and_contains_diff(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    
    # Mock classifier and diff calc
    source.classifier = DriveTransitionClassifier()
    source.diff_calc = DriveTextDiffCalculator()
    
    # Setup previous state
    previous = DriveFileSnapshot(
        file_id="f1",
        name="Test.txt",
        mime_type="text/plain",
        parents=["root"],
        trashed=False,
        created_time="2024-01-01T00:00:00Z",
        modified_time="2024-01-01T00:00:00Z",
        owned_by_me=True,
        content_hash="old-hash",
        content_snapshot="Hello World"
    )
    
    source._get_cached_snapshot = MagicMock(return_value=previous)
    source._set_cached_snapshot = MagicMock()
    
    # New state
    current_resource = {
        "id": "f1",
        "name": "Test.txt",
        "mimeType": "text/plain",
        "parents": ["root"],
        "trashed": False,
        "createdTime": "2024-01-01T00:00:00Z",
        "modifiedTime": "2024-01-01T01:00:00Z",
        "version": "2",
        "ownedByMe": True,
        "lastModifyingUser": {"displayName": "Alice"},
        "description": "Updated description"
    }
    
    source._fetch_file = MagicMock(return_value=current_resource)
    source._fetch_text_content = MagicMock(return_value="Hello Beautiful World")
    
    now = datetime.now(timezone.utc)
    events = source._process_change(
        service=MagicMock(),
        change={"fileId": "f1", "removed": False, "time": "2024-01-01T01:00:00Z"},
        now=now
    )
    
    assert len(events) == 1
    event = events[0]
    assert event.event_type == GoogleDriveEventType.FILE_UPDATED
    assert event.data["modificationDate"] == "2024-01-01T01:00:00Z"
    assert event.data["lastModifyingUser"] == {"displayName": "Alice"}
    assert event.data["description"] == "Updated description"
    assert "contentDiff" in event.data
    assert event.data["contentDiff"]["totalChangedSections"] > 0

@pytest.mark.asyncio
async def test_file_moved_contains_before_after(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source.classifier = DriveTransitionClassifier()
    
    previous = DriveFileSnapshot(
        file_id="f1",
        name="Test.txt",
        mime_type="text/plain",
        parents=["folder-A"],
        trashed=False,
        created_time="2024-01-01T00:00:00Z",
        modified_time="2024-01-01T00:00:00Z",
        owned_by_me=True
    )
    
    source._get_cached_snapshot = MagicMock(return_value=previous)
    
    current_resource = {
        "id": "f1",
        "name": "Test.txt",
        "mimeType": "text/plain",
        "parents": ["folder-B"],
        "trashed": False,
        "version": "2",
        "modifiedTime": "2024-01-01T01:00:00Z",
        "ownedByMe": True,
    }
    
    source._fetch_file = MagicMock(return_value=current_resource)
    
    events = source._process_change(
        service=MagicMock(),
        change={"fileId": "f1", "removed": False, "time": "2024-01-01T01:00:00Z"},
        now=datetime.now(timezone.utc)
    )
    
    # It might emit both MOVED and UPDATED if we are not careful, but usually it's one or the other or both.
    # In our current classifier, it will emit both if modified_time also changed.
    moved_event = next(e for e in events if e.event_type == GoogleDriveEventType.FILE_MOVED)
    assert moved_event.data["parentIds"] == {"before": ["folder-A"], "after": ["folder-B"]}
