import asyncio
import pytest
import logging
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

from src.database import Base, Event
from src.services import AppServices
from src.pipeline.notifier import EventNotifier
from src.sinks.sse import SSESink

@pytest.fixture
def db_session_maker():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session

@pytest.fixture
def services(db_session_maker):
    return AppServices(
        app=FastAPI(),
        config=None,
        db_session_maker=db_session_maker,
        notifier=EventNotifier()
    )

@pytest.mark.asyncio
async def test_sse_sink_init(services):
    config = {
        "path": "/custom_sse",
        "match": "test.*",
        "heartbeat_timeout": 15.0,
        "coalesce": ["test.*"]
    }
    sink = SSESink("sse", config, services)
    assert sink.name == "sse"
    assert sink.path == "/sse/custom_sse"
    assert sink.match == "test.*"
    assert sink.heartbeat_timeout == 15.0
    assert sink.coalescer is not None

@pytest.mark.asyncio
async def test_sse_generator_real_flow(services):
    sink = SSESink("sse", {"match": "test.*"}, services)
    
    mock_request = MagicMock(spec=Request)
    mock_request.is_disconnected = AsyncMock(return_value=False)
    
    gen = sink.event_generator(mock_request)
    
    # Connection message
    msg = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert msg["event"] == "info"
    
    # 1. Inject an event AFTER connection
    with services.db_session_maker() as session:
        event = Event(
            event_id="e1",
            source_id=1,
            event_type="test.click",
            entity_id="user1",
            data={"foo": "bar"}
        )
        session.add(event)
        session.commit()
    
    # Notify the sink
    services.notifier.notify()
    
    # First real event
    msg = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert msg["event"] == "message"
    import json
    data = json.loads(msg["data"])
    assert data["event_id"] == "e1"
    
    # Set disconnected to True so the next iteration exits
    mock_request.is_disconnected.return_value = True
    services.notifier.notify()
    
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(gen), timeout=1.0)

@pytest.mark.asyncio
async def test_sse_generator_event_type_filtering(services):
    sink = SSESink("sse", {"match": "*"}, services)
    
    mock_request = MagicMock(spec=Request)
    mock_request.is_disconnected = AsyncMock(return_value=False)
    
    # Start generator
    gen = sink.event_generator(mock_request, event_type="type1")
    await anext(gen) # connected
    
    # Inject events AFTER connection
    with services.db_session_maker() as session:
        session.add(Event(event_id="e1", source_id=1, event_type="type1", entity_id="1"))
        session.add(Event(event_id="e2", source_id=1, event_type="type2", entity_id="2"))
        session.commit()
    services.notifier.notify()
    
    # Should only get type1
    msg = await asyncio.wait_for(anext(gen), timeout=1.0)
    import json
    data = json.loads(msg["data"])
    assert data["event_type"] == "type1"
    
    # Now it should wait for more events. We disconnect.
    mock_request.is_disconnected.return_value = True
    services.notifier.notify()

    with pytest.raises(StopAsyncIteration):
         await asyncio.wait_for(anext(gen), timeout=1.0)

@pytest.mark.asyncio
async def test_sse_sink_match_property(services):
    sink = SSESink("sse", {"match": "test.*"}, services)
    assert sink.match == "test.*"
    
    # Test setter
    sink.match = ["a", "b"]
    assert sink.match == ["a", "b"]
    
    sink.match = "single"
    assert sink.match == "single"

@pytest.mark.asyncio
async def test_sse_generator_coalescing(services):
    # Setup sink with coalescing
    config = {
        "match": "*",
        "coalesce": ["test.*"]
    }
    sink = SSESink("sse", config, services)
    
    mock_request = MagicMock(spec=Request)
    mock_request.is_disconnected = AsyncMock(return_value=False)
    
    gen = sink.event_generator(mock_request)
    await anext(gen) # info: connected
    
    # Inject two events that should be coalesced
    with services.db_session_maker() as session:
        session.add(Event(event_id="e1", source_id=1, event_type="test.click", entity_id="u1", data={"v": 1}))
        session.add(Event(event_id="e2", source_id=1, event_type="test.click", entity_id="u1", data={"v": 2}))
        session.commit()
    services.notifier.notify()
    
    # Should get one coalesced message
    msg = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert msg["event"] == "message"
    
    # Clean up
    mock_request.is_disconnected.return_value = True
    services.notifier.notify()

@pytest.mark.asyncio
async def test_sse_generator_heartbeat_yield(services):
    # Set a very short heartbeat timeout to trigger it quickly
    sink = SSESink("sse", {"match": "*", "heartbeat_timeout": 0.1}, services)
    
    mock_request = MagicMock(spec=Request)
    mock_request.is_disconnected = AsyncMock(return_value=False)
    
    gen = sink.event_generator(mock_request)
    await anext(gen) # info: connected
    
    # Wait for heartbeat
    msg = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert msg["event"] == "heartbeat"
    assert msg["data"] == "ping"
    
    # Clean up
    mock_request.is_disconnected.return_value = True
    services.notifier.notify()

@pytest.mark.asyncio
async def test_sse_generator_exception(services, caplog):
    sink = SSESink("sse", {"match": "*"}, services)
    
    mock_request = MagicMock(spec=Request)
    # Raising an unexpected exception during is_disconnected check
    mock_request.is_disconnected = AsyncMock(side_effect=RuntimeError("Unexpected error"))
    
    gen = sink.event_generator(mock_request)
    await anext(gen) # info: connected
    
    # Next anext(gen) will hit the loop, call is_disconnected, and catch the exception
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
        
    assert "Error in SSE generator" in caplog.text
    assert "Unexpected error" in caplog.text

@pytest.mark.asyncio
async def test_sse_generator_event_type_filtering_restrictive(services):
    # Sink configured to ONLY allow 'test.*'
    sink = SSESink("sse", {"match": "test.*"}, services)
    
    mock_request = MagicMock(spec=Request)
    mock_request.is_disconnected = AsyncMock(return_value=False)
    
    # 1. Request for 'test.click' (should work)
    gen1 = sink.event_generator(mock_request, event_type="test.click")
    await anext(gen1) # connected
    
    with services.db_session_maker() as session:
        session.add(Event(event_id="e1", source_id=1, event_type="test.click", entity_id="1"))
        session.add(Event(event_id="e2", source_id=1, event_type="other.type", entity_id="2"))
        session.commit()
    services.notifier.notify()
    
    msg = await asyncio.wait_for(anext(gen1), timeout=1.0)
    import json
    data = json.loads(msg["data"])
    assert data["event_type"] == "test.click"
    
    # 2. Request for 'other.type' (should return NOTHING because sink is restricted to 'test.*')
    # Even though it's requested in the URL, the config 'match' should block it.
    gen2 = sink.event_generator(mock_request, event_type="other.type")
    await anext(gen2) # connected
    
    # It should time out or we can check the database query result indirectly by disconnecting
    mock_request.is_disconnected.return_value = True
    services.notifier.notify()
    
    with pytest.raises(StopAsyncIteration):
         await asyncio.wait_for(anext(gen2), timeout=1.0)

@pytest.mark.asyncio
async def test_sse_sink_endpoint_integration(services):
    # Ensure events table exists
    with services.db_session_maker() as session:
        pass
    
    sink = SSESink("sse", {"match": "*"}, services)
    client = TestClient(services.app)
    
    # Test that the endpoint is reachable
    # For SSE, we can use client.get and it will return the whole stream if it ever ends,
    # or we can use it with a generator. 
    # But for a simple smoke test, let's just check the response headers.
    response = client.get("/sse/")
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
