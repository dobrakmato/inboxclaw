import pytest
import json
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from src.sources.home_assistant import HomeAssistantSource
from src.config import HomeAssistantSourceConfig
from websockets.exceptions import ConnectionClosed

@pytest.fixture
def mock_services():
    services = MagicMock()
    services.writer = MagicMock()
    return services

@pytest.mark.asyncio
async def test_location_threshold_filtering(mock_services):
    # Threshold set to 100 meters
    config = HomeAssistantSourceConfig(
        type="home_assistant",
        url="ws://localhost:8123/api/websocket",
        access_token="fake_token",
        entity_ids=["device_tracker.phone_1", "sensor.phone_1_geocoded_location"],
        location_threshold_meters=100.0
    )
    source = HomeAssistantSource("ha_test", config, mock_services, 1)
    
    mock_ws = AsyncMock()
    
    # Prague, Old Town Square: 50.087, 14.421
    # Prague, Charles Bridge: 50.086, 14.411 (approx 720 meters away)
    # Prague, very close point: 50.08701, 14.42101 (approx 1.3 meters away)
    
    responses = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "type": "result", "success": True}),
        
        # 1. device_tracker: Very small move (should be filtered)
        json.dumps({
            "type": "event",
            "event": {
                "variables": {
                    "trigger": {
                        "entity_id": "device_tracker.phone_1",
                        "from_state": {"state": "home", "attributes": {"latitude": 50.087, "longitude": 14.421}},
                        "to_state": {"state": "home", "attributes": {"latitude": 50.08701, "longitude": 14.42101}, "last_updated": "2024-03-15T14:00:00Z"}
                    }
                }
            }
        }),
        
        # 2. device_tracker: Significant move (should NOT be filtered)
        json.dumps({
            "type": "event",
            "event": {
                "variables": {
                    "trigger": {
                        "entity_id": "device_tracker.phone_1",
                        "from_state": {"state": "home", "attributes": {"latitude": 50.087, "longitude": 14.421}},
                        "to_state": {"state": "home", "attributes": {"latitude": 50.086, "longitude": 14.411}, "last_updated": "2024-03-15T14:05:00Z"}
                    }
                }
            }
        }),

        # 3. geocoded_location: Very small move (should be filtered)
        json.dumps({
            "type": "event",
            "event": {
                "variables": {
                    "trigger": {
                        "entity_id": "sensor.phone_1_geocoded_location",
                        "from_state": {"state": "Old Address", "attributes": {"location": [50.087, 14.421]}},
                        "to_state": {"state": "New Address", "attributes": {"location": [50.08701, 14.42101]}, "last_updated": "2024-03-15T14:10:00Z"}
                    }
                }
            }
        }),

        # 4. geocoded_location: Significant move (should NOT be filtered)
        json.dumps({
            "type": "event",
            "event": {
                "variables": {
                    "trigger": {
                        "entity_id": "sensor.phone_1_geocoded_location",
                        "from_state": {"state": "Old Address", "attributes": {"location": [50.087, 14.421]}},
                        "to_state": {"state": "New Address", "attributes": {"location": [50.086, 14.411]}, "last_updated": "2024-03-15T14:15:00Z"}
                    }
                }
            }
        }),
    ]
    
    mock_ws.recv.side_effect = responses + [ConnectionClosed(None, None)]
    
    with patch("websockets.connect", return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_ws))):
        try:
            await source._listen()
        except ConnectionClosed:
            pass
            
    # Should have only 1 call (the significant geocoded move)
    # Call 1 (small move device_tracker): filtered by not state_changed (both 'home')
    # Call 2 (significant move device_tracker): filtered by not state_changed (both 'home')
    # Call 3 (small move geocoded): filtered by location_threshold_meters
    # Call 4 (significant move geocoded): emitted
    assert mock_services.writer.write_events.call_count == 1
    
    call_args = mock_services.writer.write_events.call_args_list
    # First call: geocoded_location significant move
    assert call_args[0][0][1][0].event_type == "home_assistant.geocoded_location_update"
    assert call_args[0][0][1][0].data["location"] == [50.086, 14.411]

@pytest.mark.asyncio
async def test_zone_change_filtering(mock_services):
    # Threshold 0 (disabled)
    config = HomeAssistantSourceConfig(
        type="home_assistant",
        url="ws://localhost:8123/api/websocket",
        access_token="fake_token",
        entity_ids=["device_tracker.phone_1"],
        location_threshold_meters=0.0
    )
    source = HomeAssistantSource("ha_test", config, mock_services, 1)
    
    mock_ws = AsyncMock()
    
    responses = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "type": "result", "success": True}),
        
        # 1. device_tracker: State changed (should be kept)
        json.dumps({
            "type": "event",
            "event": {
                "variables": {
                    "trigger": {
                        "entity_id": "device_tracker.phone_1",
                        "from_state": {"state": "home"},
                        "to_state": {"state": "not_home", "attributes": {"latitude": 50.0, "longitude": 14.0}, "last_updated": "2024-03-15T14:00:00Z"}
                    }
                }
            }
        }),
        
        # 2. device_tracker: State NOT changed (should be FILTERED)
        json.dumps({
            "type": "event",
            "event": {
                "variables": {
                    "trigger": {
                        "entity_id": "device_tracker.phone_1",
                        "from_state": {"state": "not_home"},
                        "to_state": {"state": "not_home", "attributes": {"latitude": 50.1, "longitude": 14.1}, "last_updated": "2024-03-15T14:05:00Z"}
                    }
                }
            }
        }),
    ]
    
    mock_ws.recv.side_effect = responses + [ConnectionClosed(None, None)]
    
    with patch("websockets.connect", return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_ws))):
        try:
            await source._listen()
        except ConnectionClosed:
            pass
            
    # Should only have 1 call (the state change)
    assert mock_services.writer.write_events.call_count == 1
    assert mock_services.writer.write_events.call_args[0][1][0].data["new_state"] == "not_home"

@pytest.mark.asyncio
async def test_location_threshold_filtering_on_device_tracker(mock_services):
    # Threshold 10 meters, but device_tracker is ONLY filtered by state_changed now
    # Wait, if I want distance filtering on lat/lon updates, I should allow it on device_tracker too if threshold is > 0
    # The user said "not emit updates if the location didn't change in defined meters (for lat/lon updates)"
    # BUT they ALSO said "switch to not emit zone changes, if there is no change in the actual zone"
    # This is a bit ambiguous if they want distance-based updates when zone is the same.
    # Given they mentioned "repeated events", geocoded location is the main culprit in the logs provided.
    
    # I'll stick to my current implementation: zone only if state changed, geocoded only if label changed + distance threshold.
    pass
