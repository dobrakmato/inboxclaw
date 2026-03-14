import time
import pytest
import requests
from e2e.utils import E2EApp

APP_PORT = 8103

def test_http_pop_standard_cycle():
    config = {
        "sources": {
            "mock_src": {"type": "mock", "interval": "1s"}
        },
        "sink": {
            "pop_sink": {
                "type": "http_pop",
                "match": "*"
            }
        }
    }
    
    with E2EApp(config, "std", APP_PORT, suite_name="http_pop") as app:
        # Wait for some events
        time.sleep(5)
        
        extract_url = app.get_url("/pop_sink/extract")
        mark_url = app.get_url("/pop_sink/mark-processed")
        
        # 1. Extract
        resp = requests.get(extract_url)
        assert resp.status_code == 200
        data = resp.json()
        events = data["events"]
        batch_id = data["batch_id"]
        assert len(events) >= 3
        assert batch_id is not None
        
        # 2. Mark processed
        mark_resp = requests.post(f"{mark_url}?batch_id={batch_id}")
        assert mark_resp.status_code == 200
        assert mark_resp.json()["marked_count"] == len(events)
        
        # 3. Verify next extract doesn't contain those (it might contain new ones)
        next_resp = requests.get(extract_url)
        new_events = next_resp.json()["events"]
        # Check that none of the old event IDs are in the new batch
        old_ids = {e["event_id"] for e in events}
        new_ids = {e["event_id"] for e in new_events}
        assert not (old_ids & new_ids)

def test_http_pop_batching():
    config = {
        "sources": {
            "mock_src": {"type": "mock", "interval": "1s"}
        },
        "sink": {
            "pop_sink": {
                "type": "http_pop",
                "match": "*"
            }
        }
    }
    
    with E2EApp(config, "batch", APP_PORT, suite_name="http_pop") as app:
        time.sleep(10)
        
        extract_url = app.get_url("/pop_sink/extract")
        
        # Extract with batch size
        resp = requests.get(f"{extract_url}?batch_size=2")
        data = resp.json()
        assert len(data["events"]) == 2
        assert data["remaining_events"] > 0

def test_http_pop_filtering():
    config = {
        "sources": {
            "mock_src": {"type": "mock", "interval": "1s"}
        },
        "sink": {
            "pop_sink": {
                "type": "http_pop",
                "match": "none_match"
            }
        }
    }
    
    with E2EApp(config, "filter", APP_PORT, suite_name="http_pop") as app:
        time.sleep(5)
        extract_url = app.get_url("/pop_sink/extract")
        resp = requests.get(extract_url)
        assert len(resp.json()["events"]) == 0
