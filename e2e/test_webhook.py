import time
import threading
import pytest
import requests
from fastapi import FastAPI, Request
import uvicorn
from e2e.utils import E2EApp

RECEIVER_PORT = 8201
APP_PORT = 8101

@pytest.fixture(scope="module")
def webhook_receiver():
    received_events = []
    app = FastAPI()

    @app.post("/webhook")
    async def receive_webhook(request: Request):
        data = await request.json()
        received_events.append(data)
        return {"status": "ok"}

    @app.get("/events")
    async def get_events():
        return received_events

    @app.delete("/events")
    async def clear_events():
        received_events.clear()
        return {"status": "cleared"}

    def run():
        uvicorn.run(app, host="127.0.0.1", port=RECEIVER_PORT)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    
    # Wait for receiver
    for _ in range(10):
        try:
            requests.get(f"http://127.0.0.1:{RECEIVER_PORT}/events")
            break
        except:
            time.sleep(0.5)
            
    return f"http://127.0.0.1:{RECEIVER_PORT}"

def test_webhook_standard_delivery(webhook_receiver):
    # Clear previous events
    requests.delete(f"{webhook_receiver}/events")
    
    config = {
        "sources": {
            "mock_src": {"type": "mock", "interval": "1s"}
        },
        "sink": {
            "web_sink": {
                "type": "webhook",
                "url": f"{webhook_receiver}/webhook",
                "match": "*"
            }
        }
    }
    
    with E2EApp(config, "std", APP_PORT, suite_name="webhook"):
        # Wait for at least 2 events
        start = time.time()
        while time.time() - start < 10:
            resp = requests.get(f"{webhook_receiver}/events")
            if len(resp.json()) >= 2:
                break
            time.sleep(1)
            
        events = requests.get(f"{webhook_receiver}/events").json()
        assert len(events) >= 2
        assert events[0]["event_type"] == "mock.random_number"

def test_webhook_filtering(webhook_receiver):
    requests.delete(f"{webhook_receiver}/events")
    
    config = {
        "sources": {
            "mock_src": {"type": "mock", "interval": "1s"}
        },
        "sink": {
            "filtered_sink": {
                "type": "webhook",
                "url": f"{webhook_receiver}/webhook",
                "match": "none_matching_pattern"
            }
        }
    }
    
    with E2EApp(config, "filter", APP_PORT, suite_name="webhook"):
        # Wait a few seconds
        time.sleep(5)
        
        resp = requests.get(f"{webhook_receiver}/events")
        assert len(resp.json()) == 0

def test_webhook_multi_match(webhook_receiver):
    requests.delete(f"{webhook_receiver}/events")
    
    config = {
        "sources": {
            "mock_src": {"type": "mock", "interval": "1s"}
        },
        "sink": {
            "multi_sink": {
                "type": "webhook",
                "url": f"{webhook_receiver}/webhook",
                "match": ["mock.*", "other.*"]
            }
        }
    }
    
    with E2EApp(config, "multi", APP_PORT, suite_name="webhook"):
        time.sleep(5)
        resp = requests.get(f"{webhook_receiver}/events")
        assert len(resp.json()) >= 2
