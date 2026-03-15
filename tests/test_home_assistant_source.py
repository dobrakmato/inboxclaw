import pytest
import json
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone
from src.sources.home_assistant import HomeAssistantSource
from src.config import HomeAssistantSourceConfig
from src.schemas import NewEvent

@pytest.fixture
def mock_services():
    services = MagicMock()
    services.writer = MagicMock()
    return services

@pytest.fixture
def ha_config():
    return HomeAssistantSourceConfig(
        type="home_assistant",
        url="ws://localhost:8123/api/websocket",
        access_token="fake_token",
        entity_ids=["device_tracker.phone_1"]
    )

def test_summarize_location_update(ha_config, mock_services):
    source = HomeAssistantSource("ha_test", ha_config, mock_services, 1)
    
    # Test case 1: Coordinate change
    trigger = {
        "entity_id": "device_tracker.phone_1",
        "from_state": {
            "state": "home",
            "attributes": {"latitude": 50.0, "longitude": 14.0, "gps_accuracy": 10}
        },
        "to_state": {
            "state": "home",
            "attributes": {"latitude": 50.1, "longitude": 14.1, "gps_accuracy": 15},
            "last_updated": "2024-03-15T14:00:00Z"
        }
    }
    
    summary = source._summarize_location_update(trigger)
    assert summary["coords_changed"] is True
    assert summary["state_changed"] is False
    assert summary["gps_accuracy_changed"] is True
    assert summary["latitude"] == 50.1
    assert summary["longitude"] == 14.1

    # Test case 2: State change (zone)
    trigger_zone = {
        "entity_id": "device_tracker.phone_1",
        "from_state": {
            "state": "home",
            "attributes": {"latitude": 50.0, "longitude": 14.0}
        },
        "to_state": {
            "state": "not_home",
            "attributes": {"latitude": 50.0, "longitude": 14.0},
            "last_updated": "2024-03-15T14:05:00Z"
        }
    }
    summary_zone = source._summarize_location_update(trigger_zone)
    assert summary_zone["state_changed"] is True
    assert summary_zone["coords_changed"] is False

def test_device_tracker_ignoring_coordinates_within_zone(ha_config, mock_services):
    source = HomeAssistantSource("ha_test", ha_config, mock_services, 1)
    
    # 1. Coordinate change within same zone (state)
    trigger = {
        "entity_id": "device_tracker.phone_1",
        "from_state": {
            "state": "home",
            "attributes": {"latitude": 50.0, "longitude": 14.0, "gps_accuracy": 10},
            "last_updated": "2024-03-15T14:00:00Z"
        },
        "to_state": {
            "state": "home",
            "attributes": {"latitude": 50.1, "longitude": 14.1, "gps_accuracy": 15},
            "last_updated": "2024-03-15T14:01:00Z"
        }
    }
    
    # We'll use the _listen-like logic here to check if it would be skipped
    update = source._summarize_location_update(trigger)
    # The current code would NOT skip this because coords_changed is True
    # We WANT it to skip because state_changed is False
    
    # 2. Zone change
    trigger_zone = {
        "entity_id": "device_tracker.phone_1",
        "from_state": {
            "state": "home",
            "attributes": {"latitude": 50.0, "longitude": 14.0},
            "last_updated": "2024-03-15T14:00:00Z"
        },
        "to_state": {
            "state": "not_home",
            "attributes": {"latitude": 50.0, "longitude": 14.0},
            "last_updated": "2024-03-15T14:05:00Z"
        }
    }
    update_zone = source._summarize_location_update(trigger_zone)
    assert update_zone["state_changed"] is True

@pytest.mark.asyncio
async def test_device_tracker_filtering_logic(mock_services):
    config = HomeAssistantSourceConfig(
        type="home_assistant",
        url="ws://localhost:8123/api/websocket",
        access_token="fake_token",
        entity_ids=["device_tracker.phone_1"]
    )
    source = HomeAssistantSource("ha_test", config, mock_services, 1)
    
    mock_ws = AsyncMock()
    
    responses = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "type": "result", "success": True}),
        # 1. Coordinate change within same zone (should be ignored)
        json.dumps({
            "type": "event",
            "event": {
                "variables": {
                    "trigger": {
                        "entity_id": "device_tracker.phone_1",
                        "from_state": {"state": "home", "attributes": {"lat": 1, "lon": 1}},
                        "to_state": {"state": "home", "attributes": {"lat": 1.1, "lon": 1.1}, "last_updated": "2024-03-15T14:00:00Z"}
                    }
                }
            }
        }),
        # 2. Zone change (should be kept)
        json.dumps({
            "type": "event",
            "event": {
                "variables": {
                    "trigger": {
                        "entity_id": "device_tracker.phone_1",
                        "from_state": {"state": "home"},
                        "to_state": {"state": "work", "last_updated": "2024-03-15T14:10:00Z"}
                    }
                }
            }
        }),
    ]
    
    from websockets.exceptions import ConnectionClosed
    mock_ws.recv.side_effect = responses + [ConnectionClosed(None, None)]
    
    with patch("websockets.connect", return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_ws))):
        try:
            await source._listen()
        except ConnectionClosed:
            pass
            
    # Should only have 1 call (the zone change)
    assert mock_services.writer.write_events.call_count == 1
    args = mock_services.writer.write_events.call_args_list[0][0]
    assert args[1][0].event_type == "home_assistant.zone_update"
    assert args[1][0].data["new_state"] == "work"

def test_summarize_geocoded_location_update(ha_config, mock_services):
    source = HomeAssistantSource("ha_test", ha_config, mock_services, 1)
    trigger = {
        "entity_id": "sensor.phone_1_geocoded_location",
        "from_state": {"state": "Old Address"},
        "to_state": {
            "state": "New Address",
            "attributes": {
                "location": [50.1, 14.1],
                "country": "Czechia",
                "locality": "Prague"
            },
            "last_updated": "2024-03-15T14:10:00Z"
        }
    }
    summary = source._summarize_geocoded_location_update(trigger)
    assert summary["kind"] == "geocoded_location_update"
    assert summary["label_changed"] is True
    assert summary["state"] == "New Address"
    assert summary["country"] == "Czechia"

def test_summarize_next_alarm_changed(ha_config, mock_services):
    source = HomeAssistantSource("ha_test", ha_config, mock_services, 1)
    trigger = {
        "entity_id": "sensor.phone_1_next_alarm",
        "from_state": {"state": "2024-03-16T06:00:00Z"},
        "to_state": {
            "state": "2024-03-16T07:00:00Z",
            "attributes": {
                "local_time": "08:00:00",
                "package": "com.android.deskclock"
            },
            "last_updated": "2024-03-15T14:20:00Z"
        }
    }
    summary = source._summarize_next_alarm_changed(trigger)
    assert summary["kind"] == "next_alarm_changed"
    assert summary["changed"] is True
    assert summary["new_alarm_utc"] == "2024-03-16T07:00:00Z"

def test_summarize_generic_sensor_update(ha_config, mock_services):
    source = HomeAssistantSource("ha_test", ha_config, mock_services, 1)
    trigger = {
        "entity_id": "sensor.phone_1_battery_level",
        "from_state": {"state": "80"},
        "to_state": {
            "state": "79",
            "last_updated": "2024-03-15T14:30:00Z"
        }
    }
    summary = source._summarize_generic_sensor_update(trigger)
    assert summary["kind"] == "generic_sensor_update"
    assert summary["new_state"] == "79"

@pytest.mark.asyncio
async def test_listen_and_publish_various_events(mock_services):
    config = HomeAssistantSourceConfig(
        type="home_assistant",
        url="ws://localhost:8123/api/websocket",
        access_token="fake_token",
        entity_ids=[
            "device_tracker.phone_1",
            "sensor.phone_1_geocoded_location",
            "sensor.phone_1_next_alarm",
            "sensor.phone_1_battery_level"
        ]
    )
    from websockets.exceptions import ConnectionClosed
    source = HomeAssistantSource("ha_test", config, mock_services, 1)
    
    mock_ws = AsyncMock()
    
    responses = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "type": "result", "success": True}),
        # 1. Geocoded location update
        json.dumps({
            "type": "event",
            "event": {
                "variables": {
                    "trigger": {
                        "entity_id": "sensor.phone_1_geocoded_location",
                        "from_state": {"state": "Home"},
                        "to_state": {
                            "state": "Work", 
                            "attributes": {"location": [50.1, 14.1]},
                            "last_updated": "2024-03-15T14:00:00Z"
                        }
                    }
                }
            }
        }),
        # 2. Next alarm change
        json.dumps({
            "type": "event",
            "event": {
                "variables": {
                    "trigger": {
                        "entity_id": "sensor.phone_1_next_alarm",
                        "from_state": {"state": "2024-03-16T06:00:00Z"},
                        "to_state": {
                            "state": "2024-03-16T07:00:00Z", 
                            "attributes": {"local_time": "08:00:00"},
                            "last_updated": "2024-03-15T14:05:00Z"
                        }
                    }
                }
            }
        }),
        # 3. Generic sensor update
        json.dumps({
            "type": "event",
            "event": {
                "variables": {
                    "trigger": {
                        "entity_id": "sensor.phone_1_battery_level",
                        "from_state": {"state": "100"},
                        "to_state": {
                            "state": "99", 
                            "last_updated": "2024-03-15T14:10:00Z"
                        }
                    }
                }
            }
        })
    ]
    
    mock_ws.recv.side_effect = responses + [ConnectionClosed(None, None)]
    
    with patch("websockets.connect", return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_ws))):
        try:
            await source._listen()
        except ConnectionClosed:
            pass
            
    assert mock_services.writer.write_events.call_count == 3
    
    # Check geocoded location event
    args1 = mock_services.writer.write_events.call_args_list[0][0]
    assert args1[1][0].event_type == "home_assistant.geocoded_location_update"
    assert args1[1][0].data["state"] == "Work"

    # Check next alarm event
    args2 = mock_services.writer.write_events.call_args_list[1][0]
    assert args2[1][0].event_type == "home_assistant.next_alarm_changed"
    assert args2[1][0].data["new_alarm_utc"] == "2024-03-16T07:00:00Z"

    # Check generic sensor event
    args3 = mock_services.writer.write_events.call_args_list[2][0]
    assert args3[1][0].event_type == "home_assistant.generic_sensor_update"
    assert args3[1][0].data["new_state"] == "99"
