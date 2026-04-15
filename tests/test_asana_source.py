import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, timezone

from src.sources.asana import AsanaSource
from src.config import AsanaSourceConfig
from src.schemas import NewEvent


@pytest.fixture
def mock_kv():
    kv = MagicMock()
    kv.list_keys_with_prefix.return_value = []
    kv.get.return_value = None
    return kv


@pytest.fixture
def mock_writer():
    return MagicMock()


@pytest.fixture
def mock_services(mock_kv, mock_writer):
    services = MagicMock()
    services.kv = mock_kv
    services.writer = mock_writer
    services.add_task = MagicMock()
    return services


@pytest.fixture
def asana_config():
    return AsanaSourceConfig(
        access_token="test-token-123",
        project_gids=["111111"],
        poll_interval="1m",
        track_comments=True,
    )


@pytest.fixture
def source(asana_config, mock_services):
    return AsanaSource("test-asana", asana_config, mock_services, 1)


def _make_task_summary(gid, name="Test Task", modified_at="2024-03-22T14:55:00.000Z"):
    return {
        "gid": gid,
        "name": name,
        "assignee": {"gid": "user1", "name": "Alice"},
        "modified_at": modified_at,
        "completed": False,
    }


_SENTINEL = object()

def _make_full_task(gid, name="Test Task", assignee=_SENTINEL, completed=False,
                    modified_at="2024-03-22T14:55:00.000Z",
                    created_at="2024-03-20T10:00:00.000Z"):
    return {
        "gid": gid,
        "name": name,
        "assignee": {"gid": "user1", "name": "Alice"} if assignee is _SENTINEL else assignee,
        "due_on": "2024-04-01",
        "due_at": None,
        "start_on": None,
        "start_at": None,
        "completed": completed,
        "completed_at": None,
        "notes": "Some notes",
        "custom_fields": [],
        "modified_at": modified_at,
        "created_at": created_at,
        "liked": False,
        "num_likes": 0,
        "num_subtasks": 0,
        "tags": [],
        "memberships": [],
        "followers": [],
        "parent": None,
    }


# --- Field Discovery ---

@pytest.mark.asyncio
async def test_discover_custom_fields(source):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": [
            {"custom_field": {"gid": "cf1", "name": "Priority Level", "type": "enum"}},
            {"custom_field": {"gid": "cf2", "name": "Sprint", "type": "text"}},
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch.object(source.client, "get", return_value=mock_response):
        await source._discover_custom_fields()
        assert source.custom_field_map == {"cf1": "Priority Level", "cf2": "Sprint"}


@pytest.mark.asyncio
async def test_discover_custom_fields_error(source):
    with patch.object(source.client, "get", side_effect=Exception("API error")):
        await source._discover_custom_fields()
        assert source.custom_field_map == {}


# --- New Task ---

@pytest.mark.asyncio
async def test_new_task_emits_created_and_assigned(source, mock_kv, mock_writer):
    task_summary = _make_task_summary("task1")
    full_task = _make_full_task("task1")

    mock_kv.list_keys_with_prefix.return_value = []

    mock_list_resp = MagicMock()
    mock_list_resp.json.return_value = {"data": [task_summary], "next_page": None}
    mock_list_resp.raise_for_status = MagicMock()

    mock_detail_resp = MagicMock()
    mock_detail_resp.json.return_value = {"data": full_task}
    mock_detail_resp.raise_for_status = MagicMock()

    mock_stories_resp = MagicMock()
    mock_stories_resp.json.return_value = {"data": []}
    mock_stories_resp.raise_for_status = MagicMock()

    async def mock_get(url, **kwargs):
        if "/projects/" in url and "/tasks" in url:
            return mock_list_resp
        if "/tasks/" in url and "/stories" in url:
            return mock_stories_resp
        if "/tasks/" in url:
            return mock_detail_resp
        return MagicMock()

    with patch.object(source.client, "get", side_effect=mock_get):
        await source.fetch_and_publish()

    # Should emit task_created + task_assigned (since assignee is set)
    assert mock_writer.write_events.call_count == 2
    calls = mock_writer.write_events.call_args_list
    event_types = [calls[i][0][1][0].event_type for i in range(2)]
    assert "asana.task_created" in event_types
    assert "asana.task_assigned" in event_types

    # Verify KV was set
    mock_kv.set.assert_any_call(1, "project:111111:task:task1", full_task)


@pytest.mark.asyncio
async def test_new_task_no_assignee_no_assigned_event(source, mock_kv, mock_writer):
    task_summary = _make_task_summary("task2")
    task_summary["assignee"] = None
    full_task = _make_full_task("task2", assignee=None)

    mock_kv.list_keys_with_prefix.return_value = []

    mock_list_resp = MagicMock()
    mock_list_resp.json.return_value = {"data": [task_summary], "next_page": None}
    mock_list_resp.raise_for_status = MagicMock()

    mock_detail_resp = MagicMock()
    mock_detail_resp.json.return_value = {"data": full_task}
    mock_detail_resp.raise_for_status = MagicMock()

    mock_stories_resp = MagicMock()
    mock_stories_resp.json.return_value = {"data": []}
    mock_stories_resp.raise_for_status = MagicMock()

    async def mock_get(url, **kwargs):
        if "/projects/" in url and "/tasks" in url:
            return mock_list_resp
        if "/tasks/" in url and "/stories" in url:
            return mock_stories_resp
        if "/tasks/" in url:
            return mock_detail_resp
        return MagicMock()

    with patch.object(source.client, "get", side_effect=mock_get):
        await source.fetch_and_publish()

    # Only task_created, no task_assigned
    assert mock_writer.write_events.call_count == 1
    event = mock_writer.write_events.call_args_list[0][0][1][0]
    assert event.event_type == "asana.task_created"


# --- Updated Task ---

@pytest.mark.asyncio
async def test_existing_task_updated_emits_update(source, mock_kv, mock_writer):
    old_task = _make_full_task("task1", name="Old Name", modified_at="2024-03-22T14:00:00.000Z")
    new_summary = _make_task_summary("task1", modified_at="2024-03-22T15:00:00.000Z")
    new_full = _make_full_task("task1", name="New Name", modified_at="2024-03-22T15:00:00.000Z")

    mock_kv.list_keys_with_prefix.return_value = ["project:111111:task:task1"]
    mock_kv.get.side_effect = lambda sid, key: old_task if "task:task1" in key else None

    mock_list_resp = MagicMock()
    mock_list_resp.json.return_value = {"data": [new_summary], "next_page": None}
    mock_list_resp.raise_for_status = MagicMock()

    mock_detail_resp = MagicMock()
    mock_detail_resp.json.return_value = {"data": new_full}
    mock_detail_resp.raise_for_status = MagicMock()

    mock_stories_resp = MagicMock()
    mock_stories_resp.json.return_value = {"data": []}
    mock_stories_resp.raise_for_status = MagicMock()

    async def mock_get(url, **kwargs):
        if "/projects/" in url and "/tasks" in url:
            return mock_list_resp
        if "/tasks/" in url and "/stories" in url:
            return mock_stories_resp
        if "/tasks/" in url:
            return mock_detail_resp
        return MagicMock()

    with patch.object(source.client, "get", side_effect=mock_get):
        await source.fetch_and_publish()

    # Should emit task_updated
    event_types = [c[0][1][0].event_type for c in mock_writer.write_events.call_args_list]
    assert "asana.task_updated" in event_types

    # Check diff contains name change
    for call in mock_writer.write_events.call_args_list:
        ev = call[0][1][0]
        if ev.event_type == "asana.task_updated":
            assert "name" in ev.data["diff"]
            assert ev.data["diff"]["name"]["before"] == "Old Name"
            assert ev.data["diff"]["name"]["after"] == "New Name"


# --- Assignee Changes ---

@pytest.mark.asyncio
async def test_assignee_change_emits_assigned(source, mock_kv, mock_writer):
    old_task = _make_full_task("task1", assignee=None, modified_at="2024-03-22T14:00:00.000Z")
    new_summary = _make_task_summary("task1", modified_at="2024-03-22T15:00:00.000Z")
    new_full = _make_full_task("task1", assignee={"gid": "user1", "name": "Alice"},
                               modified_at="2024-03-22T15:00:00.000Z")

    mock_kv.list_keys_with_prefix.return_value = ["project:111111:task:task1"]
    mock_kv.get.side_effect = lambda sid, key: old_task if "task:task1" in key else None

    mock_list_resp = MagicMock()
    mock_list_resp.json.return_value = {"data": [new_summary], "next_page": None}
    mock_list_resp.raise_for_status = MagicMock()

    mock_detail_resp = MagicMock()
    mock_detail_resp.json.return_value = {"data": new_full}
    mock_detail_resp.raise_for_status = MagicMock()

    mock_stories_resp = MagicMock()
    mock_stories_resp.json.return_value = {"data": []}
    mock_stories_resp.raise_for_status = MagicMock()

    async def mock_get(url, **kwargs):
        if "/projects/" in url and "/tasks" in url:
            return mock_list_resp
        if "/tasks/" in url and "/stories" in url:
            return mock_stories_resp
        if "/tasks/" in url:
            return mock_detail_resp
        return MagicMock()

    with patch.object(source.client, "get", side_effect=mock_get):
        await source.fetch_and_publish()

    event_types = [c[0][1][0].event_type for c in mock_writer.write_events.call_args_list]
    assert "asana.task_assigned" in event_types


@pytest.mark.asyncio
async def test_assignee_removed_emits_unassigned(source, mock_kv, mock_writer):
    old_task = _make_full_task("task1", assignee={"gid": "user1", "name": "Alice"},
                               modified_at="2024-03-22T14:00:00.000Z")
    new_summary = _make_task_summary("task1", modified_at="2024-03-22T15:00:00.000Z")
    new_summary["assignee"] = None
    new_full = _make_full_task("task1", assignee=None, modified_at="2024-03-22T15:00:00.000Z")

    mock_kv.list_keys_with_prefix.return_value = ["project:111111:task:task1"]
    mock_kv.get.side_effect = lambda sid, key: old_task if "task:task1" in key else None

    mock_list_resp = MagicMock()
    mock_list_resp.json.return_value = {"data": [new_summary], "next_page": None}
    mock_list_resp.raise_for_status = MagicMock()

    mock_detail_resp = MagicMock()
    mock_detail_resp.json.return_value = {"data": new_full}
    mock_detail_resp.raise_for_status = MagicMock()

    mock_stories_resp = MagicMock()
    mock_stories_resp.json.return_value = {"data": []}
    mock_stories_resp.raise_for_status = MagicMock()

    async def mock_get(url, **kwargs):
        if "/projects/" in url and "/tasks" in url:
            return mock_list_resp
        if "/tasks/" in url and "/stories" in url:
            return mock_stories_resp
        if "/tasks/" in url:
            return mock_detail_resp
        return MagicMock()

    with patch.object(source.client, "get", side_effect=mock_get):
        await source.fetch_and_publish()

    event_types = [c[0][1][0].event_type for c in mock_writer.write_events.call_args_list]
    assert "asana.task_unassigned" in event_types


# --- Removed Task ---

@pytest.mark.asyncio
async def test_removed_task_emits_removed(source, mock_kv, mock_writer):
    cached_task = _make_full_task("task1")
    mock_kv.list_keys_with_prefix.return_value = ["project:111111:task:task1"]
    mock_kv.get.return_value = cached_task

    mock_list_resp = MagicMock()
    mock_list_resp.json.return_value = {"data": [], "next_page": None}
    mock_list_resp.raise_for_status = MagicMock()

    with patch.object(source.client, "get", return_value=mock_list_resp):
        await source.fetch_and_publish()

    assert mock_writer.write_events.call_count == 1
    event = mock_writer.write_events.call_args[0][1][0]
    assert event.event_type == "asana.task_removed"
    assert event.entity_id == "task1"
    mock_kv.delete.assert_any_call(1, "project:111111:task:task1")


# --- Comments ---

@pytest.mark.asyncio
async def test_new_comment_emits_commented(source, mock_kv, mock_writer):
    old_task = _make_full_task("task1", modified_at="2024-03-22T14:00:00.000Z")
    # Same modified_at so no update event, but new comment
    new_summary = _make_task_summary("task1", modified_at="2024-03-22T14:00:00.000Z")

    mock_kv.list_keys_with_prefix.return_value = ["project:111111:task:task1"]

    def kv_get(sid, key):
        if "task:task1" in key:
            return old_task
        if "comments:task1" in key:
            return ["comment1"]
        return None

    mock_kv.get.side_effect = kv_get

    mock_list_resp = MagicMock()
    mock_list_resp.json.return_value = {"data": [new_summary], "next_page": None}
    mock_list_resp.raise_for_status = MagicMock()

    mock_stories_resp = MagicMock()
    mock_stories_resp.json.return_value = {
        "data": [
            {"gid": "comment1", "type": "comment", "text": "Old comment",
             "created_at": "2024-03-22T13:00:00.000Z", "created_by": {"name": "Alice"}},
            {"gid": "comment2", "type": "comment", "text": "New comment!",
             "created_at": "2024-03-22T14:30:00.000Z", "created_by": {"name": "Bob"}},
        ]
    }
    mock_stories_resp.raise_for_status = MagicMock()

    async def mock_get(url, **kwargs):
        if "/projects/" in url and "/tasks" in url:
            return mock_list_resp
        if "/stories" in url:
            return mock_stories_resp
        return MagicMock()

    with patch.object(source.client, "get", side_effect=mock_get):
        await source.fetch_and_publish()

    event_types = [c[0][1][0].event_type for c in mock_writer.write_events.call_args_list]
    assert "asana.task_commented" in event_types

    # Find the comment event
    for call in mock_writer.write_events.call_args_list:
        ev = call[0][1][0]
        if ev.event_type == "asana.task_commented":
            assert ev.data["text"] == "New comment!"
            assert ev.data["author"] == "Bob"
            assert ev.data["comment_gid"] == "comment2"


# --- Completion ---

@pytest.mark.asyncio
async def test_task_completed_emits_completed(source, mock_kv, mock_writer):
    old_task = _make_full_task("task1", completed=False, modified_at="2024-03-22T14:00:00.000Z")
    new_summary = _make_task_summary("task1", modified_at="2024-03-22T15:00:00.000Z")
    new_full = _make_full_task("task1", completed=True, modified_at="2024-03-22T15:00:00.000Z")

    mock_kv.list_keys_with_prefix.return_value = ["project:111111:task:task1"]
    mock_kv.get.side_effect = lambda sid, key: old_task if "task:task1" in key else None

    mock_list_resp = MagicMock()
    mock_list_resp.json.return_value = {"data": [new_summary], "next_page": None}
    mock_list_resp.raise_for_status = MagicMock()

    mock_detail_resp = MagicMock()
    mock_detail_resp.json.return_value = {"data": new_full}
    mock_detail_resp.raise_for_status = MagicMock()

    mock_stories_resp = MagicMock()
    mock_stories_resp.json.return_value = {"data": []}
    mock_stories_resp.raise_for_status = MagicMock()

    async def mock_get(url, **kwargs):
        if "/projects/" in url and "/tasks" in url:
            return mock_list_resp
        if "/tasks/" in url and "/stories" in url:
            return mock_stories_resp
        if "/tasks/" in url:
            return mock_detail_resp
        return MagicMock()

    with patch.object(source.client, "get", side_effect=mock_get):
        await source.fetch_and_publish()

    event_types = [c[0][1][0].event_type for c in mock_writer.write_events.call_args_list]
    assert "asana.task_completed" in event_types


# --- Diff / Ignored Fields ---

def test_compute_diff_basic(source):
    old = _make_full_task("t1", name="Old")
    new = _make_full_task("t1", name="New")
    diff = source._compute_diff(old, new)
    assert "name" in diff
    assert diff["name"]["before"] == "Old"
    assert diff["name"]["after"] == "New"


def test_compute_diff_ignored_fields(source):
    source.config.ignored_fields = ["notes"]
    old = _make_full_task("t1")
    new = _make_full_task("t1")
    new["notes"] = "Changed notes"
    diff = source._compute_diff(old, new)
    assert "notes" not in diff


def test_compute_diff_custom_fields(source):
    source.custom_field_map = {"cf1": "Priority Level"}
    old = _make_full_task("t1")
    old["custom_fields"] = [{"gid": "cf1", "name": "Priority Level", "display_value": "High"}]
    new = _make_full_task("t1")
    new["custom_fields"] = [{"gid": "cf1", "name": "Priority Level", "display_value": "Low"}]
    diff = source._compute_diff(old, new)
    assert "Priority Level" in diff
    assert diff["Priority Level"]["before"] == "High"
    assert diff["Priority Level"]["after"] == "Low"


# --- Helpers ---

def test_simplify_value(source):
    assert source._simplify_value(None) is None
    assert source._simplify_value("hello") == "hello"
    assert source._simplify_value({"name": "Alice", "gid": "123"}) == "Alice"
    assert source._simplify_value({"gid": "123"}) == "123"
    assert source._simplify_value([{"name": "A"}, {"name": "B"}]) == ["A", "B"]


def test_extract_assignee_name(source):
    assert source._extract_assignee_name({"assignee": {"gid": "1", "name": "Alice"}}) == "Alice"
    assert source._extract_assignee_name({"assignee": None}) is None
    assert source._extract_assignee_name({}) is None


def test_parse_asana_date(source):
    dt = source._parse_asana_date("2024-03-22T14:55:00.000Z")
    assert dt is not None
    assert dt.year == 2024
    assert dt.month == 3
    assert dt.tzinfo is not None

    assert source._parse_asana_date(None) is None
    assert source._parse_asana_date("invalid") is None


# --- Pagination ---

@pytest.mark.asyncio
async def test_list_project_tasks_pagination(source):
    page1 = MagicMock()
    page1.json.return_value = {
        "data": [_make_task_summary("t1")],
        "next_page": {"offset": "abc123"},
    }
    page1.raise_for_status = MagicMock()

    page2 = MagicMock()
    page2.json.return_value = {
        "data": [_make_task_summary("t2")],
        "next_page": None,
    }
    page2.raise_for_status = MagicMock()

    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        return page1 if call_count == 1 else page2

    with patch.object(source.client, "get", side_effect=mock_get):
        tasks = await source._list_project_tasks("111111")

    assert len(tasks) == 2
    assert tasks[0]["gid"] == "t1"
    assert tasks[1]["gid"] == "t2"


# --- Empty state ---

@pytest.mark.asyncio
async def test_empty_project_no_events(source, mock_kv, mock_writer):
    mock_kv.list_keys_with_prefix.return_value = []

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": [], "next_page": None}
    mock_resp.raise_for_status = MagicMock()

    with patch.object(source.client, "get", return_value=mock_resp):
        await source.fetch_and_publish()

    mock_writer.write_events.assert_not_called()
