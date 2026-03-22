import time
import json
import pytest
import requests
from e2e.utils import E2EApp

APP_PORT = 8102

def test_sse_stream():
    config = {
        "sources": {
            "mock_src": {"type": "mock", "interval": "1s"}
        },
        "sink": {
            "sse_sink": {
                "type": "sse",
                "match": "*"
            }
        }
    }
    
    with E2EApp(config, "std", APP_PORT, suite_name="sse") as app:
        url = app.get_url("/sse_sink/")
        response = requests.get(url, stream=True, timeout=30)
        assert response.status_code == 200
        
        count = 0
        for line in response.iter_lines():
            if not line: continue
            line = line.decode('utf-8')
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str in ("connected", "ping"): continue
                
                event = json.loads(data_str)
                assert event["event_type"] == "mock.random_number"
                count += 1
                if count >= 2: break
        
        assert count >= 2

def test_sse_filtering():
    config = {
        "sources": {
            "mock_src": {"type": "mock", "interval": "1s"}
        },
        "sink": {
            "sse_sink": {
                "type": "sse",
                "match": "*",
                "heartbeat_timeout": 1.0 # Short heartbeat for testing
            }
        }
    }
    
    with E2EApp(config, "filter", APP_PORT, suite_name="sse") as app:
        # Filter that matches nothing
        url = app.get_url("/sse_sink/?event_type=non_existent")
        # Use a short timeout to just check the beginning of the stream
        response = requests.get(url, stream=True, timeout=10)
        
        # We should only get "connected" and then heartbeats, no messages
        connected_received = False
        heartbeat_received = False
        
        for line in response.iter_lines():
            if not line: continue
            line = line.decode('utf-8')
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str == "connected":
                    connected_received = True
                    continue
                if data_str == "ping":
                    heartbeat_received = True
                    break # We've seen enough
                
                # If we get any actual event data, it's a failure
                assert "mock.random_number" not in data_str
        
        assert connected_received
        assert heartbeat_received
            
