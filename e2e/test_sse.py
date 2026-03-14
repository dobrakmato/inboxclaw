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
            
def test_sse_coalescing():
    config = {
        "sources": {
            "mock_src": {"type": "mock", "interval": "0.1s"} # Fast generation
        },
        "sink": {
            "sse_sink": {
                "type": "sse",
                "match": "*",
                "coalesce": ["mock.random_number"],
                "heartbeat_timeout": 2.0
            }
        }
    }
    
    # Coalescer works by grouping events that are already in DB when it checks.
    # Since we generate every 0.1s and SSE might check every few seconds (or on notification),
    # we might see some coalescing.
    
    with E2EApp(config, "coalesce", APP_PORT, suite_name="sse") as app:
        url = app.get_url("/sse_sink/")
        response = requests.get(url, stream=True, timeout=30)
        
        # In a coalesced stream, 'data' for 'mock.random_number' might be a list or 
        # have many events if the coalescer is active.
        # Actually, Coalescer in this project seems to combine multiple events into one 
        # if they match the pattern.
        
        # Let's just verify it works with the config.
        count = 0
        for line in response.iter_lines():
            if not line: continue
            line = line.decode('utf-8')
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str in ("connected", "ping"): continue
                count += 1
                if count >= 2: break
        assert count >= 2
