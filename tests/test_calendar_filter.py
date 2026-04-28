import pytest
from unittest.mock import MagicMock
from src.sources.google_calendar import GoogleCalendarSource
from src.config import GoogleCalendarSourceConfig, CalendarFilterItem

@pytest.fixture
def mock_services():
    services = MagicMock()
    services.writer = MagicMock()
    services.kv = MagicMock()
    return services

def test_calendar_source_filtering_logic(mock_services):
    config = GoogleCalendarSourceConfig(
        token_file="fake_token.json",
        filters=[
            {"ignore_summary": CalendarFilterItem(in_field="summary", contains="IGNORE ME")},
            {"regex_summary": CalendarFilterItem(in_field="summary", regex=r"^\[Test\].*")},
            {"ignore_description": CalendarFilterItem(in_field="description", contains="SECRET")},
            {"ignore_location": CalendarFilterItem(in_field="location", contains="Room 404")},
            {"ignore_organizer": CalendarFilterItem(in_field="organizer", contains="bot@example.com")},
            {"ignore_attendee": CalendarFilterItem(in_field="attendees", contains="spam@example.com")}
        ]
    )
    source = GoogleCalendarSource("test_calendar", config, mock_services, 1)

    # 1. Matches summary contains
    event1 = {"summary": "Please IGNORE ME now", "id": "1"}
    assert source._should_filter(event1) is True

    # 2. Matches summary regex
    event2 = {"summary": "[Test] Event", "id": "2"}
    assert source._should_filter(event2) is True

    # 3. Matches description contains
    event3 = {"summary": "Normal", "description": "This has a SECRET", "id": "3"}
    assert source._should_filter(event3) is True

    # 4. Matches location contains
    event4 = {"summary": "Normal", "location": "Meeting in Room 404", "id": "4"}
    assert source._should_filter(event4) is True

    # 5. Matches organizer email
    event5 = {
        "summary": "Normal",
        "organizer": {"email": "bot@example.com"},
        "id": "5"
    }
    assert source._should_filter(event5) is True

    # 6. Matches attendee email
    event6 = {
        "summary": "Normal",
        "attendees": [{"email": "user@example.com"}, {"email": "spam@example.com"}],
        "id": "6"
    }
    assert source._should_filter(event6) is True

    # 7. No match
    event7 = {
        "summary": "Keep this",
        "description": "Nothing special",
        "location": "Office",
        "organizer": {"email": "human@example.com"},
        "attendees": [{"email": "user@example.com"}],
        "id": "7"
    }
    assert source._should_filter(event7) is False
