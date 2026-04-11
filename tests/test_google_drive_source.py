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
            "owners": [{"displayName": "Bob", "emailAddress": "bob@example.com"}],
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
        "owners": [{"displayName": "Bob", "emailAddress": "bob@example.com"}],
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
                    "ownedByMe": True,
                },
                {
                    "id": "f2",
                    "name": "File 2",
                    "mimeType": "application/pdf",
                    "version": "5",
                    "ownedByMe": True,
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


@pytest.mark.asyncio
async def test_content_snapshot_preserved_on_non_content_change(services):
    """After a text file has content cached, a non-content change (e.g. move)
    must NOT lose the stored content_snapshot so that future diffs still work."""
    from src.utils.google_drive_sync import DriveFileSnapshot, DriveTextDiffCalculator

    config = make_config()
    source = GoogleDriveSource("drive", config, services, source_id=1)

    # Simulate a previously cached snapshot WITH content (as if a prior update stored it)
    prev = DriveFileSnapshot(
        file_id="f1",
        name="Doc",
        mime_type="application/vnd.google-apps.document",
        parents=["folder-a"],
        trashed=False,
        created_time="2026-01-01T00:00:00Z",
        modified_time="2026-01-01T00:00:00Z",
        owned_by_me=True,
        content_snapshot="Hello world",
        content_hash=DriveTextDiffCalculator.get_hash("Hello world"),
    )
    source._get_cached_snapshot = MagicMock(return_value=prev)
    set_snapshot_mock = MagicMock()
    source._set_cached_snapshot = set_snapshot_mock

    # The file was moved (parents changed) but modifiedTime is the same → no update signal
    source._fetch_file = MagicMock(return_value={
        "id": "f1",
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["folder-b"],
        "trashed": False,
        "createdTime": "2026-01-01T00:00:00Z",
        "modifiedTime": "2026-01-01T00:00:00Z",
        "ownedByMe": True,
    })

    events = source._process_change(
        service=MagicMock(),
        change={"fileId": "f1", "removed": False, "time": "2026-01-02T00:00:00Z"},
        now=datetime.now(timezone.utc),
    )

    # Should emit a move event
    assert any(e.event_type == GoogleDriveEventType.FILE_MOVED for e in events)

    # The cached snapshot must still have content_snapshot preserved
    cached = set_snapshot_mock.call_args[0][1]  # second positional arg is the snapshot
    assert cached.content_snapshot == "Hello world"
    assert cached.content_hash == DriveTextDiffCalculator.get_hash("Hello world")


@pytest.mark.asyncio
async def test_diff_produced_after_baseline_only_bootstrap(services):
    """After baseline_only bootstrap (no content), the first content update stores
    content, and the second content update produces a diff."""
    from src.utils.google_drive_sync import DriveFileSnapshot, DriveTextDiffCalculator

    config = make_config(bootstrap_mode="baseline_only")
    source = GoogleDriveSource("drive", config, services, source_id=1)

    # --- First update: previous from bootstrap has no content ---
    prev_bootstrap = DriveFileSnapshot(
        file_id="f1",
        name="Doc",
        mime_type="application/vnd.google-apps.document",
        parents=["folder-a"],
        trashed=False,
        created_time="2026-01-01T00:00:00Z",
        modified_time="2026-01-01T00:00:00Z",
        owned_by_me=True,
        content_snapshot=None,
        content_hash=None,
    )
    source._get_cached_snapshot = MagicMock(return_value=prev_bootstrap)
    set_mock = MagicMock()
    source._set_cached_snapshot = set_mock

    mock_service = MagicMock()
    source._fetch_file = MagicMock(return_value={
        "id": "f1",
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["folder-a"],
        "trashed": False,
        "createdTime": "2026-01-01T00:00:00Z",
        "modifiedTime": "2026-01-02T00:00:00Z",
        "ownedByMe": True,
    })
    source._fetch_text_content = MagicMock(return_value="Version 1 content")

    events1 = source._process_change(
        service=mock_service,
        change={"fileId": "f1", "removed": False, "time": "2026-01-02T00:00:00Z"},
        now=datetime.now(timezone.utc),
    )

    assert any(e.event_type == GoogleDriveEventType.FILE_UPDATED for e in events1)
    cached_after_first = set_mock.call_args[0][1]
    assert cached_after_first.content_snapshot == "Version 1 content"

    # --- Second update: previous now has content from first update ---
    source._get_cached_snapshot = MagicMock(return_value=cached_after_first)
    set_mock.reset_mock()

    source._fetch_file = MagicMock(return_value={
        "id": "f1",
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["folder-a"],
        "trashed": False,
        "createdTime": "2026-01-01T00:00:00Z",
        "modifiedTime": "2026-01-03T00:00:00Z",
        "ownedByMe": True,
    })
    source._fetch_text_content = MagicMock(return_value="Version 2 content")

    events2 = source._process_change(
        service=mock_service,
        change={"fileId": "f1", "removed": False, "time": "2026-01-03T00:00:00Z"},
        now=datetime.now(timezone.utc),
    )

    updated_events = [e for e in events2 if e.event_type == GoogleDriveEventType.FILE_UPDATED]
    assert len(updated_events) == 1
    assert "contentDiff" in updated_events[0].data


@pytest.mark.asyncio
async def test_event_unique_uses_version_when_change_time_missing(services):
    """When the change has no 'time' field, event_unique should fall back to
    the file version. This requires DriveFileSnapshot to carry a version field."""
    from src.utils.google_drive_sync import DriveFileSnapshot

    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._get_cached_snapshot = MagicMock(return_value=None)
    source._set_cached_snapshot = MagicMock()

    source._fetch_file = MagicMock(return_value={
        "id": "f1",
        "name": "Notes",
        "mimeType": "text/plain",
        "parents": ["root"],
        "trashed": False,
        "createdTime": "2026-04-01T00:00:00Z",
        "modifiedTime": "2026-04-01T00:00:00Z",
        "version": "42",
        "ownedByMe": True,
    })

    events = source._process_change(
        service=MagicMock(),
        change={"fileId": "f1", "removed": False},  # no "time" key
        now=datetime.now(timezone.utc),
    )

    assert len(events) == 1
    assert events[0].event_type == GoogleDriveEventType.FILE_CREATED
    # The unique part should contain the version "42"
    assert "42" in events[0].event_id


@pytest.mark.asyncio
async def test_occurred_at_uses_change_time_not_now(services):
    """Events should carry the change timestamp from the API, not the wall-clock
    time when the poll happened."""
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._get_cached_snapshot = MagicMock(return_value=None)
    source._set_cached_snapshot = MagicMock()

    source._fetch_file = MagicMock(return_value={
        "id": "f1",
        "name": "Notes",
        "mimeType": "text/plain",
        "parents": ["root"],
        "trashed": False,
        "createdTime": "2026-04-01T00:00:00Z",
        "modifiedTime": "2026-04-01T00:00:00Z",
        "version": "1",
        "ownedByMe": True,
    })

    now = datetime(2026, 4, 12, 0, 0, 0, tzinfo=timezone.utc)
    events = source._process_change(
        service=MagicMock(),
        change={"fileId": "f1", "removed": False, "time": "2026-04-01T10:00:00Z"},
        now=now,
    )

    assert len(events) == 1
    # occurred_at should be the change time, not "now"
    assert events[0].occurred_at == datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
