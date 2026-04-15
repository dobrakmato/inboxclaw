import pytest
import asyncio
import httpx
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
import json

from src.sources.jira import JiraSource
from src.config import JiraSourceConfig
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
def jira_config():
    return JiraSourceConfig(
        url="https://test.atlassian.net",
        email="test@example.com",
        api_token="token123",
        jql="assignee = currentUser()",
        poll_interval="1m"
    )

@pytest.fixture
def source(jira_config, mock_services):
    return JiraSource("test-jira", jira_config, mock_services, 1)

def _make_issue(key, summary="Test Issue", updated="2024-03-22T14:55:00.000+0000"):
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "updated": updated,
            "status": {"name": "To Do"},
            "priority": {"name": "Medium"},
            "issuetype": {"name": "Task"},
            "project": {"name": "Test Project"}
        }
    }

@pytest.mark.asyncio
async def test_discover_fields(source):
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"id": "summary", "name": "Summary"},
        {"id": "status", "name": "Status"}
    ]
    mock_response.raise_for_status = MagicMock()

    with patch.object(source.client, 'get', return_value=mock_response) as mock_get:
        await source._discover_fields()
        mock_get.assert_called_once_with("/rest/api/3/field")
        assert source.field_map == {"summary": "Summary", "status": "Status"}

@pytest.mark.asyncio
async def test_fetch_and_publish_new_issue(source, mock_services, mock_kv, mock_writer):
    # Mock field discovery
    source.field_map = {"summary": "Summary", "status": "Status"}
    
    # Mock search results: one new issue
    issue = _make_issue("PROJ-1")
    mock_search_response = MagicMock()
    mock_search_response.json.return_value = {
        "issues": [issue],
        "total": 1
    }
    
    # New issue also needs full detail fetch + comments fetch
    full_issue = _make_issue("PROJ-1")
    full_issue["fields"]["description"] = "Full description"
    mock_detail_response = MagicMock()
    mock_detail_response.json.return_value = full_issue
    
    mock_comments_response = MagicMock()
    mock_comments_response.json.return_value = {"comments": []}
    mock_comments_response.raise_for_status = MagicMock()
    
    # Mock KV state: empty
    mock_kv.list_keys_with_prefix.return_value = []
    
    with patch.object(source.client, 'post', return_value=mock_search_response), \
         patch.object(source.client, 'get') as mock_get:
        mock_get.side_effect = [mock_detail_response, mock_comments_response]
        await source.fetch_and_publish()
        
        # Verify KV set with FULL issue and comments
        mock_kv.set.assert_any_call(source.source_id, "issue:PROJ-1", full_issue)
        mock_kv.set.assert_any_call(source.source_id, "comments:PROJ-1", [])
        
        # Verify event emitted
        mock_writer.write_events.assert_called_once()
        events = mock_writer.write_events.call_args[0][1]
        assert len(events) == 1
        assert events[0].event_type == "jira.task_assigned"
        assert events[0].entity_id == "PROJ-1"
        assert events[0].data["summary"] == "Test Issue"
        assert events[0].data["full_issue"]["fields"]["description"] == "Full description"

@pytest.mark.asyncio
async def test_fetch_and_publish_updated_issue(source, mock_services, mock_kv, mock_writer):
    source.field_map = {"summary": "Summary", "status": "Status", "description": "Description"}
    
    # Old issue in KV
    old_issue = _make_issue("PROJ-1", updated="2024-01-01T00:00:00.000+0000")
    mock_kv.list_keys_with_prefix.return_value = ["issue:PROJ-1"]
    mock_kv.get.side_effect = lambda sid, key: old_issue if key == "issue:PROJ-1" else []
    
    # New issue from search
    new_issue_summary = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")
    mock_search_response = MagicMock()
    mock_search_response.json.return_value = {"issues": [new_issue_summary], "total": 1}
    
    # Full issue detail (with more fields)
    full_issue = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")
    full_issue["fields"]["description"] = "New description"
    mock_detail_response = MagicMock()
    mock_detail_response.json.return_value = full_issue
    
    # Changelog
    mock_changelog_response = MagicMock()
    mock_changelog_response.json.return_value = {"values": [{"id": "101", "items": [{"field": "description", "toString": "New description"}]}]}
    
    # Comments
    mock_comments_response = MagicMock()
    mock_comments_response.json.return_value = {"comments": []}
    mock_comments_response.raise_for_status = MagicMock()
    
    with patch.object(source.client, 'post', return_value=mock_search_response), \
         patch.object(source.client, 'get') as mock_get:
        
        mock_get.side_effect = [mock_detail_response, mock_changelog_response, mock_comments_response]
        
        await source.fetch_and_publish()
        
        # Verify KV update
        mock_kv.set.assert_any_call(source.source_id, "issue:PROJ-1", full_issue)
        
        # Verify update event
        mock_writer.write_events.assert_called_once()
        events = mock_writer.write_events.call_args[0][1]
        assert events[0].event_type == "jira.task_updated"
        assert "Description" in events[0].data["diff"]
        assert events[0].data["diff"]["Description"]["after"] == "New description"

@pytest.mark.asyncio
async def test_fetch_and_publish_unassigned_issue(source, mock_services, mock_kv, mock_writer):
    # Old issue in KV
    old_issue = _make_issue("PROJ-1")
    mock_kv.list_keys_with_prefix.return_value = ["issue:PROJ-1"]
    mock_kv.get.return_value = old_issue
    
    # Search returns NOTHING
    mock_search_response = MagicMock()
    mock_search_response.json.return_value = {"issues": [], "total": 0}
    
    with patch.object(source.client, 'post', return_value=mock_search_response):
        await source.fetch_and_publish()
        
        # Verify KV deletion
        mock_kv.delete.assert_any_call(source.source_id, "issue:PROJ-1")
        mock_kv.delete.assert_any_call(source.source_id, "comments:PROJ-1")
        
        # Verify unassigned event
        mock_writer.write_events.assert_called_once()
        events = mock_writer.write_events.call_args[0][1]
        assert events[0].event_type == "jira.task_unassigned"
        assert events[0].entity_id == "PROJ-1"
        assert events[0].data["status"] == "To Do"
        assert events[0].data["project"] == "Test Project"

@pytest.mark.asyncio
async def test_compute_diff(source):
    source.field_map = {"summary": "Summary", "status": "Status"}
    old_issue = {"fields": {"summary": "Old", "status": {"name": "Open"}, "updated": "old-date"}}
    new_issue = {"fields": {"summary": "New", "status": {"name": "Closed"}, "updated": "new-date"}}
    
    # 'updated' is ignored by default
    diff = source._compute_diff(old_issue, new_issue)
    
    assert "Summary" in diff
    assert diff["Summary"]["before"] == "Old"
    assert diff["Summary"]["after"] == "New"
    assert "Status" in diff
    assert diff["Status"]["before"] == "Open"
    assert diff["Status"]["after"] == "Closed"
    assert "updated" not in diff
    assert "Updated" not in diff

@pytest.mark.asyncio
async def test_search_issues_pagination(source):
    # Mock multiple pages of search results
    mock_response_1 = MagicMock()
    mock_response_1.json.return_value = {
        "issues": [{"key": f"PROJ-{i}"} for i in range(100)],
        "total": 150
    }
    mock_response_1.raise_for_status = MagicMock()

    mock_response_2 = MagicMock()
    mock_response_2.json.return_value = {
        "issues": [{"key": f"PROJ-{i}"} for i in range(100, 150)],
        "total": 150
    }
    mock_response_2.raise_for_status = MagicMock()

    with patch.object(source.client, 'post') as mock_post:
        mock_post.side_effect = [mock_response_1, mock_response_2]
        
        issues = await source._search_issues()
        
        assert len(issues) == 150
        assert mock_post.call_count == 2
        
        # Check second call parameters
        second_call_payload = mock_post.call_args_list[1][1]["json"]
        assert second_call_payload["startAt"] == 100

@pytest.mark.asyncio
async def test_discover_fields_error_handling(source):
    # Test that error in field discovery is logged but doesn't crash
    with patch.object(source.client, 'get', side_effect=Exception("API Error")):
        await source._discover_fields()
        assert source.field_map == {}

@pytest.mark.asyncio
async def test_fetch_detail_error_handling(source, mock_kv):
    # Test that error in detail fetch is handled in _handle_existing_issue
    source.field_map = {"summary": "Summary"}
    old_issue = _make_issue("PROJ-1", updated="2024-01-01")
    new_issue_summary = _make_issue("PROJ-1", updated="2024-01-02")
    
    mock_kv.get.return_value = old_issue

    with patch.object(source.client, 'get', side_effect=httpx.HTTPStatusError("Error", request=MagicMock(), response=MagicMock(status_code=500))):
        # This will raise HTTPStatusError because _handle_existing_issue calls _fetch_issue_detail which doesn't catch it
        with pytest.raises(httpx.HTTPStatusError):
            await source._handle_existing_issue(new_issue_summary)

@pytest.mark.asyncio
async def test_fetch_and_publish_partial_failure(source, mock_services, mock_kv, mock_writer):
    # Test that one failure doesn't stop other issues from being processed
    source.field_map = {"summary": "Summary"}
    
    # 2 issues: one updated, one new
    issue1 = _make_issue("PROJ-1", updated="2024-01-02")
    issue2 = _make_issue("PROJ-2")
    
    mock_search_response = MagicMock()
    mock_search_response.json.return_value = {"issues": [issue1, issue2], "total": 2}
    
    # PROJ-1 is existing but detail fetch will fail
    mock_kv.list_keys_with_prefix.return_value = ["issue:PROJ-1"]
    old_proj1 = _make_issue("PROJ-1", updated="2024-01-01")
    mock_kv.get.side_effect = lambda sid, key: old_proj1 if key == "issue:PROJ-1" else []
    
    with patch.object(source.client, 'post', return_value=mock_search_response), \
         patch.object(source.client, 'get') as mock_get:
        
        # Mock responses
        res1 = MagicMock()
        res1.raise_for_status.side_effect = Exception("Detail fetch failed")
        
        res2 = MagicMock()
        res2.json.return_value = _make_issue("PROJ-2")
        res2.raise_for_status = MagicMock()
        
        res_comments = MagicMock()
        res_comments.json.return_value = {"comments": []}
        res_comments.raise_for_status = MagicMock()

        # PROJ-1 detail fail, then PROJ-2 detail success, then PROJ-2 comments
        mock_get.side_effect = [res1, res2, res_comments]

        await source.fetch_and_publish()
        
        # PROJ-2 should still be handled as new issue
        mock_kv.set.assert_any_call(source.source_id, "issue:PROJ-2", _make_issue("PROJ-2"))
        
        # We should have at least the event for PROJ-2
        assert mock_writer.write_events.call_count >= 1
        all_events = []
        for call in mock_writer.write_events.call_args_list:
            all_events.extend(call[0][1])
        
        assert any(e.entity_id == "PROJ-2" for e in all_events)

@pytest.mark.asyncio
async def test_compute_diff_ignored_fields(source):
    source.field_map = {"summary": "Summary", "status": "Status", "customfield_101": "Story Points"}
    source.config.ignored_fields = ["status", "updated"]
    
    old_issue = {
        "fields": {
            "summary": "Old Summary",
            "status": {"name": "To Do"},
            "customfield_101": 5,
            "updated": "2024-01-01T00:00:00.000+0000"
        }
    }
    new_issue = {
        "fields": {
            "summary": "New Summary",
            "status": {"name": "In Progress"},
            "customfield_101": 8,
            "updated": "2024-01-02T00:00:00.000+0000"
        }
    }
    
    diff = source._compute_diff(old_issue, new_issue)
    
    # Should contain summary and story points
    assert "Summary" in diff
    assert "Story Points" in diff
    # Should NOT contain status because it's ignored
    assert "Status" not in diff
    # Should NOT contain updated because it's ignored
    assert "updated" not in diff

@pytest.mark.asyncio
async def test_fetch_and_publish_changelog_filtering(source, mock_services, mock_kv, mock_writer):
    # This test will fail until we implement changelog filtering
    source.field_map = {"summary": "Summary", "status": "Status", "description": "Description"}
    source.config.ignored_fields = ["status"]
    
    old_issue = _make_issue("PROJ-1", updated="2024-01-01T00:00:00.000+0000")
    mock_kv.list_keys_with_prefix.return_value = ["issue:PROJ-1"]
    mock_kv.get.side_effect = lambda sid, key: old_issue if key == "issue:PROJ-1" else []
    
    new_issue = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")
    new_issue["fields"]["description"] = "New description"
    new_issue["fields"]["status"]["name"] = "In Progress"
    
    mock_search_response = MagicMock()
    mock_search_response.json.return_value = {"issues": [new_issue], "total": 1}
    
    mock_detail_response = MagicMock()
    mock_detail_response.json.return_value = new_issue
    
    # Changelog has an item for description (not ignored) and status (ignored)
    mock_changelog_response = MagicMock()
    mock_changelog_response.json.return_value = {
        "values": [
            {
                "id": "101",
                "items": [
                    {"field": "description", "fieldId": "description", "toString": "New description"},
                    {"field": "status", "fieldId": "status", "toString": "In Progress"}
                ]
            }
        ]
    }
    
    mock_comments_response = MagicMock()
    mock_comments_response.json.return_value = {"comments": []}
    mock_comments_response.raise_for_status = MagicMock()
    
    with patch.object(source.client, 'post', return_value=mock_search_response), \
         patch.object(source.client, 'get') as mock_get:
        
        mock_get.side_effect = [mock_detail_response, mock_changelog_response, mock_comments_response]
        
        await source.fetch_and_publish()
        
        # Verify event emitted
        events = mock_writer.write_events.call_args[0][1]
        # Description change -> task_updated (status is ignored so no status_changed event)
        assert any(e.event_type == "jira.task_updated" for e in events)
        updated_event = [e for e in events if e.event_type == "jira.task_updated"][0]
        
        changelog = updated_event.data["changelog"]
        assert len(changelog) == 1
        items = changelog[0]["items"]
        # Only description should be present in the items if we filter it
        assert len(items) == 1
        assert items[0]["field"] == "description"
        assert not any(i["field"] == "status" for i in items)

@pytest.mark.asyncio
async def test_search_issues_with_custom_fields(source):
    # Mock field discovery with a custom field
    source.field_map = {"summary": "Summary", "customfield_101": "Story Points"}
    
    mock_response = MagicMock()
    mock_response.json.return_value = {"issues": [], "total": 0}
    mock_response.raise_for_status = MagicMock()

    with patch.object(source.client, 'post', return_value=mock_response) as mock_post:
        await source._search_issues()
        
        # Verify custom field was included in the search
        payload = mock_post.call_args[1]["json"]
        assert "customfield_101" in payload["fields"]
        assert "summary" in payload["fields"]

def test_simplify_field_value_comprehensive(source):
    # Standard objects
    assert source._simplify_field_value({"name": "Task"}) == "Task"
    assert source._simplify_field_value({"displayName": "Joe"}) == "Joe"
    assert source._simplify_field_value({"value": "A"}) == "A"
    assert source._simplify_field_value({"label": "L"}) == "L"
    assert source._simplify_field_value({"emailAddress": "joe@test.com"}) == "joe@test.com"
    
    # List of objects
    assert source._simplify_field_value([{"name": "a"}, {"name": "b"}]) == ["a", "b"]
    
    # Nested and complex
    assert source._simplify_field_value({"something": "else"}) == {"something": "else"}
    
    # Null and primitive
    assert source._simplify_field_value(None) is None
    assert source._simplify_field_value("string") == "string"
    assert source._simplify_field_value(123) == 123

def test_parse_jira_date_comprehensive(source):
    # Standard Jira format
    dt = source._parse_jira_date("2024-03-22T14:55:00.000+0530")
    assert dt.hour == 14 and dt.minute == 55
    assert dt.tzinfo is not None
    
    # ISO format with Z
    dt = source._parse_jira_date("2024-03-22T14:55:00Z")
    assert dt.hour == 14 and dt.tzinfo == timezone.utc
    
    # Simple date
    dt = source._parse_jira_date("2024-03-22")
    assert dt.year == 2024 and dt.month == 3
    
    # Malformed - should return current time or something not crashing
    dt = source._parse_jira_date("invalid")
    assert isinstance(dt, datetime)

@pytest.mark.asyncio
async def test_simplify_field_value_edge_cases(source):
    # Test complex/nested structures
    assert source._simplify_field_value({"some": {"nested": "value"}}) == {"some": {"nested": "value"}}
    # Test list of non-dict items
    assert source._simplify_field_value([1, 2, 3]) == [1, 2, 3]
    # Test empty list and empty dict
    assert source._simplify_field_value([]) == []
    assert source._simplify_field_value({}) == {}

@pytest.mark.asyncio
async def test_fetch_and_publish_empty_search(source, mock_services, mock_kv, mock_writer):
    # Search returns NOTHING
    mock_search_response = MagicMock()
    mock_search_response.json.return_value = {"issues": [], "total": 0}
    
    # KV is empty too
    mock_kv.list_keys_with_prefix.return_value = []
    
    with patch.object(source.client, 'post', return_value=mock_search_response):
        await source.fetch_and_publish()
        
        # No events should be emitted, no KV updates
        mock_writer.write_events.assert_not_called()
        mock_kv.set.assert_not_called()
        mock_kv.delete.assert_not_called()

@pytest.mark.asyncio
async def test_fetch_and_publish_search_error(source, mock_services, mock_kv, mock_writer):
    # Search API error should be propagated (handled by main loop in run())
    with patch.object(source.client, 'post', side_effect=Exception("API Down")):
        with pytest.raises(Exception, match="API Down"):
            await source.fetch_and_publish()

def test_parse_jira_date_negative_offset_without_colon(source):
    """Jira can return dates with negative offsets like -0530 (no colon)."""
    dt = source._parse_jira_date("2024-03-22T14:55:00.000-0530")
    assert dt.year == 2024
    assert dt.hour == 14
    assert dt.minute == 55
    assert dt.tzinfo is not None

@pytest.mark.asyncio
async def test_handle_removed_issue_with_none_fields(source, mock_kv, mock_writer):
    """When Jira fields like priority/assignee are None, _handle_removed_issue should not crash."""
    cached_issue = {
        "key": "PROJ-1",
        "fields": {
            "summary": "Test",
            "status": None,
            "priority": None,
            "assignee": None,
            "issuetype": None,
            "project": None,
            "updated": "2024-01-01T00:00:00.000+0000"
        }
    }
    mock_kv.get.return_value = cached_issue
    
    await source._handle_removed_issue("PROJ-1")
    
    mock_writer.write_events.assert_called_once()
    events = mock_writer.write_events.call_args[0][1]
    assert events[0].event_type == "jira.task_unassigned"
    assert events[0].data["status"] is None
    assert events[0].data["priority"] is None

@pytest.mark.asyncio
async def test_handle_new_issue_with_none_fields(source, mock_kv, mock_writer):
    """When Jira fields like priority/status are None, _handle_new_issue should not crash."""
    issue = {
        "key": "PROJ-1",
        "fields": {
            "summary": "Test",
            "status": None,
            "priority": None,
            "issuetype": None,
            "project": None,
            "updated": "2024-01-01T00:00:00.000+0000"
        }
    }
    mock_detail = MagicMock()
    mock_detail.json.return_value = issue
    mock_detail.raise_for_status = MagicMock()
    
    mock_comments = MagicMock()
    mock_comments.json.return_value = {"comments": []}
    mock_comments.raise_for_status = MagicMock()
    
    with patch.object(source.client, 'get') as mock_get:
        mock_get.side_effect = [mock_detail, mock_comments]
        await source._handle_new_issue(issue)
    
    mock_writer.write_events.assert_called_once()
    events = mock_writer.write_events.call_args[0][1]
    assert events[0].data["status"] is None
    assert events[0].data["priority"] is None


# --- Helper for existing-issue tests ---

def _setup_existing_issue_test(source, mock_kv, old_issue, new_issue, changelog_values=None, comments=None, cached_comments=None):
    """Helper to set up mocks for _handle_existing_issue tests."""
    source.field_map = {"summary": "Summary", "status": "Status", "assignee": "Assignee",
                        "priority": "Priority", "description": "Description",
                        "issuetype": "Issue Type", "project": "Project"}

    mock_kv.get.side_effect = lambda sid, key: (
        old_issue if key == f"issue:{old_issue['key']}" else
        (cached_comments or []) if key == f"comments:{old_issue['key']}" else None
    )

    mock_detail = MagicMock()
    mock_detail.json.return_value = new_issue
    mock_detail.raise_for_status = MagicMock()

    mock_changelog = MagicMock()
    mock_changelog.json.return_value = {"values": changelog_values or []}
    mock_changelog.raise_for_status = MagicMock()

    mock_comments_resp = MagicMock()
    mock_comments_resp.json.return_value = {"comments": comments or []}
    mock_comments_resp.raise_for_status = MagicMock()

    return [mock_detail, mock_changelog, mock_comments_resp]


@pytest.mark.asyncio
async def test_status_changed_event(source, mock_kv, mock_writer):
    """Status change emits jira.task_status_changed instead of generic task_updated."""
    old = _make_issue("PROJ-1", updated="2024-01-01T00:00:00.000+0000")
    new = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")
    new["fields"]["status"] = {"name": "In Progress"}

    mocks = _setup_existing_issue_test(source, mock_kv, old, new)

    with patch.object(source.client, 'get') as mock_get:
        mock_get.side_effect = mocks
        await source._handle_existing_issue(
            _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000"))

    events = mock_writer.write_events.call_args[0][1]
    assert len(events) == 1
    assert events[0].event_type == "jira.task_status_changed"
    assert events[0].entity_id == "PROJ-1"
    assert events[0].data["status_before"] == "To Do"
    assert events[0].data["status_after"] == "In Progress"
    assert "jira-PROJ-1-status-" in events[0].event_id


@pytest.mark.asyncio
async def test_reassigned_event(source, mock_kv, mock_writer):
    """Assignee change emits jira.task_reassigned."""
    old = _make_issue("PROJ-1", updated="2024-01-01T00:00:00.000+0000")
    old["fields"]["assignee"] = {"displayName": "Alice"}
    new = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")
    new["fields"]["assignee"] = {"displayName": "Bob"}

    mocks = _setup_existing_issue_test(source, mock_kv, old, new)

    with patch.object(source.client, 'get') as mock_get:
        mock_get.side_effect = mocks
        await source._handle_existing_issue(
            _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000"))

    events = mock_writer.write_events.call_args[0][1]
    assert len(events) == 1
    assert events[0].event_type == "jira.task_reassigned"
    assert events[0].data["assignee_before"] == "Alice"
    assert events[0].data["assignee_after"] == "Bob"


@pytest.mark.asyncio
async def test_comment_added_event(source, mock_kv, mock_writer):
    """New comment emits jira.comment_added."""
    old = _make_issue("PROJ-1", updated="2024-01-01T00:00:00.000+0000")
    new = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")

    new_comment = {
        "id": "10042",
        "author": {"displayName": "Jane"},
        "body": "Looks good!",
        "created": "2024-01-02T09:00:00.000+0000",
        "updated": "2024-01-02T09:00:00.000+0000"
    }

    mocks = _setup_existing_issue_test(source, mock_kv, old, new,
                                        comments=[new_comment], cached_comments=[])

    with patch.object(source.client, 'get') as mock_get:
        mock_get.side_effect = mocks
        await source._handle_existing_issue(
            _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000"))

    events = mock_writer.write_events.call_args[0][1]
    comment_events = [e for e in events if e.event_type == "jira.comment_added"]
    assert len(comment_events) == 1
    assert comment_events[0].data["comment_id"] == "10042"
    assert comment_events[0].data["author"] == "Jane"
    assert comment_events[0].data["body"] == "Looks good!"
    assert comment_events[0].event_id == "jira-PROJ-1-comment-10042-created"


@pytest.mark.asyncio
async def test_comment_updated_event(source, mock_kv, mock_writer):
    """Edited comment emits jira.comment_updated with before/after body."""
    old = _make_issue("PROJ-1", updated="2024-01-01T00:00:00.000+0000")
    new = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")

    old_comment = {
        "id": "10042",
        "author": {"displayName": "Jane"},
        "body": "Original text",
        "created": "2024-01-01T09:00:00.000+0000",
        "updated": "2024-01-01T09:00:00.000+0000"
    }
    edited_comment = {
        "id": "10042",
        "author": {"displayName": "Jane"},
        "body": "Edited text",
        "created": "2024-01-01T09:00:00.000+0000",
        "updated": "2024-01-02T10:00:00.000+0000"
    }

    mocks = _setup_existing_issue_test(source, mock_kv, old, new,
                                        comments=[edited_comment], cached_comments=[old_comment])

    with patch.object(source.client, 'get') as mock_get:
        mock_get.side_effect = mocks
        await source._handle_existing_issue(
            _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000"))

    events = mock_writer.write_events.call_args[0][1]
    edit_events = [e for e in events if e.event_type == "jira.comment_updated"]
    assert len(edit_events) == 1
    assert edit_events[0].data["body_before"] == "Original text"
    assert edit_events[0].data["body_after"] == "Edited text"
    assert edit_events[0].data["comment_id"] == "10042"


@pytest.mark.asyncio
async def test_mixed_events_single_poll(source, mock_kv, mock_writer):
    """A single poll cycle can emit multiple event types simultaneously."""
    old = _make_issue("PROJ-1", updated="2024-01-01T00:00:00.000+0000")
    old["fields"]["assignee"] = {"displayName": "Alice"}
    new = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")
    new["fields"]["status"] = {"name": "In Progress"}
    new["fields"]["assignee"] = {"displayName": "Bob"}
    new["fields"]["description"] = "Updated desc"

    new_comment = {
        "id": "10050",
        "author": {"displayName": "Charlie"},
        "body": "New comment",
        "created": "2024-01-02T08:00:00.000+0000",
        "updated": "2024-01-02T08:00:00.000+0000"
    }

    mocks = _setup_existing_issue_test(source, mock_kv, old, new,
                                        comments=[new_comment], cached_comments=[])

    with patch.object(source.client, 'get') as mock_get:
        mock_get.side_effect = mocks
        await source._handle_existing_issue(
            _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000"))

    events = mock_writer.write_events.call_args[0][1]
    event_types = {e.event_type for e in events}
    assert "jira.task_status_changed" in event_types
    assert "jira.task_reassigned" in event_types
    assert "jira.task_updated" in event_types  # description change
    assert "jira.comment_added" in event_types
    assert len(events) == 4


@pytest.mark.asyncio
async def test_status_change_excluded_from_task_updated_diff(source, mock_kv, mock_writer):
    """When status changes alongside other fields, status is NOT in the task_updated diff."""
    old = _make_issue("PROJ-1", updated="2024-01-01T00:00:00.000+0000")
    new = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")
    new["fields"]["status"] = {"name": "Done"}
    new["fields"]["description"] = "New desc"

    mocks = _setup_existing_issue_test(source, mock_kv, old, new)

    with patch.object(source.client, 'get') as mock_get:
        mock_get.side_effect = mocks
        await source._handle_existing_issue(
            _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000"))

    events = mock_writer.write_events.call_args[0][1]
    updated_events = [e for e in events if e.event_type == "jira.task_updated"]
    assert len(updated_events) == 1
    assert "Status" not in updated_events[0].data["diff"]
    assert "Description" in updated_events[0].data["diff"]


@pytest.mark.asyncio
async def test_only_comment_change_no_field_events(source, mock_kv, mock_writer):
    """When only a comment is added (no field changes), only comment_added is emitted."""
    old = _make_issue("PROJ-1", updated="2024-01-01T00:00:00.000+0000")
    new = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")

    new_comment = {
        "id": "10060",
        "author": {"displayName": "Dave"},
        "body": "Just a comment",
        "created": "2024-01-02T12:00:00.000+0000",
        "updated": "2024-01-02T12:00:00.000+0000"
    }

    mocks = _setup_existing_issue_test(source, mock_kv, old, new,
                                        comments=[new_comment], cached_comments=[])

    with patch.object(source.client, 'get') as mock_get:
        mock_get.side_effect = mocks
        await source._handle_existing_issue(
            _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000"))

    events = mock_writer.write_events.call_args[0][1]
    assert len(events) == 1
    assert events[0].event_type == "jira.comment_added"


@pytest.mark.asyncio
async def test_multiple_comments_added(source, mock_kv, mock_writer):
    """Multiple new comments each emit their own comment_added event."""
    old = _make_issue("PROJ-1", updated="2024-01-01T00:00:00.000+0000")
    new = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")

    comments = [
        {"id": "10070", "author": {"displayName": "A"}, "body": "First",
         "created": "2024-01-02T08:00:00.000+0000", "updated": "2024-01-02T08:00:00.000+0000"},
        {"id": "10071", "author": {"displayName": "B"}, "body": "Second",
         "created": "2024-01-02T09:00:00.000+0000", "updated": "2024-01-02T09:00:00.000+0000"},
    ]

    mocks = _setup_existing_issue_test(source, mock_kv, old, new,
                                        comments=comments, cached_comments=[])

    with patch.object(source.client, 'get') as mock_get:
        mock_get.side_effect = mocks
        await source._handle_existing_issue(
            _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000"))

    events = mock_writer.write_events.call_args[0][1]
    comment_events = [e for e in events if e.event_type == "jira.comment_added"]
    assert len(comment_events) == 2
    assert {e.data["comment_id"] for e in comment_events} == {"10070", "10071"}


@pytest.mark.asyncio
async def test_unchanged_comment_no_event(source, mock_kv, mock_writer):
    """A comment that hasn't changed should not emit any event."""
    old = _make_issue("PROJ-1", updated="2024-01-01T00:00:00.000+0000")
    new = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")
    new["fields"]["description"] = "Changed desc"

    existing_comment = {
        "id": "10080",
        "author": {"displayName": "Eve"},
        "body": "Same text",
        "created": "2024-01-01T08:00:00.000+0000",
        "updated": "2024-01-01T08:00:00.000+0000"
    }

    mocks = _setup_existing_issue_test(source, mock_kv, old, new,
                                        comments=[existing_comment], cached_comments=[existing_comment])

    with patch.object(source.client, 'get') as mock_get:
        mock_get.side_effect = mocks
        await source._handle_existing_issue(
            _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000"))

    events = mock_writer.write_events.call_args[0][1]
    assert all(e.event_type != "jira.comment_added" for e in events)
    assert all(e.event_type != "jira.comment_updated" for e in events)
    # Only the description change should be emitted
    assert len(events) == 1
    assert events[0].event_type == "jira.task_updated"


@pytest.mark.asyncio
async def test_no_events_when_only_ignored_fields_change(source, mock_kv, mock_writer):
    """If only ignored fields changed and no comment changes, no events are emitted."""
    old = _make_issue("PROJ-1", updated="2024-01-01T00:00:00.000+0000")
    # updated field is in ignored_fields by default, so a new 'updated' value
    # triggers the fetch but produces no diff
    new = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")

    mocks = _setup_existing_issue_test(source, mock_kv, old, new)

    with patch.object(source.client, 'get') as mock_get:
        mock_get.side_effect = mocks
        await source._handle_existing_issue(
            _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000"))

    mock_writer.write_events.assert_not_called()


@pytest.mark.asyncio
async def test_comment_cache_updated_after_processing(source, mock_kv, mock_writer):
    """After processing, both issue and comments caches are updated."""
    old = _make_issue("PROJ-1", updated="2024-01-01T00:00:00.000+0000")
    new = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")
    new["fields"]["description"] = "Changed"

    new_comment = {
        "id": "10090",
        "author": {"displayName": "Frank"},
        "body": "Hello",
        "created": "2024-01-02T08:00:00.000+0000",
        "updated": "2024-01-02T08:00:00.000+0000"
    }

    mocks = _setup_existing_issue_test(source, mock_kv, old, new,
                                        comments=[new_comment], cached_comments=[])

    with patch.object(source.client, 'get') as mock_get:
        mock_get.side_effect = mocks
        await source._handle_existing_issue(
            _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000"))

    mock_kv.set.assert_any_call(source.source_id, "issue:PROJ-1", new)
    mock_kv.set.assert_any_call(source.source_id, "comments:PROJ-1", [new_comment])


@pytest.mark.asyncio
async def test_comment_fetch_failure_no_false_diffs(source, mock_kv, mock_writer):
    """When comment fetch fails, cached comments are used and comment cache is NOT updated."""
    source.field_map = {"summary": "Summary", "status": "Status", "description": "Description"}

    old = _make_issue("PROJ-1", updated="2024-01-01T00:00:00.000+0000")
    new = _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000")
    new["fields"]["description"] = "Changed"

    existing_comment = {
        "id": "10099",
        "author": {"displayName": "Grace"},
        "body": "Existing comment",
        "created": "2024-01-01T08:00:00.000+0000",
        "updated": "2024-01-01T08:00:00.000+0000"
    }

    mock_kv.get.side_effect = lambda sid, key: (
        old if key == "issue:PROJ-1" else
        [existing_comment] if key == "comments:PROJ-1" else None
    )

    mock_detail = MagicMock()
    mock_detail.json.return_value = new
    mock_detail.raise_for_status = MagicMock()

    mock_changelog = MagicMock()
    mock_changelog.json.return_value = {"values": []}
    mock_changelog.raise_for_status = MagicMock()

    # Comment fetch fails
    mock_comments_resp = MagicMock()
    mock_comments_resp.raise_for_status.side_effect = Exception("Comment API down")

    with patch.object(source.client, 'get') as mock_get:
        mock_get.side_effect = [mock_detail, mock_changelog, mock_comments_resp]
        await source._handle_existing_issue(
            _make_issue("PROJ-1", updated="2024-01-02T00:00:00.000+0000"))

    # Should emit task_updated for description change but NO comment events
    events = mock_writer.write_events.call_args[0][1]
    assert all(e.event_type not in ("jira.comment_added", "jira.comment_updated") for e in events)
    assert any(e.event_type == "jira.task_updated" for e in events)

    # Comment cache should NOT be updated (only issue cache)
    set_calls = mock_kv.set.call_args_list
    comment_cache_calls = [c for c in set_calls if c[0][1] == "comments:PROJ-1"]
    assert len(comment_cache_calls) == 0
