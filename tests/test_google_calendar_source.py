import asyncio

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, timezone, timedelta
from googleapiclient.errors import HttpError
from src.sources.google_calendar import GoogleCalendarSource, CalendarEventType
from src.config import GoogleCalendarSourceConfig


def _kv_get_from(mapping):
    return lambda _sid, key: mapping.get(key)


def _calendar_fingerprint() -> str:
    return '{"single_events": true, "max_into_future": 31536000.0}'

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
    assert ev.entity_id == "primary:evt1"
    assert ev.data["event_id"] == "evt1"
    assert ev.data["calendar_id"] == "primary"
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
            return 31536000.0
        return None
    
    mock_services.kv.get.side_effect = kv_get_mock
    
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
        if "config_max_into_future" in key:
            return 31536000.0
        return None

    mock_services.kv.get.side_effect = kv_get_mock

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
    config.single_events = False
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    service = MagicMock()
    execute = service.events.return_value.list.return_value.execute
    execute.return_value = {"items": [], "nextSyncToken": "sync-2"}

    source._fetch_page(service, "primary", sync_token="sync-1")

    _, kwargs = service.events.return_value.list.call_args
    assert kwargs["syncToken"] == "sync-1"
    assert kwargs["showDeleted"] is True
    assert kwargs["singleEvents"] is False
    assert "timeMin" not in kwargs
    assert "timeMax" not in kwargs


@pytest.mark.asyncio
async def test_google_calendar_single_events_change_resets_sync_token(mock_services, config):
    config.calendar_overrides = {"primary": {"single_events": False}}
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    source._rebuild_sync_baseline = MagicMock(return_value=True)

    mock_services.kv.get.side_effect = _kv_get_from(
        {
            "config_fingerprint:primary": '{"single_events": true, "max_into_future": 31536000.0}',
            "sync_token:primary": "sync-1",
        }
    )

    await source.fetch_and_publish_calendar(MagicMock(), "primary")

    mock_services.kv.delete.assert_any_call(1, "sync_token:primary")
    mock_services.kv.delete.assert_any_call(1, "config_fingerprint:primary")
    source._rebuild_sync_baseline.assert_called_once()


def test_google_calendar_cancelled_recurring_instance_keeps_tombstone(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    old_event = {
        "id": "evt1_20260101",
        "summary": "Standup",
        "status": "confirmed",
        "etag": "v1",
        "recurringEventId": "evt1",
        "originalStartTime": {"dateTime": "2026-01-01T09:00:00Z"},
        "start": {"dateTime": "2026-01-01T09:00:00Z"},
        "end": {"dateTime": "2026-01-01T09:30:00Z"},
    }
    cancelled_instance = {
        "id": "evt1_20260101",
        "status": "cancelled",
        "etag": "v2",
        "recurringEventId": "evt1",
        "originalStartTime": {"dateTime": "2026-01-01T09:00:00Z"},
        "updated": "2026-01-01T08:00:00Z",
    }
    mock_services.kv.get.side_effect = lambda sid, key: old_event if key == "snap:primary:evt1_20260101" else None

    events = source._classify_event_change("primary", cancelled_instance)

    assert len(events) == 1
    assert events[0].event_type == CalendarEventType.DELETED
    mock_services.kv.set.assert_called_once_with(1, "snap:primary:evt1_20260101", cancelled_instance)
    mock_services.kv.delete.assert_not_called()


def test_google_calendar_entity_ids_are_calendar_scoped(mock_services, config):
    source = GoogleCalendarSource("test_gcal", config, mock_services, 1)
    event_item = {
        "id": "shared_evt",
        "summary": "Calendar-scoped identity",
        "start": {"dateTime": "2026-01-01T10:00:00Z"},
        "end": {"dateTime": "2026-01-01T11:00:00Z"},
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
        "start": {"dateTime": "2025-06-01T10:00:00Z"},
        "end": {"dateTime": "2025-06-01T11:00:00Z"},
        "status": "confirmed",
        "etag": "v1",
        "sequence": 0,
    }

    new_event = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": "2025-06-01T10:00:00Z"},
        "end": {"dateTime": "2025-06-01T11:00:00Z"},
        "status": "confirmed",
        "etag": "v2",
        "sequence": 1,
    }

    mock_services.kv.get.return_value = old_event

    events = source._classify_event_change("primary", new_event)
    assert len(events) == 0


def test_google_calendar_too_old_clears_cache(mock_services):
    """Bug 4b: When _is_too_old filters an event, existing cache should be cleaned."""
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

    events = source._classify_event_change("primary", old_cached)
    assert events == []
    # Should have deleted the stale cache entry
    mock_services.kv.delete.assert_called_with(1, "snap:primary:evt-old")
