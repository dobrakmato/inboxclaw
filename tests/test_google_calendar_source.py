import asyncio

import pytest
from unittest.mock import MagicMock, patch, AsyncMock, call
from datetime import datetime, timezone, timedelta
from googleapiclient.errors import HttpError
from src.sources.google_calendar import GoogleCalendarSource, CalendarEventType
from src.config import GoogleCalendarSourceConfig


def _kv_get_from(mapping):
    return lambda _sid, key: mapping.get(key)


def _calendar_fingerprint() -> str:
    return '{"max_into_future": 31536000.0}'


def _snapshot_state(count: int) -> dict:
    return {"complete": True, "count": count}


def _future_iso(days: int, hours: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days, hours=hours)).isoformat()


def _past_iso(days: int = 0, hours: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=hours)).isoformat()


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
        poll_interval="1m",
        max_event_age_days=None  # Disable age filtering for tests by default
    )

def test_google_calendar_created(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    event_item = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1"
    }
    
    # No previous event in cache
    mock_services.kv.get.return_value = None
    
    events = source._classify_event_change("primary", event_item)
    
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == CalendarEventType.CREATED
    assert ev.entity_id == "primary:evt1"
    assert ev.data["event_id"] == "evt1"
    assert ev.data["calendar_id"] == "primary"
    assert ev.data["summary"] == "Meeting"
    assert "event" in ev.data
    assert ev.data["event"]["id"] == "evt1"


def test_google_calendar_payload_omits_provider_metadata(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    event_item = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1",
        "sequence": 3,
        "iCalUID": "evt1@example.com",
        "reminders": {"useDefault": True},
    }

    mock_services.kv.get.return_value = None

    events = source._classify_event_change("primary", event_item)

    assert len(events) == 1
    emitted_event = events[0].data["event"]
    assert emitted_event["id"] == "evt1"
    for field in ("etag", "sequence", "iCalUID", "reminders"):
        assert field not in emitted_event
    assert event_item["etag"] == "v1"


def test_google_calendar_created_compacts_datetime_payload_keys(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    start_time = _future_iso(7)
    end_time = _future_iso(7, 1)

    event_item = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": start_time, "timeZone": "Europe/Prague"},
        "end": {"dateTime": end_time, "timeZone": "Europe/Prague"},
        "originalStartTime": {"dateTime": start_time, "timeZone": "Europe/Prague"},
        "status": "confirmed",
        "etag": "v1",
    }

    mock_services.kv.get.return_value = None

    events = source._classify_event_change("primary", event_item)

    assert len(events) == 1
    data = events[0].data
    assert data["start"] == {"dt": start_time, "tz": "Europe/Prague"}
    assert data["event"]["start"] == {"dt": start_time, "tz": "Europe/Prague"}
    assert data["event"]["end"] == {"dt": end_time, "tz": "Europe/Prague"}
    assert data["event"]["originalStartTime"] == {"dt": start_time, "tz": "Europe/Prague"}
    assert event_item["start"] == {"dateTime": start_time, "timeZone": "Europe/Prague"}


def test_google_calendar_created_summarizes_large_attendee_lists(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    event_item = {
        "id": "evt1",
        "summary": "All hands",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1",
        "attendees": [
            {"email": "accepted1@example.com", "responseStatus": "accepted"},
            {"email": "accepted2@example.com", "responseStatus": "accepted"},
            {"email": "tentative@example.com", "responseStatus": "tentative"},
            {"email": "unknown@example.com"},
        ],
    }
    mock_services.kv.get.return_value = None

    events = source._classify_event_change("primary", event_item)

    assert len(events) == 1
    assert events[0].data["event"]["attendees"] == {
        "total": 4,
        "by_state": {
            "accepted": 2,
            "tentative": 1,
            "unknown": 1,
        },
    }
    assert "accepted1@example.com" not in str(events[0].data)


def test_google_calendar_created_keeps_attendees_at_default_limit(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    attendees = [
        {"email": "one@example.com", "responseStatus": "accepted"},
        {"email": "two@example.com", "responseStatus": "declined"},
        {"email": "three@example.com", "responseStatus": "needsAction"},
    ]
    event_item = {
        "id": "evt1",
        "summary": "Small meeting",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1",
        "attendees": attendees,
    }
    mock_services.kv.get.return_value = None

    events = source._classify_event_change("primary", event_item)

    assert events[0].data["event"]["attendees"] == attendees


def test_google_calendar_attendee_detail_limit_is_configurable(mock_services, config):
    config.attendee_detail_limit = 4
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    attendees = [
        {"email": "one@example.com", "responseStatus": "accepted"},
        {"email": "two@example.com", "responseStatus": "declined"},
        {"email": "three@example.com", "responseStatus": "needsAction"},
        {"email": "four@example.com", "responseStatus": "tentative"},
    ]
    event_item = {
        "id": "evt1",
        "summary": "Medium meeting",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1",
        "attendees": attendees,
    }
    mock_services.kv.get.return_value = None

    events = source._classify_event_change("primary", event_item)

    assert events[0].data["event"]["attendees"] == attendees


def test_google_calendar_updated(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    old_event = {
        "id": "evt1",
        "summary": "Old Title",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1"
    }
    
    new_event = {
        "id": "evt1",
        "summary": "New Title",
        "start": {"dateTime": old_event["start"]["dateTime"]},
        "end": {"dateTime": old_event["end"]["dateTime"]},
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


def test_google_calendar_updated_compacts_datetime_payload_keys_in_changes(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    old_start = _future_iso(7)
    new_start = _future_iso(7, 2)
    end_time = _future_iso(7, 3)

    old_event = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": old_start, "timeZone": "Europe/Prague"},
        "end": {"dateTime": end_time, "timeZone": "Europe/Prague"},
        "status": "confirmed",
        "etag": "v1",
    }
    new_event = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": new_start, "timeZone": "Europe/Prague"},
        "end": {"dateTime": end_time, "timeZone": "Europe/Prague"},
        "status": "confirmed",
        "etag": "v2",
    }

    mock_services.kv.get.return_value = old_event

    events = source._classify_event_change("primary", new_event)

    assert len(events) == 1
    data = events[0].data
    assert data["start"] == {"dt": new_start, "tz": "Europe/Prague"}
    assert data["changes"]["start"] == {
        "before": {"dt": old_start, "tz": "Europe/Prague"},
        "after": {"dt": new_start, "tz": "Europe/Prague"},
    }


def test_google_calendar_update_summarizes_attendee_changes_above_limit(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    old_event = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1",
        "attendees": [
            {"email": "one@example.com", "responseStatus": "accepted"},
            {"email": "two@example.com", "responseStatus": "accepted"},
            {"email": "three@example.com", "responseStatus": "declined"},
        ],
    }
    new_event = {
        **old_event,
        "etag": "v2",
        "attendees": [
            *old_event["attendees"],
            {"email": "five@example.com", "responseStatus": "accepted"},
        ],
    }
    mock_services.kv.get.return_value = old_event

    events = source._classify_event_change("primary", new_event)

    updated_events = [event for event in events if event.event_type == CalendarEventType.UPDATED]
    assert len(updated_events) == 1
    assert updated_events[0].data["changes"]["attendees"] == {
        "before": {
            "total": 3,
            "by_state": {
                "accepted": 2,
                "declined": 1,
            },
        },
        "after": {
            "total": 4,
            "by_state": {
                "accepted": 3,
                "declined": 1,
            },
        },
    }
    assert "one@example.com" not in str(updated_events[0].data)
    assert "five@example.com" not in str(updated_events[0].data)

def test_google_calendar_rsvp_changed(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    old_event = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "attendees": [
            {"email": "user1@example.com", "responseStatus": "needsAction"}
        ],
        "etag": "v1"
    }
    
    new_event = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": old_event["start"]["dateTime"]},
        "end": {"dateTime": old_event["end"]["dateTime"]},
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


def test_google_calendar_rsvp_changed_summarizes_large_events(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    old_event = {
        "id": "evt1",
        "summary": "All hands",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "attendees": [
            {"email": "one@example.com", "responseStatus": "needsAction"},
            {"email": "two@example.com", "responseStatus": "accepted"},
            {"email": "three@example.com", "responseStatus": "declined"},
            {"email": "four@example.com", "responseStatus": "tentative"},
        ],
        "etag": "v1",
    }
    new_event = {
        **old_event,
        "etag": "v2",
        "attendees": [
            {"email": "one@example.com", "responseStatus": "accepted"},
            {"email": "two@example.com", "responseStatus": "accepted"},
            {"email": "three@example.com", "responseStatus": "declined"},
            {"email": "four@example.com", "responseStatus": "tentative"},
        ],
    }
    mock_services.kv.get.return_value = old_event

    events = source._classify_event_change("primary", new_event)

    assert len(events) == 1
    assert events[0].event_type == CalendarEventType.RSVP_CHANGED
    assert events[0].data["rsvp_changes"] == {
        "changed": 1,
        "before": {
            "total": 4,
            "by_state": {
                "accepted": 1,
                "declined": 1,
                "needsAction": 1,
                "tentative": 1,
            },
        },
        "after": {
            "total": 4,
            "by_state": {
                "accepted": 2,
                "declined": 1,
                "tentative": 1,
            },
        },
    }
    assert "one@example.com" not in str(events[0].data)

def test_google_calendar_deleted(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    old_event = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
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
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1"
    }
    
    mock_services.kv.get.return_value = None
    
    events = source._classify_event_change("primary", event_item)
    
    assert len(events) == 1
    ev = events[0]
    assert ev.data["recurring_event_id"] == "master_evt1"
    assert ev.data["recurrence"] == ["RRULE:FREQ=WEEKLY"]


def test_google_calendar_recurring_master_past_first_occurrence_still_updates(mock_services, config):
    config.max_event_age_days = 2.0
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    past_start = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    past_end = (datetime.now(timezone.utc) - timedelta(days=30) + timedelta(hours=1)).isoformat()
    old_event = {
        "id": "series_evt",
        "summary": "Weekly Meeting",
        "start": {"dateTime": past_start},
        "end": {"dateTime": past_end},
        "recurrence": ["RRULE:FREQ=WEEKLY"],
        "status": "confirmed",
        "etag": "v1",
    }
    new_event = {
        "id": "series_evt",
        "summary": "Renamed Weekly Meeting",
        "start": old_event["start"],
        "end": old_event["end"],
        "recurrence": old_event["recurrence"],
        "status": "confirmed",
        "etag": "v2",
    }
    mock_services.kv.get.return_value = old_event

    events = source._classify_event_change("primary", new_event)

    assert len(events) == 1
    assert events[0].event_type == CalendarEventType.UPDATED


def test_google_calendar_recurring_master_deletion_after_past_first_occurrence(mock_services, config):
    config.max_event_age_days = 2.0
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    past_start = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    past_end = (datetime.now(timezone.utc) - timedelta(days=30) + timedelta(hours=1)).isoformat()
    old_event = {
        "id": "series_evt",
        "summary": "Weekly Meeting",
        "start": {"dateTime": past_start},
        "end": {"dateTime": past_end},
        "recurrence": ["RRULE:FREQ=WEEKLY"],
        "status": "confirmed",
        "etag": "v1",
    }
    cancelled_event = {
        "id": "series_evt",
        "status": "cancelled",
        "etag": "v2",
    }
    mock_services.kv.get.return_value = old_event

    events = source._classify_event_change("primary", cancelled_event)

    assert len(events) == 1
    assert events[0].event_type == CalendarEventType.DELETED


def test_google_calendar_ended_finite_recurring_master_not_emitted(mock_services, config):
    config.max_event_age_days = 2.0
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    past_start = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    past_end = (datetime.now(timezone.utc) - timedelta(days=30) + timedelta(hours=1)).isoformat()
    old_event = {
        "id": "ended_series",
        "summary": "Ended series",
        "start": {"dateTime": past_start},
        "end": {"dateTime": past_end},
        "recurrence": ["RRULE:FREQ=DAILY;COUNT=1"],
        "status": "confirmed",
        "etag": "v1",
    }
    new_event = {
        **old_event,
        "summary": "Renamed ended series",
        "etag": "v2",
    }

    result = source._classify_event_change_result(
        "primary",
        new_event,
        previous_event=old_event,
    )

    assert result.events == []
    assert len(result.cache_mutations) == 1
    assert result.cache_mutations[0].event_payload is None


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
                "start": {"dateTime": _future_iso(7)},
                "end": {"dateTime": _future_iso(7, 1)},
                "status": "confirmed",
                "etag": "v1"
            },
            {
                "id": "evt1_inst2",
                "recurringEventId": "master_evt1",
                "summary": "Weekly Meeting",
                "start": {"dateTime": _future_iso(14)},
                "end": {"dateTime": _future_iso(14, 1)},
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
        if key == "config_fingerprint:primary":
            return _calendar_fingerprint()
        if key == "snapshot_state:primary":
            return _snapshot_state(0)
        if "config_max_into_future" in key:
            # Match the default from config (365d = 31536000.0)
            return 31536000.0
        return None
    
    mock_services.kv.get.side_effect = kv_get_mock
    mock_services.kv.list_keys_with_prefix.return_value = []
    
    # Use config with collapse enabled (default)
    await source.fetch_and_publish_calendar(MagicMock(), "primary")
    
    # Distinct recurring instances must both be preserved.
    assert mock_services.writer.write_events.called
    args, _ = mock_services.writer.write_events.call_args
    emitted = args[1]
    assert len(emitted) == 2
    assert emitted[0].entity_id == "primary:evt1_inst1"
    assert emitted[1].entity_id == "primary:evt1_inst2"

@pytest.mark.asyncio
async def test_google_calendar_410_recovery_stores_config_as_float(mock_services, config):
    """
    After a 410 (sync token expired) recovery, the config_max_into_future
    must be stored as a float, not a string. Storing it as a string would
    cause config_changed to be True on the next poll, triggering an
    unnecessary baseline rebuild every cycle.
    """
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    # Mock _fetch_page: first call raises 410, rebuild succeeds
    mock_resp = MagicMock()
    mock_resp.status = 410
    http_error = HttpError(resp=mock_resp, content=b"sync token expired")

    call_count = {"n": 0}
    def fetch_page_side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise http_error
        # Rebuild baseline pages
        return {
            "items": [],
            "nextSyncToken": "new_sync_token"
        }

    source._fetch_page = MagicMock(side_effect=fetch_page_side_effect)

    # KV: has a sync token but no config stored yet
    def kv_get_mock(sid, key):
        if "sync_token" in key:
            return "old_sync_token"
        if key == "config_fingerprint:primary":
            return _calendar_fingerprint()
        if key == "snapshot_state:primary":
            return _snapshot_state(0)
        if "config_max_into_future" in key:
            return 31536000.0
        return None

    mock_services.kv.get.side_effect = kv_get_mock
    mock_services.kv.list_keys_with_prefix.return_value = []

    await source.fetch_and_publish_calendar(MagicMock(), "primary")

    # Verify that config was stored as float, not string
    set_calls = mock_services.kv.set.call_args_list
    config_set_calls = [c for c in set_calls if "config_max_into_future" in str(c)]
    assert len(config_set_calls) > 0, "config_max_into_future should have been saved"
    # The value argument (3rd positional) must be a float, not a string
    for call in config_set_calls:
        stored_value = call[0][2]  # positional args: (source_id, key, value)
        assert isinstance(stored_value, float), (
            f"config_max_into_future should be stored as float, got {type(stored_value).__name__}: {stored_value!r}"
        )


def test_google_calendar_past_event_age_filter(mock_services, config):
    """
    Verify that updates to events that ended long ago are ignored,
    even if they were recently modified.
    """
    # Enable age filtering for this test
    config.max_event_age_days = 1.0
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    now = datetime.now(timezone.utc)
    
    # Event that happened 20 days ago
    start_time = (now - timedelta(days=20)).isoformat()
    end_time = (now - timedelta(days=20, hours=-1)).isoformat()
    # But it was JUST updated (now)
    updated_time = now.isoformat()
    
    old_event = {
        "id": "past_evt",
        "summary": "Old Past Meeting",
        "start": {"dateTime": start_time},
        "end": {"dateTime": end_time},
        "status": "confirmed",
        "updated": (now - timedelta(minutes=5)).isoformat(),
        "etag": "v1"
    }
    
    new_event = {
        "id": "past_evt",
        "summary": "New Past Meeting Title",
        "start": {"dateTime": start_time},
        "end": {"dateTime": end_time},
        "status": "confirmed",
        "updated": updated_time,
        "etag": "v2"
    }
    
    # Mock cache to return old event
    mock_services.kv.get.side_effect = lambda sid, key: old_event if "snap:primary:past_evt" in key else None
    
    events = source._classify_event_change("primary", new_event)
    
    # It should NOT be emitted because the event itself ended 20 days ago.
    assert len(events) == 0

def test_google_calendar_recent_event_age_filter(mock_services, config):
    """
    Ensure that updates to RECENT events are still emitted even when age filtering is active.
    """
    config.max_event_age_days = 1.0
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    
    now = datetime.now(timezone.utc)
    
    # Event that is happening now
    start_time = now.isoformat()
    end_time = (now + timedelta(hours=1)).isoformat()
    updated_time = now.isoformat()
    
    old_event = {
        "id": "recent_evt",
        "summary": "Old Title",
        "start": {"dateTime": start_time},
        "end": {"dateTime": end_time},
        "status": "confirmed",
        "updated": (now - timedelta(minutes=5)).isoformat(),
        "etag": "v1"
    }
    
    new_event = {
        "id": "recent_evt",
        "summary": "New Title",
        "start": {"dateTime": start_time},
        "end": {"dateTime": end_time},
        "status": "confirmed",
        "updated": updated_time,
        "etag": "v2"
    }
    
    mock_services.kv.get.side_effect = lambda sid, key: old_event if "snap:primary:recent_evt" in key else None
    
    events = source._classify_event_change("primary", new_event)

    assert len(events) == 1


def test_google_calendar_recently_ended_update_not_emitted(mock_services, config):
    config.max_event_age_days = 2.0
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    ended_at = _past_iso(hours=1)
    old_event = {
        "id": "recently_ended",
        "summary": "Old title",
        "start": {"dateTime": _past_iso(hours=2)},
        "end": {"dateTime": ended_at},
        "status": "confirmed",
        "etag": "v1",
    }
    new_event = {
        "id": "recently_ended",
        "summary": "New title",
        "start": old_event["start"],
        "end": old_event["end"],
        "status": "confirmed",
        "etag": "v2",
    }
    mock_services.kv.get.return_value = old_event

    result = source._classify_event_change_result("primary", new_event)

    assert result.events == []
    assert len(result.cache_mutations) == 1
    assert result.cache_mutations[0].event_payload == new_event


def test_google_calendar_rescheduled_past_event_into_future_is_emitted(mock_services, config):
    config.max_event_age_days = 2.0
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    old_event = {
        "id": "rescheduled",
        "summary": "Rescheduled meeting",
        "start": {"dateTime": _past_iso(hours=2)},
        "end": {"dateTime": _past_iso(hours=1)},
        "status": "confirmed",
        "etag": "v1",
    }
    new_event = {
        "id": "rescheduled",
        "summary": "Rescheduled meeting",
        "start": {"dateTime": _future_iso(3)},
        "end": {"dateTime": _future_iso(3, 1)},
        "status": "confirmed",
        "etag": "v2",
    }
    mock_services.kv.get.return_value = old_event

    events = source._classify_event_change("primary", new_event)

    assert len(events) == 1
    assert events[0].event_type == CalendarEventType.UPDATED


def test_google_calendar_future_event_rescheduled_into_past_emits_deleted(mock_services, config):
    config.max_event_age_days = 2.0
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    past_start = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    past_end = (datetime.now(timezone.utc) - timedelta(days=10) + timedelta(hours=1)).isoformat()

    old_event = {
        "id": "moved_to_past",
        "summary": "Moved to past",
        "start": {"dateTime": _future_iso(3)},
        "end": {"dateTime": _future_iso(3, 1)},
        "status": "confirmed",
        "etag": "v1",
    }
    new_event = {
        "id": "moved_to_past",
        "summary": "Moved to past",
        "start": {"dateTime": past_start},
        "end": {"dateTime": past_end},
        "status": "confirmed",
        "etag": "v2",
    }
    mock_services.kv.get.return_value = old_event

    result = source._classify_event_change_result("primary", new_event)

    assert len(result.events) == 1
    assert result.events[0].event_type == CalendarEventType.DELETED
    assert len(result.cache_mutations) == 1
    assert result.cache_mutations[0].event_payload is None


def test_google_calendar_rescheduled_after_suppressed_past_update_is_updated(mock_services, config):
    config.max_event_age_days = 2.0
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    old_event = {
        "id": "rescheduled_after_past_update",
        "summary": "Original title",
        "start": {"dateTime": _past_iso(hours=2)},
        "end": {"dateTime": _past_iso(hours=1)},
        "status": "confirmed",
        "etag": "v1",
    }
    suppressed_past_update = {
        "id": "rescheduled_after_past_update",
        "summary": "Suppressed title",
        "start": old_event["start"],
        "end": old_event["end"],
        "status": "confirmed",
        "etag": "v2",
    }
    suppressed_result = source._classify_event_change_result(
        "primary",
        suppressed_past_update,
        previous_event=old_event,
    )
    cached_after_suppressed = suppressed_result.cache_mutations[0].event_payload

    future_update = {
        "id": "rescheduled_after_past_update",
        "summary": "Suppressed title",
        "start": {"dateTime": _future_iso(3)},
        "end": {"dateTime": _future_iso(3, 1)},
        "status": "confirmed",
        "etag": "v3",
    }
    rescheduled_result = source._classify_event_change_result(
        "primary",
        future_update,
        previous_event=cached_after_suppressed,
    )

    assert suppressed_result.events == []
    assert len(rescheduled_result.events) == 1
    assert rescheduled_result.events[0].event_type == CalendarEventType.UPDATED


def test_google_calendar_recently_ended_cancelled_event_not_emitted(mock_services, config):
    config.max_event_age_days = 2.0
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    old_event = {
        "id": "recently_ended_cancelled",
        "summary": "Recently ended meeting",
        "start": {"dateTime": _past_iso(hours=2)},
        "end": {"dateTime": _past_iso(hours=1)},
        "status": "confirmed",
        "etag": "v1",
    }
    cancelled_event = {
        "id": "recently_ended_cancelled",
        "status": "cancelled",
        "etag": "v2",
    }
    mock_services.kv.get.return_value = old_event

    result = source._classify_event_change_result("primary", cancelled_event)

    assert result.events == []
    assert len(result.cache_mutations) == 1
    assert result.cache_mutations[0].event_payload is None


def test_google_calendar_future_event_not_filtered_by_old_updated_timestamp(mock_services, config):
    config.max_event_age_days = 1.0
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    now = datetime.now(timezone.utc)
    future_start = (now + timedelta(days=10)).isoformat()
    future_end = (now + timedelta(days=10, hours=1)).isoformat()
    old_metadata = (now - timedelta(days=7)).isoformat()

    future_event = {
        "id": "future_evt",
        "summary": "Future planning session",
        "start": {"dateTime": future_start},
        "end": {"dateTime": future_end},
        "status": "confirmed",
        "created": old_metadata,
        "updated": old_metadata,
        "etag": "v1",
    }

    mock_services.kv.get.return_value = None

    events = source._classify_event_change("primary", future_event)

    assert len(events) == 1
    assert events[0].event_type == CalendarEventType.CREATED


def test_google_calendar_fetch_page_always_requests_deleted_entries(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    service = MagicMock()
    execute = service.events.return_value.list.return_value.execute
    execute.return_value = {"items": [], "nextSyncToken": "sync-2"}

    source._fetch_page(service, "primary", sync_token="sync-1")

    _, kwargs = service.events.return_value.list.call_args
    assert kwargs["syncToken"] == "sync-1"
    assert kwargs["showDeleted"] is True
    assert kwargs["singleEvents"] is True
    assert "timeMin" not in kwargs
    assert "timeMax" not in kwargs


@pytest.mark.asyncio
async def test_google_calendar_old_single_events_fingerprint_resets_sync_token(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    source._rebuild_sync_baseline = MagicMock(return_value=True)

    mock_services.kv.get.side_effect = _kv_get_from(
        {
            "config_fingerprint:primary": '{"single_events": false, "max_into_future": 31536000.0}',
            "sync_token:primary": "sync-1",
        }
    )

    await source.fetch_and_publish_calendar(MagicMock(), "primary")

    mock_services.kv.delete.assert_any_call(1, "sync_token:primary")
    mock_services.kv.delete.assert_any_call(1, "config_fingerprint:primary")
    source._rebuild_sync_baseline.assert_called_once()


@pytest.mark.asyncio
async def test_google_calendar_legacy_max_future_key_resets_sync_token(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    source._rebuild_sync_baseline = MagicMock(return_value=True)

    mock_services.kv.get.side_effect = _kv_get_from(
        {
            "config_max_into_future:primary": 31536000.0,
            "sync_token:primary": "sync-1",
        }
    )

    await source.fetch_and_publish_calendar(MagicMock(), "primary")

    mock_services.kv.delete.assert_any_call(1, "sync_token:primary")
    mock_services.kv.delete.assert_any_call(1, "config_max_into_future:primary")
    source._rebuild_sync_baseline.assert_called_once()


@pytest.mark.asyncio
async def test_google_calendar_max_future_shrink_prunes_without_deleted_cascade(mock_services, config):
    config.max_into_future = "14d"
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    source._rebuild_sync_baseline = MagicMock(return_value=True)

    far_event = {
        "id": "far",
        "summary": "Far future event",
        "start": {"dateTime": _future_iso(30)},
        "end": {"dateTime": _future_iso(30, 1)},
        "status": "confirmed",
        "etag": "v1",
    }
    near_event = {
        "id": "near",
        "summary": "Near future event",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1",
    }
    mock_services.kv.get.side_effect = _kv_get_from(
        {
            "config_fingerprint:primary": '{"max_into_future": 946080000.0}',
            "sync_token:primary": "sync-1",
            "snap:primary:far": far_event,
            "snap:primary:near": near_event,
        }
    )
    mock_services.kv.list_keys_with_prefix.return_value = [
        "snap:primary:far",
        "snap:primary:near",
    ]

    await source.fetch_and_publish_calendar(MagicMock(), "primary")

    mock_services.writer.write_events.assert_not_called()
    delete_calls = mock_services.kv.delete.call_args_list
    assert call(1, "snap:primary:far") in delete_calls
    assert call(1, "snap:primary:near") not in delete_calls
    assert call(1, "sync_token:primary") in delete_calls
    source._rebuild_sync_baseline.assert_called_once()


def test_google_calendar_cancelled_recurring_instance_keeps_tombstone(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    instance_start = _future_iso(7)
    old_event = {
        "id": "evt1_20260101",
        "summary": "Standup",
        "status": "confirmed",
        "etag": "v1",
        "recurringEventId": "evt1",
        "originalStartTime": {"dateTime": instance_start},
        "start": {"dateTime": instance_start},
        "end": {"dateTime": _future_iso(7, 1)},
    }
    cancelled_instance = {
        "id": "evt1_20260101",
        "status": "cancelled",
        "etag": "v2",
        "recurringEventId": "evt1",
        "originalStartTime": {"dateTime": instance_start},
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    mock_services.kv.get.side_effect = lambda sid, key: old_event if key == "snap:primary:evt1_20260101" else None

    result = source._classify_event_change_result("primary", cancelled_instance)
    events = result.events

    assert len(events) == 1
    assert events[0].event_type == CalendarEventType.DELETED
    assert len(result.cache_mutations) == 1
    mutation = result.cache_mutations[0]
    assert mutation.calendar_id == "primary"
    assert mutation.event_id == "evt1_20260101"
    assert mutation.event_payload == cancelled_instance
    mock_services.kv.set.assert_not_called()
    mock_services.kv.delete.assert_not_called()


def test_google_calendar_cached_cancelled_recurring_tombstone_not_reemitted(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    instance_start = _future_iso(7)
    cancelled_instance = {
        "id": "evt1_20260101",
        "status": "cancelled",
        "etag": "v2",
        "recurringEventId": "evt1",
        "originalStartTime": {"dateTime": instance_start},
        "updated": datetime.now(timezone.utc).isoformat(),
    }

    result = source._classify_event_change_result(
        "primary",
        cancelled_instance,
        previous_event=cancelled_instance,
    )

    assert result.events == []
    assert len(result.cache_mutations) == 1
    assert result.cache_mutations[0].event_payload == cancelled_instance


def test_google_calendar_entity_ids_are_calendar_scoped(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    event_item = {
        "id": "shared_evt",
        "summary": "Calendar-scoped identity",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1",
    }

    mock_services.kv.get.return_value = None

    events = source._classify_event_change("team@example.com", event_item)

    assert len(events) == 1
    event = events[0]
    assert event.entity_id == "team@example.com:shared_evt"
    assert event.event_id.startswith("gcal:team@example.com:shared_evt:")
    assert event.data["calendar_id"] == "team@example.com"


@pytest.mark.asyncio
async def test_google_calendar_cleanup_loop_does_not_delete_snapshots(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    with patch("src.sources.google_calendar.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
        with pytest.raises(asyncio.CancelledError):
            await source._cleanup_loop()

    mock_services.kv.delete_expired_with_prefix.assert_not_called()


@pytest.mark.asyncio
async def test_google_calendar_410_recovery_reconciles_stale_snapshots(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    mock_resp = MagicMock()
    mock_resp.status = 410
    http_error = HttpError(resp=mock_resp, content=b"sync token expired")
    call_count = {"n": 0}

    def fetch_page_side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise http_error
        return {"items": [], "nextSyncToken": "new-sync"}

    source._fetch_page = MagicMock(side_effect=fetch_page_side_effect)
    mock_services.kv.get.side_effect = _kv_get_from(
        {
            "sync_token:primary": "expired-sync",
            "config_fingerprint:primary": _calendar_fingerprint(),
            "snap:primary:stale-1": {
                "id": "stale-1",
                "summary": "Removed event",
                "status": "confirmed",
                "etag": "v1",
            },
            "snap:primary:stale-2": {
                "id": "stale-2",
                "summary": "Removed event 2",
                "status": "confirmed",
                "etag": "v1",
            },
        }
    )
    mock_services.kv.list_keys_with_prefix.return_value = [
        "snap:primary:stale-1",
        "snap:primary:stale-2",
    ]

    await source.fetch_and_publish_calendar(MagicMock(), "primary")

    mock_services.kv.list_keys_with_prefix.assert_any_call(1, "snap:primary:")
    mock_services.kv.delete.assert_any_call(1, "snap:primary:stale-1")
    mock_services.kv.delete.assert_any_call(1, "snap:primary:stale-2")
    mock_services.kv.set.assert_any_call(1, "sync_token:primary", "new-sync")
    mock_services.writer.write_events.assert_called_once()
    _, events = mock_services.writer.write_events.call_args.args
    assert len(events) == 2
    assert all(event.event_type == CalendarEventType.DELETED for event in events)


@pytest.mark.asyncio
async def test_google_calendar_410_recovery_skips_past_events(mock_services, config):
    """Bug 3: Past events missing from fresh listing should not emit DELETED."""
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    mock_resp = MagicMock()
    mock_resp.status = 410
    http_error = HttpError(resp=mock_resp, content=b"sync token expired")
    call_count = {"n": 0}

    def fetch_page_side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise http_error
        return {"items": [], "nextSyncToken": "new-sync"}

    source._fetch_page = MagicMock(side_effect=fetch_page_side_effect)

    past_end = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    mock_services.kv.get.side_effect = _kv_get_from(
        {
            "sync_token:primary": "expired-sync",
            "config_fingerprint:primary": _calendar_fingerprint(),
            "snap:primary:past-1": {
                "id": "past-1",
                "summary": "Old meeting",
                "status": "confirmed",
                "end": {"dateTime": past_end},
                "etag": "v1",
            },
        }
    )
    mock_services.kv.list_keys_with_prefix.return_value = [
        "snap:primary:past-1",
    ]

    await source.fetch_and_publish_calendar(MagicMock(), "primary")

    # No DELETED should be emitted for a past event
    mock_services.writer.write_events.assert_not_called()


def test_google_calendar_sequence_only_change_not_emitted(mock_services, config):
    """Bug 1: A change to only 'sequence' should not emit an UPDATED event."""
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    old_event = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1",
        "sequence": 0,
    }

    new_event = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": old_event["start"]["dateTime"]},
        "end": {"dateTime": old_event["end"]["dateTime"]},
        "status": "confirmed",
        "etag": "v2",
        "sequence": 1,
    }

    mock_services.kv.get.return_value = old_event

    events = source._classify_event_change("primary", new_event)
    assert len(events) == 0


def test_google_calendar_payload_metadata_only_change_not_emitted(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    old_event = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1",
        "sequence": 0,
        "iCalUID": "evt1@example.com",
        "reminders": {"useDefault": True},
    }
    new_event = {
        **old_event,
        "etag": "v2",
        "sequence": 1,
        "iCalUID": "evt1-rewritten@example.com",
        "reminders": {"useDefault": False},
    }

    mock_services.kv.get.return_value = old_event

    events = source._classify_event_change("primary", new_event)

    assert events == []


def test_google_calendar_too_old_clears_cache(mock_services):
    """Bug 4b: When _is_too_old filters an event, existing cache cleanup should be queued."""
    config_with_age = GoogleCalendarSourceConfig(
        type="google_calendar",
        token_file="test_token.json",
        calendar_ids=["primary"],
        poll_interval="1m",
        max_event_age_days=2,
    )
    source = GoogleCalendarSource("test_gcal", config_with_age, mock_services, 1)

    past_end = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    old_cached = {
        "id": "evt-old",
        "summary": "Ancient meeting",
        "start": {"dateTime": past_end},
        "end": {"dateTime": past_end},
        "status": "confirmed",
        "etag": "v1",
    }

    mock_services.kv.get.return_value = old_cached

    result = source._classify_event_change_result("primary", old_cached)
    assert result.events == []
    assert len(result.cache_mutations) == 1
    mutation = result.cache_mutations[0]
    assert mutation.calendar_id == "primary"
    assert mutation.event_id == "evt-old"
    assert mutation.event_payload is None
    mock_services.kv.delete.assert_not_called()


@pytest.mark.asyncio
async def test_google_calendar_write_failure_does_not_advance_cache_or_cursor(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    event_start = _future_iso(7)
    event_end = _future_iso(7, 1)

    old_event = {
        "id": "evt1",
        "summary": "Old title",
        "start": {"dateTime": event_start},
        "end": {"dateTime": event_end},
        "status": "confirmed",
        "etag": "v1",
    }
    new_event = {
        "id": "evt1",
        "summary": "New title",
        "start": {"dateTime": event_start},
        "end": {"dateTime": event_end},
        "status": "confirmed",
        "etag": "v2",
    }
    source._fetch_page = MagicMock(return_value={
        "items": [new_event],
        "nextSyncToken": "sync-2",
    })
    mock_services.kv.get.side_effect = _kv_get_from(
        {
            "sync_token:primary": "sync-1",
            "config_fingerprint:primary": _calendar_fingerprint(),
            "snapshot_state:primary": _snapshot_state(1),
            "snap:primary:evt1": old_event,
        }
    )
    mock_services.kv.list_keys_with_prefix.return_value = ["snap:primary:evt1"]
    mock_services.writer.write_events.side_effect = RuntimeError("database unavailable")

    await source.fetch_and_publish_calendar(MagicMock(), "primary")

    forbidden_keys = {"snap:primary:evt1", "sync_token:primary", "lookahead_cursor:primary"}
    for call in mock_services.kv.set.call_args_list:
        assert call.args[1] not in forbidden_keys
    mock_services.kv.delete.assert_not_called()


@pytest.mark.asyncio
async def test_google_calendar_missing_snapshots_rebuilds_without_created_cascade(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    source._rebuild_sync_baseline = MagicMock(return_value=True)
    mock_services.kv.get.side_effect = _kv_get_from(
        {
            "sync_token:primary": "sync-1",
            "config_fingerprint:primary": _calendar_fingerprint(),
        }
    )
    mock_services.kv.list_keys_with_prefix.return_value = []

    await source.fetch_and_publish_calendar(MagicMock(), "primary")

    source._rebuild_sync_baseline.assert_called_once()
    mock_services.writer.write_events.assert_not_called()


def test_google_calendar_future_cancelled_recurring_instance_ignored_when_never_seen(mock_services, config):
    config.max_into_future = "14d"
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    future_start = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    cancelled_instance = {
        "id": "evt1_future",
        "status": "cancelled",
        "etag": "v2",
        "recurringEventId": "evt1",
        "originalStartTime": {"dateTime": future_start},
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    mock_services.kv.get.return_value = None

    result = source._classify_event_change_result("primary", cancelled_instance)

    assert result.events == []
    assert result.cache_mutations == []


def test_google_calendar_filtered_baseline_snapshot_not_stored(mock_services):
    config_with_filter = GoogleCalendarSourceConfig(
        type="google_calendar",
        token_file="test_token.json",
        filters=[
            {"ignore_private": {"in": "summary", "contains": "Private"}}
        ],
    )
    source = GoogleCalendarSource("test_gcal", config_with_filter, mock_services, 1)

    event_item = {
        "id": "filtered",
        "summary": "Private appointment",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1",
    }

    assert source._should_store_baseline_snapshot("primary", event_item) is False


def test_google_calendar_cached_filtered_tombstone_not_emitted(mock_services):
    config_with_filter = GoogleCalendarSourceConfig(
        type="google_calendar",
        token_file="test_token.json",
        filters=[
            {"ignore_private": {"in": "summary", "contains": "Private"}}
        ],
    )
    source = GoogleCalendarSource("test_gcal", config_with_filter, mock_services, 1)
    previous_event = {
        "id": "filtered",
        "summary": "Private appointment",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1",
    }
    tombstone = {
        "id": "filtered",
        "status": "cancelled",
        "etag": "v2",
    }

    result = source._classify_event_change_result(
        "primary",
        tombstone,
        previous_event=previous_event,
    )

    assert result.events == []
    assert len(result.cache_mutations) == 1
    assert result.cache_mutations[0].event_payload is None


def test_google_calendar_past_cancelled_recurring_instance_queues_cache_delete_without_event(mock_services, config):
    config.max_event_age_days = 1.0
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    past_start = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    old_event = {
        "id": "evt1_past",
        "summary": "Old standup",
        "status": "confirmed",
        "etag": "v1",
        "recurringEventId": "evt1",
        "originalStartTime": {"dateTime": past_start},
        "start": {"dateTime": past_start},
        "end": {"dateTime": past_start},
    }
    cancelled_instance = {
        "id": "evt1_past",
        "status": "cancelled",
        "etag": "v2",
        "recurringEventId": "evt1",
        "originalStartTime": {"dateTime": past_start},
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    mock_services.kv.get.return_value = old_event

    result = source._classify_event_change_result("primary", cancelled_instance)

    assert result.events == []
    assert len(result.cache_mutations) == 1
    assert result.cache_mutations[0].event_payload is None


def test_google_calendar_fetch_page_uses_explicit_time_max_without_sync_token(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    service = MagicMock()
    execute = service.events.return_value.list.return_value.execute
    execute.return_value = {"items": [], "nextSyncToken": "sync-2"}

    source._fetch_page(
        service,
        "primary",
        time_min="2026-01-01T00:00:00+00:00",
        time_max="2026-01-15T00:00:00+00:00",
    )

    _, kwargs = service.events.return_value.list.call_args
    assert kwargs["timeMin"] == "2026-01-01T00:00:00+00:00"
    assert kwargs["timeMax"] == "2026-01-15T00:00:00+00:00"
    assert "syncToken" not in kwargs


@pytest.mark.asyncio
async def test_google_calendar_rolling_lookahead_emits_events_entering_window(mock_services, config):
    config.max_into_future = "14d"
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    old_cursor = (datetime.now(timezone.utc) + timedelta(days=14, hours=-1)).isoformat()
    event_item = {
        "id": "entering_window",
        "summary": "Soon useful meeting",
        "start": {"dateTime": (datetime.now(timezone.utc) + timedelta(days=14, minutes=-30)).isoformat()},
        "end": {"dateTime": (datetime.now(timezone.utc) + timedelta(days=14, minutes=30)).isoformat()},
        "status": "confirmed",
        "etag": "v1",
    }
    source._fetch_page = MagicMock(side_effect=[
        {"items": [], "nextSyncToken": "sync-2"},
        {"items": [event_item]},
    ])
    mock_services.kv.get.side_effect = _kv_get_from(
        {
            "sync_token:primary": "sync-1",
            "config_fingerprint:primary": '{"max_into_future": 1209600.0}',
            "snapshot_state:primary": _snapshot_state(0),
            "lookahead_cursor:primary": old_cursor,
        }
    )
    mock_services.kv.list_keys_with_prefix.return_value = []

    await source.fetch_and_publish_calendar(MagicMock(), "primary")

    assert source._fetch_page.call_count == 2
    lookahead_kwargs = source._fetch_page.call_args_list[1].kwargs
    assert lookahead_kwargs["sync_token"] is None
    assert lookahead_kwargs["time_min"] == old_cursor
    assert lookahead_kwargs["time_max"] is not None

    mock_services.writer.write_events.assert_called_once()
    _, emitted = mock_services.writer.write_events.call_args.args
    assert len(emitted) == 1
    assert emitted[0].event_type == CalendarEventType.CREATED
    assert emitted[0].entity_id == "primary:entering_window"

    set_keys = [call.args[1] for call in mock_services.kv.set.call_args_list]
    assert "snap:primary:entering_window" in set_keys
    assert "lookahead_cursor:primary" in set_keys
    assert "sync_token:primary" in set_keys


@pytest.mark.asyncio
async def test_google_calendar_missing_lookahead_cursor_initializes_without_scan(mock_services, config):
    config.max_into_future = "14d"
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    source._fetch_page = MagicMock(return_value={"items": [], "nextSyncToken": "sync-2"})
    mock_services.kv.get.side_effect = _kv_get_from(
        {
            "sync_token:primary": "sync-1",
            "config_fingerprint:primary": '{"max_into_future": 1209600.0}',
            "snapshot_state:primary": _snapshot_state(0),
        }
    )
    mock_services.kv.list_keys_with_prefix.return_value = []

    await source.fetch_and_publish_calendar(MagicMock(), "primary")

    source._fetch_page.assert_called_once()
    mock_services.writer.write_events.assert_not_called()
    mock_services.kv.set.assert_any_call(1, "sync_token:primary", "sync-2")
    set_keys = [call.args[1] for call in mock_services.kv.set.call_args_list]
    assert "lookahead_cursor:primary" in set_keys


@pytest.mark.asyncio
async def test_google_calendar_rolling_lookahead_does_not_reconcile_absent_events(mock_services, config):
    config.max_into_future = "14d"
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)

    old_cursor = (datetime.now(timezone.utc) + timedelta(days=14, hours=-1)).isoformat()
    cached_event = {
        "id": "cached",
        "summary": "Already tracked",
        "start": {"dateTime": _future_iso(7)},
        "end": {"dateTime": _future_iso(7, 1)},
        "status": "confirmed",
        "etag": "v1",
    }
    source._fetch_page = MagicMock(side_effect=[
        {"items": [], "nextSyncToken": "sync-2"},
        {"items": []},
    ])
    mock_services.kv.get.side_effect = _kv_get_from(
        {
            "sync_token:primary": "sync-1",
            "config_fingerprint:primary": '{"max_into_future": 1209600.0}',
            "snapshot_state:primary": _snapshot_state(1),
            "lookahead_cursor:primary": old_cursor,
            "snap:primary:cached": cached_event,
        }
    )
    mock_services.kv.list_keys_with_prefix.return_value = ["snap:primary:cached"]

    await source.fetch_and_publish_calendar(MagicMock(), "primary")

    assert source._fetch_page.call_count == 2
    mock_services.writer.write_events.assert_not_called()
    assert call(1, "snap:primary:cached") not in mock_services.kv.delete.call_args_list
