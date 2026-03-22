import pytest
import logging
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine, select, StaticPool
from datetime import datetime, timezone, timedelta
import time

from src.database import Base, Event, HttpPullBatch, HttpPullBatchEvent, Sink
from src.services import AppServices
from src.pipeline.notifier import EventNotifier
from src.sinks.http_pull import HttpPullSink

def create_sink(name, config, services):
    with services.db_session_maker() as session:
        sink_row = session.scalar(select(Sink).where(Sink.name == name))
        if not sink_row:
            sink_row = Sink(name=name, type="http_pull")
            session.add(sink_row)
            session.commit()
            session.refresh(sink_row)
        return HttpPullSink(name, config, services, sink_row.id)

@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://", # Pure in-memory
        connect_args={"check_same_thread": False},
        poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine

@pytest.fixture
def db_session_maker(engine):
    Session = sessionmaker(bind=engine)
    return Session

@pytest.fixture
def services(db_session_maker):
    app = FastAPI()
    return AppServices(
        app=app,
        config=None,
        db_session_maker=db_session_maker,
        notifier=EventNotifier()
    )

@pytest.fixture
def client(services):
    return TestClient(services.app)

def add_events(session_maker, count, event_type="test", entity_id_prefix="e", start_id=1, created_at=None, event_id_prefix=None):
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    if event_id_prefix is None:
        event_id_prefix = entity_id_prefix
    with session_maker() as session:
        for i in range(count):
            ev = Event(
                event_id=f"{event_id_prefix}_{start_id + i}",
                source_id=1,
                event_type=event_type,
                entity_id=f"{entity_id_prefix}_{start_id + i}",
                created_at=created_at
            )
            session.add(ev)
        session.commit()

def test_thorough_sequential_polling(services, client, db_session_maker):
    """Test polling multiple times until drain, ensuring counts are correct."""
    create_sink("pull", {"match": "*"}, services)
    
    # Add 25 events
    add_events(db_session_maker, 25, event_type="type1", event_id_prefix="type1")
    
    # 1. Pull 10
    resp = client.get("/pull/extract?batch_size=10")
    data = resp.json()
    assert len(data["events"]) == 10
    # remaining_events = total unprocessed MINUS what we just pulled
    assert data["remaining_events"] == 15 
    batch1_id = data["batch_id"]
    
    # 2. Pull 10 again (should be SAME because not confirmed)
    resp = client.get("/pull/extract?batch_size=10")
    data = resp.json()
    assert len(data["events"]) == 10
    assert data["events"][0]["event_id"] == "type1_1" # same first event
    
    # 3. Confirm first batch
    client.post(f"/pull/mark-processed?batch_id={batch1_id}")
    
    # 4. Pull 10 (should be DIFFERENT events now)
    resp = client.get("/pull/extract?batch_size=10")
    data = resp.json()
    assert len(data["events"]) == 10
    assert data["events"][0]["event_id"] == "type1_11" 
    # 25 total - 10 (this batch) - 10 (confirmed batch) = 5
    assert data["remaining_events"] == 5
    batch2_id = data["batch_id"]
    
    # 5. Confirm second batch
    client.post(f"/pull/mark-processed?batch_id={batch2_id}")
    
    # 6. Pull remaining (should be 5)
    resp = client.get("/pull/extract?batch_size=10")
    data = resp.json()
    assert len(data["events"]) == 5
    # 25 total - 5 (this batch) - 20 (already confirmed) = 0
    assert data["remaining_events"] == 0
    batch3_id = data["batch_id"]
    
    # 7. Confirm third batch
    client.post(f"/pull/mark-processed?batch_id={batch3_id}")
    
    # 8. Pull (should be empty)
    resp = client.get("/pull/extract")
    data = resp.json()
    assert len(data["events"]) == 0
    assert data["remaining_events"] == 0

def test_thorough_multiple_sinks_overlapping(services, client, db_session_maker):
    """Test two sinks matching the same events. They should be completely separate."""
    # Sink A matches 'gmail.*'
    create_sink("sink_a", {"match": "gmail.*"}, services)
    # Sink B matches '*'
    create_sink("sink_b", {"match": "*"}, services)
    
    add_events(db_session_maker, 5, event_type="gmail.new", event_id_prefix="gmail")
    add_events(db_session_maker, 5, event_type="other", event_id_prefix="other")
    
    # 1. Sink A pulls gmail (5 events)
    resp_a = client.get("/sink_a/extract")
    data_a = resp_a.json()
    assert len(data_a["events"]) == 5
    
    # 2. Sink B pulls all (10 events)
    # They are completely separate now.
    resp_b = client.get("/sink_b/extract")
    data_b = resp_b.json()
    assert len(data_b["events"]) == 10
    
    # 3. Sink A confirms its batch
    client.post(f"/sink_a/mark-processed?batch_id={data_a['batch_id']}")
    
    # 4. Sink B pulls again
    # Sink B should STILL see all 10 events if it hasn't confirmed its own batch.
    # If it pulls again (new batch), it should still see all 10 because it hasn't confirmed any.
    resp_b_2 = client.get("/sink_b/extract")
    data_b_2 = resp_b_2.json()
    assert len(data_b_2["events"]) == 10
    
    # 5. Sink B confirms its batch
    client.post(f"/sink_b/mark-processed?batch_id={data_b['batch_id']}")
    
    # 6. Sink B pulls again - now it should only see 0 because it confirmed 10.
    resp_b_3 = client.get("/sink_b/extract")
    assert len(resp_b_3.json()["events"]) == 0

def test_thorough_partial_confirmations(services, client, db_session_maker):
    """Test what happens if we have overlapping batches and confirm only one."""
    create_sink("pull", {"match": "*"}, services)
    
    add_events(db_session_maker, 10, event_id_prefix="test") # events 1-10
    
    # Batch 1: events 1-5
    resp1 = client.get("/pull/extract?batch_size=5")
    batch1 = resp1.json()
    
    # Batch 2: events 1-10 (since Batch 1 not confirmed)
    resp2 = client.get("/pull/extract?batch_size=10")
    batch2 = resp2.json()
    
    # Confirm Batch 1
    client.post(f"/pull/mark-processed?batch_id={batch1['batch_id']}")
    
    # Pull again: should see 6-10? 
    # NO: if we mark batch 1 processed, events 1-5 are marked processed.
    # If we pull again, we should see 6-10.
    resp3 = client.get("/pull/extract")
    batch3 = resp3.json()
    assert len(batch3["events"]) == 5
    assert batch3["events"][0]["event_id"] == "test_6"

    # Now confirm Batch 2 (which contained 1-10)
    # It should mark whatever is still 'unprocessed' in that batch.
    # batch_id 2 has links to events 1-10.
    # 1-5 are already marked processed (via batch 1 linkage).
    # 6-10 are NOT yet marked processed in batch 2 linkage.
    resp_mark = client.post(f"/pull/mark-processed?batch_id={batch2['batch_id']}")
    # It should report 10 because it marks its own links. 
    # Let's check the code: 
    # stmt = select(HttpPullBatchEvent).where(and_(HttpPullBatchEvent.batch_id == batch_id, HttpPullBatchEvent.processed == False))
    # It will only mark those that it hasn't marked yet. 
    # But wait, batch 1 marked its OWN links. Batch 2 has separate links (different HttpPullBatchEvent rows).
    # So Batch 2 will still have processed=False for all 10 events.
    assert resp_mark.json()["marked_count"] == 10
    
    # Final pull should be empty
    resp4 = client.get("/pull/extract")
    assert len(resp4.json()["events"]) == 0
