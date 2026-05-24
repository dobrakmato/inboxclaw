from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock
from googleapiclient.errors import HttpError

from src.config import GoogleDriveSourceConfig
from src.sources.google_drive import GoogleDriveSource
from src.utils.google_drive_sync import DriveFileSnapshot, GoogleDriveEventType


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
    source._process_change_result = MagicMock(return_value=([], []))
    source._flush_debounced_updates = MagicMock(return_value=[])

    services.cursor.get_last_cursor.return_value = "start-token"
    service.changes().list.return_value.execute.side_effect = [
        {"changes": [{"fileId": "f1"}], "nextPageToken": "page-2"},
        {"changes": [{"fileId": "f2"}], "newStartPageToken": "new-start"},
    ]

    await source.fetch_and_publish()

    services.cursor.set_cursor.assert_called_once_with(1, "new-start")
    assert source._process_change_result.call_count == 2


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
async def test_initial_fetch_establishes_cursor_without_file_scan(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    service = MagicMock()
    source._get_service = MagicMock(return_value=service)
    source._flush_debounced_updates = MagicMock(return_value=[])

    services.cursor.get_last_cursor.return_value = None
    service.changes().getStartPageToken().execute.return_value = {"startPageToken": "start-token"}

    await source.fetch_and_publish()

    services.cursor.set_cursor.assert_called_once_with(1, "start-token")
    service.files().list.assert_not_called()


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
async def test_diff_produced_after_metadata_only_cached_snapshot(services):
    """After a metadata-only cached snapshot, the first content update stores
    content and the second content update produces a diff."""
    from src.utils.google_drive_sync import DriveFileSnapshot, DriveTextDiffCalculator

    config = make_config()
    source = GoogleDriveSource("drive", config, services, source_id=1)

    # --- First update: previous cached metadata has no content ---
    prev_cached = DriveFileSnapshot(
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
    source._get_cached_snapshot = MagicMock(return_value=prev_cached)
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


@pytest.mark.asyncio
async def test_expired_page_token_resets_cursor_without_file_scan(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    service = MagicMock()
    source._get_service = MagicMock(return_value=service)

    services.cursor.get_last_cursor.return_value = "expired-token"
    error_resp = MagicMock()
    error_resp.status = 410
    service.changes().list.return_value.execute.side_effect = HttpError(error_resp, b"expired")
    service.changes().getStartPageToken.return_value.execute.return_value = {"startPageToken": "fresh-token"}

    await source.fetch_and_publish()

    services.cursor.set_cursor.assert_called_once_with(1, "fresh-token")
    service.changes().getStartPageToken.assert_called_once()
    service.files().list.assert_not_called()
    services.writer.write_events.assert_not_called()
    services.kv.delete.assert_not_called()


@pytest.mark.asyncio
async def test_changes_list_requests_all_drives_items(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    service = MagicMock()
    source._get_service = MagicMock(return_value=service)
    source._process_change_result = MagicMock(return_value=([], []))

    services.cursor.get_last_cursor.return_value = "start-token"
    service.changes().list.return_value.execute.return_value = {
        "changes": [],
        "newStartPageToken": "new-start",
    }

    await source.fetch_and_publish()

    _, kwargs = service.changes().list.call_args
    assert kwargs["pageToken"] == "start-token"
    assert kwargs["includeRemoved"] is True
    assert kwargs["supportsAllDrives"] is True
    assert kwargs["includeItemsFromAllDrives"] is True


@pytest.mark.asyncio
async def test_changes_list_can_restrict_to_my_drive(services):
    source = GoogleDriveSource("drive", make_config(restrict_to_my_drive=True), services, source_id=1)
    service = MagicMock()
    source._get_service = MagicMock(return_value=service)
    source._process_change_result = MagicMock(return_value=([], []))

    services.cursor.get_last_cursor.return_value = "start-token"
    service.changes().list.return_value.execute.return_value = {
        "changes": [],
        "newStartPageToken": "new-start",
    }

    await source.fetch_and_publish()

    _, kwargs = service.changes().list.call_args
    assert kwargs["restrictToMyDrive"] is True
    service.files().list.assert_not_called()


def test_filtered_tracked_file_suppresses_events_and_drops_cache(services):
    source = GoogleDriveSource(
        "drive",
        make_config(filters=[{"ignore_private": {"in": "name", "contains": "Private"}}]),
        services,
        source_id=1,
    )
    previous = DriveFileSnapshot(
        file_id="f1",
        name="Team Plan",
        mime_type="text/plain",
        parents=["root"],
        trashed=False,
        created_time=None,
        modified_time=None,
        owned_by_me=True,
        version="v1",
    )
    source._get_cached_snapshot = MagicMock(return_value=previous)
    source._delete_cached_snapshot = MagicMock()
    source._fetch_file = MagicMock(return_value={
        "id": "f1",
        "name": "Private Plan",
        "mimeType": "text/plain",
        "parents": ["root"],
        "trashed": False,
        "ownedByMe": True,
        "version": "v2",
    })

    events = source._process_change(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-01T10:00:00Z"},
        datetime.now(timezone.utc),
    )

    assert events == []
    source._delete_cached_snapshot.assert_called_once_with(previous.file_id)


def test_first_seen_file_before_trusted_baseline_is_cached_without_created(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._get_cached_snapshot = MagicMock(return_value=None)
    source._set_cached_snapshot = MagicMock()
    services.kv.get.side_effect = lambda source_id, key: {
        "trusted": True,
        "established_at": "2026-04-01T00:00:00+00:00",
        "config_fingerprint": source._config_fingerprint(),
    } if key == source.BASELINE_KEY else None
    source._fetch_file = MagicMock(return_value={
        "id": "f1",
        "name": "Existing",
        "mimeType": "text/plain",
        "parents": ["root"],
        "trashed": False,
        "createdTime": "2026-03-01T00:00:00Z",
        "modifiedTime": "2026-04-02T00:00:00Z",
        "ownedByMe": True,
        "version": "2",
    })

    events, mutations = source._process_change_result(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-02T00:00:00Z"},
        datetime.now(timezone.utc),
        allow_created=None,
        allow_first_seen_shared=None,
    )

    assert events == []
    assert len(mutations) == 1
    assert mutations[0].action == "set"


def test_first_seen_file_after_trusted_baseline_emits_created(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._get_cached_snapshot = MagicMock(return_value=None)
    services.kv.get.side_effect = lambda source_id, key: {
        "trusted": True,
        "established_at": "2026-04-01T00:00:00+00:00",
        "config_fingerprint": source._config_fingerprint(),
    } if key == source.BASELINE_KEY else None
    source._fetch_file = MagicMock(return_value={
        "id": "f1",
        "name": "New",
        "mimeType": "text/plain",
        "parents": ["root"],
        "trashed": False,
        "createdTime": "2026-04-02T00:00:00Z",
        "modifiedTime": "2026-04-02T00:00:00Z",
        "ownedByMe": True,
        "version": "1",
    })

    events, _ = source._process_change_result(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-02T00:00:00Z"},
        datetime.now(timezone.utc),
        allow_created=None,
        allow_first_seen_shared=None,
    )

    assert len(events) == 1
    assert events[0].event_type == GoogleDriveEventType.FILE_CREATED


def test_first_seen_shared_file_emits_shared_not_created(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._get_cached_snapshot = MagicMock(return_value=None)
    services.kv.get.side_effect = lambda source_id, key: {
        "trusted": True,
        "established_at": "2026-04-01T00:00:00+00:00",
        "config_fingerprint": source._config_fingerprint(),
    } if key == source.BASELINE_KEY else None
    source._fetch_file = MagicMock(return_value={
        "id": "f1",
        "name": "Shared",
        "mimeType": "application/pdf",
        "parents": ["root"],
        "trashed": False,
        "createdTime": "2026-03-01T00:00:00Z",
        "modifiedTime": "2026-04-02T00:00:00Z",
        "ownedByMe": False,
        "sharedWithMeTime": "2026-04-02T00:00:00Z",
        "sharingUser": {"displayName": "Alice"},
        "version": "2",
    })

    events, _ = source._process_change_result(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-02T00:00:00Z"},
        datetime.now(timezone.utc),
        allow_created=None,
        allow_first_seen_shared=None,
    )

    assert len(events) == 1
    assert events[0].event_type == GoogleDriveEventType.FILE_SHARED_WITH_YOU


def test_text_update_with_unchanged_content_is_suppressed(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    content_hash = source.diff_calc.get_hash("same content")
    previous = DriveFileSnapshot(
        file_id="f1",
        name="Doc",
        mime_type="text/plain",
        parents=["root"],
        trashed=False,
        created_time="2026-04-01T00:00:00Z",
        modified_time="2026-04-01T00:00:00Z",
        owned_by_me=True,
        content_snapshot="same content",
        content_hash=content_hash,
    )
    source._get_cached_snapshot = MagicMock(return_value=previous)
    source._set_cached_snapshot = MagicMock()
    source._fetch_file = MagicMock(return_value={
        "id": "f1",
        "name": "Doc",
        "mimeType": "text/plain",
        "parents": ["root"],
        "trashed": False,
        "createdTime": "2026-04-01T00:00:00Z",
        "modifiedTime": "2026-04-01T01:00:00Z",
        "ownedByMe": True,
        "version": "2",
    })
    source._fetch_text_content = MagicMock(return_value="same content")

    events = source._process_change(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-01T01:00:00Z"},
        datetime.now(timezone.utc),
    )

    assert events == []
    source._set_cached_snapshot.assert_called_once()


def test_empty_text_content_update_includes_diff(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    previous = DriveFileSnapshot(
        file_id="f1",
        name="Doc",
        mime_type="text/plain",
        parents=["root"],
        trashed=False,
        created_time="2026-04-01T00:00:00Z",
        modified_time="2026-04-01T00:00:00Z",
        owned_by_me=True,
        content_snapshot="",
        content_hash=source.diff_calc.get_hash(""),
    )
    source._get_cached_snapshot = MagicMock(return_value=previous)
    source._set_cached_snapshot = MagicMock()
    source._fetch_file = MagicMock(return_value={
        "id": "f1",
        "name": "Doc",
        "mimeType": "text/plain",
        "parents": ["root"],
        "trashed": False,
        "createdTime": "2026-04-01T00:00:00Z",
        "modifiedTime": "2026-04-01T01:00:00Z",
        "ownedByMe": True,
        "version": "2",
    })
    source._fetch_text_content = MagicMock(return_value="hello")

    events = source._process_change(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-01T01:00:00Z"},
        datetime.now(timezone.utc),
    )

    updated_events = [e for e in events if e.event_type == GoogleDriveEventType.FILE_UPDATED]
    assert len(updated_events) == 1
    assert updated_events[0].data["contentDiff"]["addedCharCount"] == 5


def test_move_only_change_does_not_emit_low_value_update(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    previous = DriveFileSnapshot(
        file_id="f1",
        name="Doc.pdf",
        mime_type="application/pdf",
        parents=["old"],
        trashed=False,
        created_time="2026-04-01T00:00:00Z",
        modified_time="2026-04-01T00:00:00Z",
        owned_by_me=True,
    )
    source._get_cached_snapshot = MagicMock(return_value=previous)
    source._set_cached_snapshot = MagicMock()
    source._fetch_file = MagicMock(return_value={
        "id": "f1",
        "name": "Doc.pdf",
        "mimeType": "application/pdf",
        "parents": ["new"],
        "trashed": False,
        "createdTime": "2026-04-01T00:00:00Z",
        "modifiedTime": "2026-04-01T01:00:00Z",
        "ownedByMe": True,
        "version": "2",
    })

    events = source._process_change(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-01T01:00:00Z"},
        datetime.now(timezone.utc),
    )

    assert [event.event_type for event in events] == [GoogleDriveEventType.FILE_MOVED]


def test_removed_change_respects_file_id_filter(services):
    source = GoogleDriveSource(
        "drive",
        make_config(filters=[{"skip_file": {"in": "file_id", "contains": "skip-me"}}]),
        services,
        source_id=1,
    )
    source._get_cached_snapshot = MagicMock(return_value=None)

    events, mutations = source._process_change_result(
        MagicMock(),
        {"fileId": "skip-me-1", "removed": True, "time": "2026-04-01T01:00:00Z"},
        datetime.now(timezone.utc),
    )

    assert events == []
    assert mutations == []


def test_first_seen_permission_shared_file_after_baseline_emits_shared(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._get_cached_snapshot = MagicMock(return_value=None)
    services.kv.get.side_effect = lambda source_id, key: {
        "trusted": True,
        "established_at": "2026-04-01T00:00:00+00:00",
        "config_fingerprint": source._config_fingerprint(),
    } if key == source.BASELINE_KEY else None
    source._fetch_file = MagicMock(return_value={
        "id": "f1",
        "name": "Shared by group",
        "mimeType": "application/pdf",
        "parents": ["root"],
        "trashed": False,
        "createdTime": "2026-04-02T00:00:00Z",
        "modifiedTime": "2026-04-02T00:00:00Z",
        "ownedByMe": False,
        "permissions": [{"type": "group", "emailAddress": "team@example.com"}],
        "version": "1",
    })

    events, mutations = source._process_change_result(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-02T00:00:00Z"},
        datetime.now(timezone.utc),
        allow_created=None,
        allow_first_seen_shared=None,
    )

    assert len(events) == 1
    assert events[0].event_type == GoogleDriveEventType.FILE_SHARED_WITH_YOU
    assert len(mutations) == 1


@pytest.mark.asyncio
async def test_content_fetch_http_error_does_not_advance_cursor(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    service = MagicMock()
    source._get_service = MagicMock(return_value=service)
    source._ensure_baseline_state = MagicMock()
    previous = DriveFileSnapshot(
        file_id="f1",
        name="Doc",
        mime_type="text/plain",
        parents=["root"],
        trashed=False,
        created_time="2026-04-01T00:00:00Z",
        modified_time="2026-04-01T00:00:00Z",
        owned_by_me=True,
        content_snapshot="old",
        content_hash=source.diff_calc.get_hash("old"),
    )
    source._get_cached_snapshot = MagicMock(return_value=previous)

    services.cursor.get_last_cursor.return_value = "start-token"
    service.changes().list.return_value.execute.return_value = {
        "changes": [{"fileId": "f1", "time": "2026-04-01T01:00:00Z"}],
        "newStartPageToken": "new-start",
    }
    error_resp = MagicMock()
    error_resp.status = 429
    service.files().get.return_value.execute.side_effect = [
        {
            "id": "f1",
            "name": "Doc",
            "mimeType": "text/plain",
            "parents": ["root"],
            "trashed": False,
            "createdTime": "2026-04-01T00:00:00Z",
            "modifiedTime": "2026-04-01T01:00:00Z",
            "ownedByMe": True,
            "version": "2",
        },
        HttpError(error_resp, b"rate limited"),
    ]

    await source.fetch_and_publish()

    services.writer.write_events.assert_not_called()
    services.cursor.set_cursor.assert_not_called()
@pytest.mark.asyncio
async def test_processing_error_does_not_advance_cursor_or_apply_cache(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    service = MagicMock()
    source._get_service = MagicMock(return_value=service)
    source._ensure_baseline_state = MagicMock()
    source._process_change_result = MagicMock(side_effect=RuntimeError("boom"))
    source._apply_cache_mutations = MagicMock()

    services.cursor.get_last_cursor.return_value = "start-token"
    service.changes().list.return_value.execute.return_value = {
        "changes": [{"fileId": "f1"}],
        "newStartPageToken": "new-start",
    }

    await source.fetch_and_publish()

    services.cursor.set_cursor.assert_not_called()
    source._apply_cache_mutations.assert_not_called()


@pytest.mark.asyncio
async def test_processing_error_does_not_write_successful_page_events(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    service = MagicMock()
    source._get_service = MagicMock(return_value=service)
    source._ensure_baseline_state = MagicMock()
    source._apply_cache_mutations = MagicMock()
    event = MagicMock()
    source._process_change_result = MagicMock(side_effect=[
        ([event], [MagicMock()]),
        RuntimeError("boom"),
    ])

    services.cursor.get_last_cursor.return_value = "start-token"
    service.changes().list.return_value.execute.return_value = {
        "changes": [{"fileId": "ok"}, {"fileId": "bad"}],
        "newStartPageToken": "new-start",
    }

    await source.fetch_and_publish()

    services.writer.write_events.assert_not_called()
    services.cursor.set_cursor.assert_not_called()
    source._apply_cache_mutations.assert_not_called()


@pytest.mark.asyncio
async def test_writer_failure_does_not_apply_cache_or_advance_cursor(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    service = MagicMock()
    source._get_service = MagicMock(return_value=service)
    source._ensure_baseline_state = MagicMock()
    source._apply_cache_mutations = MagicMock()
    event = MagicMock()
    source._process_change_result = MagicMock(return_value=([event], [MagicMock()]))

    services.cursor.get_last_cursor.return_value = "start-token"
    services.writer.write_events.side_effect = RuntimeError("db down")
    service.changes().list.return_value.execute.return_value = {
        "changes": [{"fileId": "f1"}],
        "newStartPageToken": "new-start",
    }

    await source.fetch_and_publish()

    services.cursor.set_cursor.assert_not_called()
    source._apply_cache_mutations.assert_not_called()
