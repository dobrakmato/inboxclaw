import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, timezone
from src.sources.google_calendar import GoogleCalendarSource, CalendarEventType
from src.config import GoogleCalendarSourceConfig

@pytest.fixture
def mock_services():
    services = MagicMock()
    services.kv = MagicMock()
    services.writer = MagicMock()
    # google_calendar uses services.writer.write_events (plural, synchronous)
    services.writer.write_events = MagicMock()
    return services

@pytest.fixture
def config():
    return GoogleCalendarSourceConfig(
        type="google_calendar",
        token_file="test_token.json",
        calendar_ids=["primary"],
        poll_interval="1m"
    )

def test_google_calendar_created(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    event_item = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": "2024-01-01T10:00:00Z"},
        "end": {"dateTime": "2024-01-01T11:00:00Z"},
        "status": "confirmed",
        "etag": "v1"
    }
    
    # No previous event in cache
    mock_services.kv.get.return_value = None
    
    events = source._classify_event_change("primary", event_item)
    
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == CalendarEventType.CREATED
    assert ev.entity_id == "evt1"
    assert ev.data["event_id"] == "evt1"
    assert ev.data["summary"] == "Meeting"
    assert "event" in ev.data
    assert ev.data["event"]["id"] == "evt1"

def test_google_calendar_updated(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    old_event = {
        "id": "evt1",
        "summary": "Old Title",
        "start": {"dateTime": "2024-01-01T10:00:00Z"},
        "status": "confirmed",
        "etag": "v1"
    }
    
    new_event = {
        "id": "evt1",
        "summary": "New Title",
        "start": {"dateTime": "2024-01-01T10:00:00Z"},
        "status": "confirmed",
        "etag": "v2"
    }
    
    # Mock cache to return old event
    mock_services.kv.get.side_effect = lambda sid, key: old_event if "snap:primary:evt1" in key else None
    
    events = source._classify_event_change("primary", new_event)
    
    # Should emit updated event (and maybe others if RSVP changed, but here only title)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == CalendarEventType.UPDATED
    assert ev.data["event_id"] == "evt1"
    assert ev.data["summary"] == "New Title"
    assert "changes" in ev.data
    assert ev.data["changes"]["summary"]["before"] == "Old Title"
    assert ev.data["changes"]["summary"]["after"] == "New Title"

def test_google_calendar_rsvp_changed(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    old_event = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": "2024-01-01T10:00:00Z"},
        "attendees": [
            {"email": "user1@example.com", "responseStatus": "needsAction"}
        ],
        "etag": "v1"
    }
    
    new_event = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": "2024-01-01T10:00:00Z"},
        "attendees": [
            {"email": "user1@example.com", "responseStatus": "accepted"}
        ],
        "etag": "v2"
    }
    
    mock_services.kv.get.side_effect = lambda sid, key: old_event if "snap:primary:evt1" in key else None
    
    events = source._classify_event_change("primary", new_event)
    
    # Should emit rsvp_changed event
    # Note: _has_non_rsvp_change should return False here because summary/start are same
    # and attendees responseStatus is ignored in normalization.
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == CalendarEventType.RSVP_CHANGED
    assert "rsvp_changes" in ev.data
    assert ev.data["rsvp_changes"][0]["attendee"] == "user1@example.com"
    assert ev.data["rsvp_changes"][0]["before"] == "needsAction"
    assert ev.data["rsvp_changes"][0]["after"] == "accepted"

def test_google_calendar_deleted(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    old_event = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": "2024-01-01T10:00:00Z"},
        "status": "confirmed",
        "etag": "v1"
    }
    
    cancelled_event = {
        "id": "evt1",
        "status": "cancelled",
        "etag": "v2"
    }
    
    mock_services.kv.get.side_effect = lambda sid, key: old_event if "snap:primary:evt1" in key else None
    
    events = source._classify_event_change("primary", cancelled_event)
    
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == CalendarEventType.DELETED
    assert ev.data["event_id"] == "evt1"
    assert "event" in ev.data
    assert "previous" in ev.data
    assert ev.data["previous"]["summary"] == "Meeting"

def test_google_calendar_recurrence_fields(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    event_item = {
        "id": "evt1_20240101",
        "recurringEventId": "master_evt1",
        "recurrence": ["RRULE:FREQ=WEEKLY"],
        "summary": "Weekly Meeting",
        "start": {"dateTime": "2024-01-01T10:00:00Z"},
        "status": "confirmed",
        "etag": "v1"
    }
    
    mock_services.kv.get.return_value = None
    
    events = source._classify_event_change("primary", event_item)
    
    assert len(events) == 1
    ev = events[0]
    assert ev.data["recurring_event_id"] == "master_evt1"
    assert ev.data["recurrence"] == ["RRULE:FREQ=WEEKLY"]

@pytest.mark.asyncio
async def test_google_calendar_collapse_recurring(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    # Mock _fetch_page to return two instances of the same recurring event
    source._fetch_page = MagicMock(return_value={
        "items": [
            {
                "id": "evt1_inst1",
                "recurringEventId": "master_evt1",
                "summary": "Weekly Meeting",
                "start": {"dateTime": "2024-01-01T10:00:00Z"},
                "status": "confirmed",
                "etag": "v1"
            },
            {
                "id": "evt1_inst2",
                "recurringEventId": "master_evt1",
                "summary": "Weekly Meeting",
                "start": {"dateTime": "2024-01-08T10:00:00Z"},
                "status": "confirmed",
                "etag": "v1"
            }
        ],
        "nextPageToken": None,
        "nextSyncToken": "sync_v2"
    })
    
    # Mock kv.get to handle both sync_token and config_max_into_future
    def kv_get_mock(sid, key):
        if "sync_token" in key:
            return "sync_v1"
        if "config_max_into_future" in key:
            # Match the default from config (365d = 31536000.0)
            return str(31536000.0)
        return None
    
    mock_services.kv.get.side_effect = kv_get_mock
    
    # Use config with collapse enabled (default)
    await source.fetch_and_publish_calendar(MagicMock(), "primary")
    
    # Should only call write_events once with ONE event (collapsed)
    assert mock_services.writer.write_events.called
    args, _ = mock_services.writer.write_events.call_args
    emitted = args[1]
    assert len(emitted) == 1
    assert emitted[0].entity_id == "evt1_inst1"

@pytest.mark.asyncio
async def test_google_calendar_no_collapse(mock_services, config):
    config.collapse_recurring_events = False
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    source._fetch_page = MagicMock(return_value={
        "items": [
            {
                "id": "evt1_inst1",
                "recurringEventId": "master_evt1",
                "summary": "Weekly Meeting",
                "start": {"dateTime": "2024-01-01T10:00:00Z"},
                "status": "confirmed",
                "etag": "v1"
            },
            {
                "id": "evt1_inst2",
                "recurringEventId": "master_evt1",
                "summary": "Weekly Meeting",
                "start": {"dateTime": "2024-01-08T10:00:00Z"},
                "status": "confirmed",
                "etag": "v1"
            }
        ],
        "nextPageToken": None,
        "nextSyncToken": "sync_v2"
    })
    
    # Mock kv.get to handle both sync_token and config_max_into_future
    def kv_get_mock(sid, key):
        if "sync_token" in key:
            return "sync_v1"
        if "config_max_into_future" in key:
            return str(31536000.0)
        return None
    
    mock_services.kv.get.side_effect = kv_get_mock
    
    await source.fetch_and_publish_calendar(MagicMock(), "primary")
    
    # Should call write_events with TWO events (not collapsed)
    assert mock_services.writer.write_events.called
    args, _ = mock_services.writer.write_events.call_args
    emitted = args[1]
    assert len(emitted) == 2
