from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock

from src.config import GoogleDriveSourceConfig
from src.sources.google_drive import GoogleDriveSource
from src.utils.google_drive_sync import GoogleDriveEventType


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
async def test_cursor_is_only_advanced_after_feed_drain(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    service = MagicMock()
    source._get_service = MagicMock(return_value=service)
    source._process_change = MagicMock(return_value=[])
    source._flush_debounced_updates = MagicMock(return_value=[])

    services.cursor.get_last_cursor.return_value = "start-token"
    service.changes().list.return_value.execute.side_effect = [
        {"changes": [{"fileId": "f1"}], "nextPageToken": "page-2"},
        {"changes": [{"fileId": "f2"}], "newStartPageToken": "new-start"},
    ]

    await source.fetch_and_publish()

    services.cursor.set_cursor.assert_called_once_with(1, "new-start")
    assert source._process_change.call_count == 2


@pytest.mark.asyncio
async def test_removed_change_emits_removed_event_name(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._get_cached_snapshot = MagicMock(return_value=None)
    source._delete_cached_snapshot = MagicMock()
    source._clear_debounce_state = MagicMock()

    events = source._process_change(
        service=MagicMock(),
        change={"fileId": "f1", "removed": True, "time": "2026-03-14T12:00:00Z"},
        now=datetime.now(timezone.utc),
    )

    assert len(events) == 1
    assert events[0].event_type == GoogleDriveEventType.FILE_REMOVED
    assert "google.drive.file_removed" in events[0].event_id
    assert events[0].data == {
        "fileId": "f1",
        "lastKnownName": None,
        "lastKnownMimeType": None,
        "lastKnownParentIds": [],
    }


@pytest.mark.asyncio
async def test_created_event_contains_delta_fields_only(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._get_cached_snapshot = MagicMock(return_value=None)
    source._set_cached_snapshot = MagicMock()
    source._fetch_file = MagicMock(
        return_value={
            "id": "f1",
            "name": "Roadmap",
            "mimeType": "application/vnd.google-apps.document",
            "parents": ["folder-1"],
            "createdTime": "2026-03-14T11:00:00Z",
            "modifiedTime": "2026-03-14T11:00:00Z",
            "version": "1",
            "trashed": False,
            "ownedByMe": True,
            "description": "My Roadmap",
            "contentHints": {"indexableText": "This is a roadmap"},
            "lastModifyingUser": {"displayName": "Bob"},
        }
    )

    events = source._process_change(
        service=MagicMock(),
        change={"fileId": "f1", "removed": False, "time": "2026-03-14T12:00:00Z"},
        now=datetime.now(timezone.utc),
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == GoogleDriveEventType.FILE_CREATED
    assert event.data == {
        "fileId": "f1",
        "name": "Roadmap",
        "mimeType": "application/vnd.google-apps.document",
        "parentIds": ["folder-1"],
        "createdTime": "2026-03-14T11:00:00Z",
        "modificationDate": "2026-03-14T11:00:00Z",
        "description": "My Roadmap",
        "indexableText": "This is a roadmap",
        "lastModifyingUser": {"displayName": "Bob"},
    }
    assert "file" not in event.data


@pytest.mark.asyncio
async def test_initial_fetch_calls_bootstrap(services):
    config = make_config(bootstrap_mode="baseline_only")
    source = GoogleDriveSource("drive", config, services, source_id=1)
    service = MagicMock()
    source._get_service = MagicMock(return_value=service)
    source._bootstrap_repository = MagicMock()
    source._flush_debounced_updates = MagicMock(return_value=[])

    services.cursor.get_last_cursor.return_value = None
    service.changes().getStartPageToken().execute.return_value = {"startPageToken": "start-token"}

    await source.fetch_and_publish()

    source._bootstrap_repository.assert_called_once_with(service)
    services.cursor.set_cursor.assert_called_once_with(1, "start-token")


@pytest.mark.asyncio
async def test_bootstrap_repository_populates_kv(services):
    config = make_config(bootstrap_mode="baseline_only")
    source = GoogleDriveSource("drive", config, services, source_id=1)
    service = MagicMock()

    service.files().list().execute.side_effect = [
        {
            "files": [
                {
                    "id": "f1",
                    "name": "File 1",
                    "mimeType": "text/plain",
                    "version": "1",
                },
                {
                    "id": "f2",
                    "name": "File 2",
                    "mimeType": "application/pdf",
                    "version": "5",
                },
            ],
            "nextPageToken": None,
        }
    ]

    source._bootstrap_repository(service)

    assert services.kv.set.call_count == 2
    # Check if correct keys and snapshots were set
    call_args_list = services.kv.set.call_args_list
    
    # f1
    assert call_args_list[0][0][0] == 1  # source_id
    assert "gdrive:file:f1" in call_args_list[0][0][1] # key
    snapshot1 = call_args_list[0][0][2]
    assert snapshot1["file_id"] == "f1"
    assert snapshot1["name"] == "File 1"

    # f2
    assert call_args_list[1][0][0] == 1
    assert "gdrive:file:f2" in call_args_list[1][0][1]
    snapshot2 = call_args_list[1][0][2]
    assert snapshot2["file_id"] == "f2"
    assert snapshot2["name"] == "File 2"
