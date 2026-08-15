from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.config import GoogleDriveSourceConfig
from src.sources.google_drive import GoogleDriveSource
from src.utils.google_drive_client import DriveApiError
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


def attach_client(source: GoogleDriveSource) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.list_drives_page = AsyncMock(return_value={"drives": []})
    source._get_client = MagicMock(return_value=client)
    return client


@pytest.mark.asyncio
async def test_cursor_is_only_advanced_after_feed_drain(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    client = attach_client(source)
    source._process_change_result = AsyncMock(return_value=([], []))
    source._commit_page = MagicMock()

    services.cursor.get_last_cursor.return_value = "start-token"
    client.list_changes = AsyncMock(side_effect=[
        {"changes": [{"fileId": "f1"}], "nextPageToken": "page-2"},
        {"changes": [{"fileId": "f2"}], "newStartPageToken": "new-start"},
    ])

    await source.fetch_and_publish()

    assert [call.args[2] for call in source._commit_page.call_args_list] == ["page-2", "new-start"]
    assert source._process_change_result.await_count == 2


@pytest.mark.asyncio
async def test_repeated_file_in_page_uses_staged_snapshot(services):
    source = GoogleDriveSource(
        "drive",
        make_config(restrict_to_my_drive=True),
        services,
        source_id=1,
    )
    client = attach_client(source)
    source._ensure_baseline_state = MagicMock()
    source._commit_page = MagicMock()
    previous = DriveFileSnapshot(
        file_id="f1",
        name="Old",
        mime_type="application/pdf",
        parents=["root"],
        trashed=False,
        created_time="2026-04-01T00:00:00Z",
        modified_time="2026-04-01T00:00:00Z",
        owned_by_me=True,
        version="1",
    )
    source._get_cached_snapshot = MagicMock(return_value=previous)
    current_resource = {
        "id": "f1",
        "name": "New",
        "mimeType": "application/pdf",
        "parents": ["root"],
        "trashed": False,
        "createdTime": "2026-04-01T00:00:00Z",
        "modifiedTime": "2026-04-01T01:00:00Z",
        "ownedByMe": True,
        "version": "2",
    }
    source._fetch_file = AsyncMock(side_effect=[current_resource, current_resource])
    services.cursor.get_last_cursor.return_value = "start-token"
    client.list_changes = AsyncMock(return_value={
        "changes": [
            {"fileId": "f1", "time": "2026-04-01T01:00:00Z"},
            {"fileId": "f1", "time": "2026-04-01T01:01:00Z"},
        ],
        "newStartPageToken": "new-start",
    })

    await source.fetch_and_publish()

    events = source._commit_page.call_args.args[0]
    assert [event.event_type for event in events] == [GoogleDriveEventType.FILE_UPDATED]
    assert events[0].data["changes"]["name"] == {"before": "Old", "after": "New"}


@pytest.mark.asyncio
async def test_second_page_failure_keeps_first_page_checkpoint(services):
    source = GoogleDriveSource(
        "drive",
        make_config(restrict_to_my_drive=True),
        services,
        source_id=1,
    )
    client = attach_client(source)
    source._ensure_baseline_state = MagicMock()
    source._commit_page = MagicMock()
    source._process_change_result = AsyncMock(
        side_effect=[([], []), RuntimeError("bad second page")]
    )
    services.cursor.get_last_cursor.return_value = "start-token"
    client.list_changes = AsyncMock(side_effect=[
        {"changes": [{"fileId": "ok"}], "nextPageToken": "page-2"},
        {"changes": [{"fileId": "bad"}], "newStartPageToken": "new-start"},
    ])

    await source.fetch_and_publish()

    source._commit_page.assert_called_once()
    assert source._commit_page.call_args.args[2] == "page-2"


@pytest.mark.asyncio
async def test_new_shared_drive_is_baselined_and_then_polled(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    client = attach_client(source)
    source._ensure_baseline_state = MagicMock()
    source._commit_page = MagicMock()
    services.cursor.get_last_cursor.return_value = "user-token"
    services.kv.list_keys_with_prefix.return_value = []
    client.list_changes = AsyncMock(side_effect=[
        {"changes": [], "newStartPageToken": "user-next"},
        {"changes": [], "newStartPageToken": "drive-next"},
    ])
    client.list_drives_page = AsyncMock(return_value={"drives": [{"id": "drive-1"}]})
    client.get_start_page_token = AsyncMock(return_value="drive-start")
    client.list_files_page = AsyncMock(return_value={
        "files": [{
            "id": "shared-file",
            "name": "Shared file",
            "mimeType": "application/pdf",
            "parents": ["shared-root"],
            "trashed": False,
            "createdTime": "2026-04-01T00:00:00Z",
            "modifiedTime": "2026-04-01T00:00:00Z",
            "ownedByMe": False,
            "driveId": "drive-1",
            "version": "1",
        }]
    })

    await source.fetch_and_publish()

    assert source._commit_page.call_count == 3
    initialization = source._commit_page.call_args_list[1]
    assert initialization.args[0] == []
    assert initialization.args[1][0].file_id == "shared-file"
    assert initialization.kwargs["kv_updates"] == {
        source._shared_drive_cursor_key("drive-1"): "drive-start"
    }
    shared_poll = client.list_changes.await_args_list[1]
    assert shared_poll.kwargs["drive_id"] == "drive-1"


def test_removed_shared_drive_deletes_cached_files_and_checkpoint(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._commit_page = MagicMock()
    previous = DriveFileSnapshot(
        file_id="shared-file",
        name="Shared file",
        mime_type="application/pdf",
        parents=["shared-root"],
        trashed=False,
        created_time="2026-04-01T00:00:00Z",
        modified_time="2026-04-01T00:00:00Z",
        owned_by_me=False,
        drive_id="drive-1",
        version="1",
    )
    services.kv.list_keys_with_prefix.return_value = [source._snapshot_key("shared-file")]
    source._get_cached_snapshot = MagicMock(return_value=previous)

    source._remove_shared_drive("drive-1")

    events, mutations, cursor = source._commit_page.call_args.args
    assert cursor is None
    assert [event.event_type for event in events] == [GoogleDriveEventType.FILE_REMOVED]
    assert [(mutation.action, mutation.file_id) for mutation in mutations] == [
        ("delete", "shared-file")
    ]
    assert source._commit_page.call_args.kwargs["kv_deletes"] == [
        source._shared_drive_cursor_key("drive-1")
    ]


@pytest.mark.asyncio
async def test_my_drive_reconciliation_excludes_shared_drive_resources(services):
    source = GoogleDriveSource(
        "drive",
        make_config(restrict_to_my_drive=True),
        services,
        source_id=1,
    )
    source._commit_page = MagicMock()
    services.kv.list_keys_with_prefix.return_value = []
    client = MagicMock()
    client.get_start_page_token = AsyncMock(return_value="fresh-token")
    client.get_file = AsyncMock(return_value={"id": "root-id"})
    client.list_files_page = AsyncMock(return_value={
        "files": [
            {
                "id": "mine",
                "name": "Mine",
                "mimeType": "application/pdf",
                "parents": ["root-id"],
                "ownedByMe": True,
            },
            {
                "id": "shared",
                "name": "Shared",
                "mimeType": "application/pdf",
                "parents": ["shared-root"],
                "ownedByMe": False,
                "driveId": "drive-1",
            },
        ]
    })

    await source._reconcile_expired_page_token(client)

    _, mutations, cursor = source._commit_page.call_args.args
    assert cursor == "fresh-token"
    assert [(mutation.action, mutation.file_id) for mutation in mutations] == [
        ("set", "mine")
    ]


@pytest.mark.asyncio
async def test_removed_change_emits_removed_event_name(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._get_cached_snapshot = MagicMock(return_value=None)
    source._delete_cached_snapshot = MagicMock()
    source._clear_debounce_state = MagicMock()

    events = await source._process_change(
        client=MagicMock(),
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
    source._fetch_file = AsyncMock(
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
    source._fetch_text_content = AsyncMock(return_value=None)

    events = await source._process_change(
        client=MagicMock(),
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
    client = attach_client(source)
    client.get_start_page_token = AsyncMock(return_value="start-token")
    source._commit_page = MagicMock()

    services.cursor.get_last_cursor.return_value = None

    await source.fetch_and_publish()

    source._commit_page.assert_called_once()
    assert source._commit_page.call_args.args[2] == "start-token"
    client.list_files_page.assert_not_called()


@pytest.mark.asyncio
async def test_initial_fetch_baselines_shared_drive_in_same_commit(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    client = attach_client(source)
    client.get_start_page_token = AsyncMock(side_effect=["user-start", "drive-start"])
    client.list_drives_page = AsyncMock(return_value={"drives": [{"id": "drive-1"}]})
    client.list_files_page = AsyncMock(return_value={
        "files": [{
            "id": "shared-file",
            "name": "Shared",
            "mimeType": "application/pdf",
            "parents": ["shared-root"],
            "ownedByMe": False,
            "driveId": "drive-1",
        }]
    })
    source._commit_page = MagicMock()
    services.cursor.get_last_cursor.return_value = None

    await source.fetch_and_publish()

    events, mutations, cursor = source._commit_page.call_args.args
    assert events == []
    assert cursor == "user-start"
    assert [mutation.file_id for mutation in mutations] == ["shared-file"]
    assert source._commit_page.call_args.kwargs["kv_updates"] == {
        source._shared_drive_cursor_key("drive-1"): "drive-start"
    }


@pytest.mark.asyncio
async def test_shared_drive_410_reconciles_only_that_log(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._reconcile_expired_page_token = AsyncMock()
    client = MagicMock()
    client.list_changes = AsyncMock(
        side_effect=DriveApiError(410, "expired", frozenset(), "expired")
    )

    completed = await source._drain_change_log(client, "expired", drive_id="drive-1")

    assert completed is True
    source._reconcile_expired_page_token.assert_awaited_once_with(
        client,
        drive_id="drive-1",
    )


@pytest.mark.asyncio
async def test_shared_drive_membership_removal_drops_old_cursor(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._list_shared_drive_ids = AsyncMock(return_value=[])
    source._get_shared_drive_cursors = MagicMock(return_value={"drive-1": "old-token"})
    source._remove_shared_drive = MagicMock()

    cursors = await source._sync_shared_drive_membership(MagicMock())

    assert cursors == {}
    source._remove_shared_drive.assert_called_once_with("drive-1")


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
    source._fetch_file = AsyncMock(return_value={
        "id": "f1",
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["folder-b"],
        "trashed": False,
        "createdTime": "2026-01-01T00:00:00Z",
        "modifiedTime": "2026-01-01T00:00:00Z",
        "ownedByMe": True,
    })
    source._fetch_text_content = AsyncMock(return_value=None)

    events = await source._process_change(
        client=MagicMock(),
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
    source._fetch_file = AsyncMock(return_value={
        "id": "f1",
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["folder-a"],
        "trashed": False,
        "createdTime": "2026-01-01T00:00:00Z",
        "modifiedTime": "2026-01-02T00:00:00Z",
        "ownedByMe": True,
    })
    source._fetch_text_content = AsyncMock(return_value="Version 1 content")

    events1 = await source._process_change(
        client=mock_service,
        change={"fileId": "f1", "removed": False, "time": "2026-01-02T00:00:00Z"},
        now=datetime.now(timezone.utc),
    )

    assert any(e.event_type == GoogleDriveEventType.FILE_UPDATED for e in events1)
    cached_after_first = set_mock.call_args[0][1]
    assert cached_after_first.content_snapshot == "Version 1 content"

    # --- Second update: previous now has content from first update ---
    source._get_cached_snapshot = MagicMock(return_value=cached_after_first)
    set_mock.reset_mock()

    source._fetch_file = AsyncMock(return_value={
        "id": "f1",
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["folder-a"],
        "trashed": False,
        "createdTime": "2026-01-01T00:00:00Z",
        "modifiedTime": "2026-01-03T00:00:00Z",
        "ownedByMe": True,
    })
    source._fetch_text_content = AsyncMock(return_value="Version 2 content")

    events2 = await source._process_change(
        client=mock_service,
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

    source._fetch_file = AsyncMock(return_value={
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
    source._fetch_text_content = AsyncMock(return_value=None)

    events = await source._process_change(
        client=MagicMock(),
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

    source._fetch_file = AsyncMock(return_value={
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
    source._fetch_text_content = AsyncMock(return_value=None)

    now = datetime(2026, 4, 12, 0, 0, 0, tzinfo=timezone.utc)
    events = await source._process_change(
        client=MagicMock(),
        change={"fileId": "f1", "removed": False, "time": "2026-04-01T10:00:00Z"},
        now=now,
    )

    assert len(events) == 1
    # occurred_at should be the change time, not "now"
    assert events[0].occurred_at == datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_expired_page_token_reconciles_current_drive_state(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    client = attach_client(source)
    source._reconcile_expired_page_token = AsyncMock()

    services.cursor.get_last_cursor.return_value = "expired-token"
    client.list_changes = AsyncMock(
        side_effect=DriveApiError(410, "expired", frozenset(), "expired")
    )

    await source.fetch_and_publish()

    source._reconcile_expired_page_token.assert_awaited_once_with(client)


@pytest.mark.asyncio
async def test_changes_list_requests_all_drives_items(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    client = attach_client(source)
    source._process_change_result = AsyncMock(return_value=([], []))
    source._commit_page = MagicMock()

    services.cursor.get_last_cursor.return_value = "start-token"
    client.list_changes = AsyncMock(return_value={
        "changes": [],
        "newStartPageToken": "new-start",
    })

    await source.fetch_and_publish()

    client.list_changes.assert_awaited_once_with(
        "start-token",
        include_corpus_removals=False,
        restrict_to_my_drive=False,
    )


@pytest.mark.asyncio
async def test_changes_list_can_restrict_to_my_drive(services):
    source = GoogleDriveSource("drive", make_config(restrict_to_my_drive=True), services, source_id=1)
    client = attach_client(source)
    source._process_change_result = AsyncMock(return_value=([], []))
    source._commit_page = MagicMock()

    services.cursor.get_last_cursor.return_value = "start-token"
    client.list_changes = AsyncMock(return_value={
        "changes": [],
        "newStartPageToken": "new-start",
    })

    await source.fetch_and_publish()

    client.list_changes.assert_awaited_once_with(
        "start-token",
        include_corpus_removals=False,
        restrict_to_my_drive=True,
    )


@pytest.mark.asyncio
async def test_filtered_tracked_file_suppresses_events_and_drops_cache(services):
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
    source._fetch_file = AsyncMock(return_value={
        "id": "f1",
        "name": "Private Plan",
        "mimeType": "text/plain",
        "parents": ["root"],
        "trashed": False,
        "ownedByMe": True,
        "version": "v2",
    })

    events = await source._process_change(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-01T10:00:00Z"},
        datetime.now(timezone.utc),
    )

    assert events == []
    source._delete_cached_snapshot.assert_called_once_with(previous.file_id)


@pytest.mark.asyncio
async def test_first_seen_file_before_trusted_baseline_is_cached_without_created(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._get_cached_snapshot = MagicMock(return_value=None)
    source._set_cached_snapshot = MagicMock()
    services.kv.get.side_effect = lambda source_id, key: {
        "trusted": True,
        "established_at": "2026-04-01T00:00:00+00:00",
        "config_fingerprint": source._config_fingerprint(),
    } if key == source.BASELINE_KEY else None
    source._fetch_file = AsyncMock(return_value={
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
    source._fetch_text_content = AsyncMock(return_value=None)

    events, mutations = await source._process_change_result(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-02T00:00:00Z"},
        datetime.now(timezone.utc),
        allow_created=None,
        allow_first_seen_shared=None,
    )

    assert events == []
    assert len(mutations) == 1
    assert mutations[0].action == "set"


@pytest.mark.asyncio
async def test_first_seen_file_after_trusted_baseline_emits_created(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._get_cached_snapshot = MagicMock(return_value=None)
    services.kv.get.side_effect = lambda source_id, key: {
        "trusted": True,
        "established_at": "2026-04-01T00:00:00+00:00",
        "config_fingerprint": source._config_fingerprint(),
    } if key == source.BASELINE_KEY else None
    source._fetch_file = AsyncMock(return_value={
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
    source._fetch_text_content = AsyncMock(return_value=None)

    events, _ = await source._process_change_result(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-02T00:00:00Z"},
        datetime.now(timezone.utc),
        allow_created=None,
        allow_first_seen_shared=None,
    )

    assert len(events) == 1
    assert events[0].event_type == GoogleDriveEventType.FILE_CREATED


@pytest.mark.asyncio
async def test_first_seen_shared_file_emits_shared_not_created(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._get_cached_snapshot = MagicMock(return_value=None)
    services.kv.get.side_effect = lambda source_id, key: {
        "trusted": True,
        "established_at": "2026-04-01T00:00:00+00:00",
        "config_fingerprint": source._config_fingerprint(),
    } if key == source.BASELINE_KEY else None
    source._fetch_file = AsyncMock(return_value={
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

    events, _ = await source._process_change_result(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-02T00:00:00Z"},
        datetime.now(timezone.utc),
        allow_created=None,
        allow_first_seen_shared=None,
    )

    assert len(events) == 1
    assert events[0].event_type == GoogleDriveEventType.FILE_SHARED_WITH_YOU


@pytest.mark.asyncio
async def test_text_update_with_unchanged_content_is_suppressed(services):
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
    source._fetch_file = AsyncMock(return_value={
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
    source._fetch_text_content = AsyncMock(return_value="same content")

    events = await source._process_change(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-01T01:00:00Z"},
        datetime.now(timezone.utc),
    )

    assert events == []
    source._set_cached_snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_empty_text_content_update_includes_diff(services):
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
    source._fetch_file = AsyncMock(return_value={
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
    source._fetch_text_content = AsyncMock(return_value="hello")

    events = await source._process_change(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-01T01:00:00Z"},
        datetime.now(timezone.utc),
    )

    updated_events = [e for e in events if e.event_type == GoogleDriveEventType.FILE_UPDATED]
    assert len(updated_events) == 1
    assert updated_events[0].data["contentDiff"]["addedCharCount"] == 5


@pytest.mark.asyncio
async def test_move_only_change_does_not_emit_low_value_update(services):
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
    source._fetch_file = AsyncMock(return_value={
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

    events = await source._process_change(
        MagicMock(),
        {"fileId": "f1", "time": "2026-04-01T01:00:00Z"},
        datetime.now(timezone.utc),
    )

    assert [event.event_type for event in events] == [GoogleDriveEventType.FILE_MOVED]


@pytest.mark.asyncio
async def test_removed_change_respects_file_id_filter(services):
    source = GoogleDriveSource(
        "drive",
        make_config(filters=[{"skip_file": {"in": "file_id", "contains": "skip-me"}}]),
        services,
        source_id=1,
    )
    source._get_cached_snapshot = MagicMock(return_value=None)

    events, mutations = await source._process_change_result(
        MagicMock(),
        {"fileId": "skip-me-1", "removed": True, "time": "2026-04-01T01:00:00Z"},
        datetime.now(timezone.utc),
    )

    assert events == []
    assert mutations == []


@pytest.mark.asyncio
async def test_first_seen_permission_shared_file_after_baseline_emits_shared(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._get_cached_snapshot = MagicMock(return_value=None)
    services.kv.get.side_effect = lambda source_id, key: {
        "trusted": True,
        "established_at": "2026-04-01T00:00:00+00:00",
        "config_fingerprint": source._config_fingerprint(),
    } if key == source.BASELINE_KEY else None
    source._fetch_file = AsyncMock(return_value={
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

    events, mutations = await source._process_change_result(
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
    client = attach_client(source)
    source._ensure_baseline_state = MagicMock()
    source._commit_page = MagicMock()
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
    client.list_changes = AsyncMock(return_value={
        "changes": [{"fileId": "f1", "time": "2026-04-01T01:00:00Z"}],
        "newStartPageToken": "new-start",
    })
    source._fetch_file = AsyncMock(return_value={
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
    source._fetch_text_content = AsyncMock(
        side_effect=DriveApiError(429, "rate limited", frozenset({"rateLimitExceeded"}), "")
    )

    await source.fetch_and_publish()

    source._commit_page.assert_not_called()


@pytest.mark.asyncio
async def test_processing_error_does_not_advance_cursor_or_apply_cache(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    client = attach_client(source)
    source._ensure_baseline_state = MagicMock()
    source._process_change_result = AsyncMock(side_effect=RuntimeError("boom"))
    source._commit_page = MagicMock()

    services.cursor.get_last_cursor.return_value = "start-token"
    client.list_changes = AsyncMock(return_value={
        "changes": [{"fileId": "f1"}],
        "newStartPageToken": "new-start",
    })

    await source.fetch_and_publish()

    source._commit_page.assert_not_called()


@pytest.mark.asyncio
async def test_processing_error_does_not_write_successful_page_events(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    client = attach_client(source)
    source._ensure_baseline_state = MagicMock()
    source._commit_page = MagicMock()
    event = MagicMock()
    source._process_change_result = AsyncMock(side_effect=[
        ([event], [MagicMock()]),
        RuntimeError("boom"),
    ])

    services.cursor.get_last_cursor.return_value = "start-token"
    client.list_changes = AsyncMock(return_value={
        "changes": [{"fileId": "ok"}, {"fileId": "bad"}],
        "newStartPageToken": "new-start",
    })

    await source.fetch_and_publish()

    source._commit_page.assert_not_called()


@pytest.mark.asyncio
async def test_writer_failure_does_not_apply_cache_or_advance_cursor(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    client = attach_client(source)
    source._ensure_baseline_state = MagicMock()
    event = MagicMock()
    source._process_change_result = AsyncMock(return_value=([event], []))
    source._commit_page = MagicMock(side_effect=RuntimeError("db down"))

    services.cursor.get_last_cursor.return_value = "start-token"
    client.list_changes = AsyncMock(return_value={
        "changes": [{"fileId": "f1"}],
        "newStartPageToken": "new-start",
    })

    await source.fetch_and_publish()

    source._commit_page.assert_called_once()


@pytest.mark.asyncio
async def test_export_size_limit_keeps_metadata_processing_live(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    previous = DriveFileSnapshot(
        file_id="f1",
        name="Large doc",
        mime_type="application/vnd.google-apps.document",
        parents=["root"],
        trashed=False,
        created_time="2026-04-01T00:00:00Z",
        modified_time="2026-04-01T00:00:00Z",
        owned_by_me=True,
        content_snapshot="last available content",
        content_hash=source.diff_calc.get_hash("last available content"),
    )
    source._get_cached_snapshot = MagicMock(return_value=previous)
    source._fetch_file = AsyncMock(return_value={
        "id": "f1",
        "name": "Large doc",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["root"],
        "trashed": False,
        "createdTime": "2026-04-01T00:00:00Z",
        "modifiedTime": "2026-04-01T01:00:00Z",
        "ownedByMe": True,
        "version": "2",
        "capabilities": {"canDownload": True},
    })
    client = MagicMock()
    client.export_file = AsyncMock(side_effect=DriveApiError(
        403,
        "This file is too large to be exported.",
        frozenset({"exportSizeLimitExceeded"}),
        "",
    ))

    events, mutations = await source._process_change_result(
        client,
        {"fileId": "f1", "time": "2026-04-01T01:00:00Z"},
        datetime.now(timezone.utc),
    )

    assert len(events) == 1
    assert events[0].event_type == GoogleDriveEventType.FILE_UPDATED
    assert len(mutations) == 1
    snapshot = mutations[0].snapshot
    assert snapshot is not None
    assert snapshot.content_unavailable is True
    assert snapshot.content_snapshot == "last available content"


@pytest.mark.asyncio
async def test_download_capability_false_skips_content_request(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    client = MagicMock()
    client.export_file = AsyncMock()

    content = await source._fetch_text_content(
        client,
        "f1",
        "application/vnd.google-apps.document",
        can_download=False,
    )

    assert content is None
    client.export_file.assert_not_awaited()


def test_native_workspace_diffing_is_limited_to_google_docs(services):
    source = GoogleDriveSource(
        "drive",
        make_config(eligible_mime_types_for_content_diff=[
            "application/vnd.google-apps.document",
            "application/vnd.google-apps.spreadsheet",
            "application/vnd.google-apps.presentation",
        ]),
        services,
        source_id=1,
    )

    assert source._is_diffable_mime("application/vnd.google-apps.document") is True
    assert source._is_diffable_mime("application/vnd.google-apps.spreadsheet") is False
    assert source._is_diffable_mime("application/vnd.google-apps.presentation") is False


@pytest.mark.asyncio
async def test_expired_token_reconciliation_emits_net_state_changes(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    previous = {
        "kept": DriveFileSnapshot(
            file_id="kept",
            name="Kept",
            mime_type="application/pdf",
            parents=["old"],
            trashed=False,
            created_time="2026-03-01T00:00:00Z",
            modified_time="2026-03-01T00:00:00Z",
            owned_by_me=True,
            version="1",
        ),
        "gone": DriveFileSnapshot(
            file_id="gone",
            name="Gone",
            mime_type="application/pdf",
            parents=["root"],
            trashed=False,
            created_time="2026-03-01T00:00:00Z",
            modified_time="2026-03-01T00:00:00Z",
            owned_by_me=True,
            version="1",
        ),
    }
    services.kv.list_keys_with_prefix.return_value = [
        source._snapshot_key("kept"),
        source._snapshot_key("gone"),
    ]
    source._get_cached_snapshot = MagicMock(side_effect=lambda file_id: previous.get(file_id))
    services.kv.get.side_effect = lambda source_id, key: {
        "trusted": True,
        "established_at": "2026-04-01T00:00:00+00:00",
        "config_fingerprint": source._config_fingerprint(),
    } if key == source.BASELINE_KEY else None
    source._commit_page = MagicMock()
    client = MagicMock()
    client.get_start_page_token = AsyncMock(return_value="fresh-token")
    client.list_files_page = AsyncMock(return_value={
        "files": [
            {
                "id": "kept",
                "name": "Kept",
                "mimeType": "application/pdf",
                "parents": ["new"],
                "trashed": False,
                "createdTime": "2026-03-01T00:00:00Z",
                "modifiedTime": "2026-04-02T00:00:00Z",
                "ownedByMe": True,
                "version": "2",
            },
            {
                "id": "new",
                "name": "New",
                "mimeType": "application/pdf",
                "parents": ["root"],
                "trashed": False,
                "createdTime": "2026-04-02T00:00:00Z",
                "modifiedTime": "2026-04-02T00:00:00Z",
                "ownedByMe": True,
                "version": "1",
            },
        ]
    })

    await source._reconcile_expired_page_token(client)

    events, mutations, cursor = source._commit_page.call_args.args[:3]
    assert cursor == "fresh-token"
    assert {event.event_type for event in events} == {
        GoogleDriveEventType.FILE_MOVED,
        GoogleDriveEventType.FILE_CREATED,
        GoogleDriveEventType.FILE_REMOVED,
    }
    assert {(mutation.action, mutation.file_id) for mutation in mutations} == {
        ("set", "kept"),
        ("set", "new"),
        ("delete", "gone"),
    }


@pytest.mark.asyncio
async def test_incomplete_reconciliation_listing_does_not_commit(services):
    source = GoogleDriveSource("drive", make_config(), services, source_id=1)
    source._commit_page = MagicMock()
    client = MagicMock()
    client.get_start_page_token = AsyncMock(return_value="fresh-token")
    client.list_files_page = AsyncMock(return_value={
        "files": [],
        "incompleteSearch": True,
    })

    with pytest.raises(RuntimeError, match="incomplete file listing"):
        await source._reconcile_expired_page_token(client)

    source._commit_page.assert_not_called()
