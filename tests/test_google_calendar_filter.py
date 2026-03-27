import pytest
from unittest.mock import MagicMock
from src.sources.google_calendar import GoogleCalendarSource
from src.config import GoogleCalendarSourceConfig

@pytest.fixture
def mock_services():
    services = MagicMock()
    services.kv = MagicMock()
    services.writer = MagicMock()
    services.writer.write_events = MagicMock()
    return services

def test_google_calendar_filter_summary(mock_services):
    config = GoogleCalendarSourceConfig(
        type="google_calendar",
        token_file="test_token.json",
        filters=[
            {"ignore_focus_time": {"in": "summary", "contains": "Focus Time"}}
        ]
    )
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    event_item = {
        "id": "evt1",
        "summary": "Focus Time",
        "start": {"dateTime": "2024-01-01T10:00:00Z"},
        "status": "confirmed",
        "etag": "v1"
    }
    
    mock_services.kv.get.return_value = None
    
    events = source._classify_event_change("primary", event_item)
    
    # Should be filtered out
    assert len(events) == 0

def test_google_calendar_filter_attendees(mock_services):
    config = GoogleCalendarSourceConfig(
        type="google_calendar",
        token_file="test_token.json",
        filters=[
            {"ignore_external": {"in": "attendees", "regex": ".*@external\\.com"}}
        ]
    )
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    event_item = {
        "id": "evt1",
        "summary": "External Meeting",
        "attendees": [
            {"email": "me@company.com"},
            {"email": "someone@external.com"}
        ],
        "start": {"dateTime": "2024-01-01T10:00:00Z"},
        "status": "confirmed",
        "etag": "v1"
    }
    
    mock_services.kv.get.return_value = None
    
    events = source._classify_event_change("primary", event_item)
    
    # Should be filtered out
    assert len(events) == 0

def test_google_calendar_filter_organizer(mock_services):
    config = GoogleCalendarSourceConfig(
        type="google_calendar",
        token_file="test_token.json",
        filters=[
            {"ignore_boss": {"in": "organizer", "contains": "boss@company.com"}}
        ]
    )
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    event_item = {
        "id": "evt1",
        "summary": "Meeting with boss",
        "organizer": {"email": "boss@company.com"},
        "start": {"dateTime": "2024-01-01T10:00:00Z"},
        "status": "confirmed",
        "etag": "v1"
    }
    
    mock_services.kv.get.return_value = None
    
    events = source._classify_event_change("primary", event_item)
    
    # Should be filtered out
    assert len(events) == 0
