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
    
    # New issue also needs full detail fetch
    full_issue = _make_issue("PROJ-1")
    full_issue["fields"]["description"] = "Full description"
    mock_detail_response = MagicMock()
    mock_detail_response.json.return_value = full_issue
    
    # Mock KV state: empty
    mock_kv.list_keys_with_prefix.return_value = []
    
    with patch.object(source.client, 'post', return_value=mock_search_response), \
         patch.object(source.client, 'get', return_value=mock_detail_response):
        await source.fetch_and_publish()
        
        # Verify KV set with FULL issue
        mock_kv.set.assert_called_with(source.source_id, "issue:PROJ-1", full_issue)
        
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
    mock_kv.get.return_value = old_issue
    
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
    
    with patch.object(source.client, 'post', return_value=mock_search_response), \
         patch.object(source.client, 'get') as mock_get:
        
        mock_get.side_effect = [mock_detail_response, mock_changelog_response]
        
        await source.fetch_and_publish()
        
        # Verify KV update
        mock_kv.set.assert_called_with(source.source_id, "issue:PROJ-1", full_issue)
        
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
        mock_kv.delete.assert_called_with(source.source_id, "issue:PROJ-1")
        
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
    mock_kv.get.return_value = _make_issue("PROJ-1", updated="2024-01-01")
    
    with patch.object(source.client, 'post', return_value=mock_search_response), \
         patch.object(source.client, 'get') as mock_get:
        
        # Mock responses
        res1 = MagicMock()
        res1.raise_for_status.side_effect = Exception("Detail fetch failed")
        
        res2 = MagicMock()
        res2.json.return_value = _make_issue("PROJ-2")
        res2.raise_for_status = MagicMock()

        # PROJ-1 fail, then PROJ-2 success for detail fetch
        mock_get.side_effect = [res1, res2]

        await source.fetch_and_publish()
        
        # PROJ-2 should still be handled as new issue
        mock_kv.set.assert_called_with(source.source_id, "issue:PROJ-2", _make_issue("PROJ-2"))
        
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
    mock_kv.get.return_value = old_issue
    
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
    
    with patch.object(source.client, 'post', return_value=mock_search_response), \
         patch.object(source.client, 'get') as mock_get:
        
        mock_get.side_effect = [mock_detail_response, mock_changelog_response]
        
        await source.fetch_and_publish()
        
        # Verify event emitted
        events = mock_writer.write_events.call_args[0][1]
        assert len(events) == 1
        
        changelog = events[0].data["changelog"]
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
    
    with patch.object(source.client, 'get', return_value=mock_detail):
        await source._handle_new_issue(issue)
    
    mock_writer.write_events.assert_called_once()
    events = mock_writer.write_events.call_args[0][1]
    assert events[0].data["status"] is None
    assert events[0].data["priority"] is None
