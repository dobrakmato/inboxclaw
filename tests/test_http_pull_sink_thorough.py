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
    assert data["remaining_events"] == 25 # total unprocessed
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
    assert data["remaining_events"] == 15 # 25 - 10 confirmed
    batch2_id = data["batch_id"]
    
    # 5. Confirm second batch
    client.post(f"/pull/mark-processed?batch_id={batch2_id}")
    
    # 6. Pull remaining (should be 5)
    resp = client.get("/pull/extract?batch_size=10")
    data = resp.json()
    assert len(data["events"]) == 5
    assert data["remaining_events"] == 5
    batch3_id = data["batch_id"]
    
    # 7. Confirm third batch
    client.post(f"/pull/mark-processed?batch_id={batch3_id}")
    
    # 8. Pull (should be empty)
    resp = client.get("/pull/extract")
    data = resp.json()
    assert len(data["events"]) == 0
    assert data["remaining_events"] == 0

def test_thorough_ttl_and_coalescing_interaction(services, client, db_session_maker):
    """Test that TTL filtering happens before coalescing."""
    # Coalesce 'sensor.temp'
    # Default TTL 1h, prefix 'sensor.*' 10 min
    create_sink("pull", {
        "match": "*",
        "coalesce": ["sensor.*"],
        "ttl_enabled": True,
        "default_ttl": 3600,
        "event_ttl": {"sensor.*": 600} # 10 minutes
    }, services)
    
    now = datetime.now(timezone.utc)
    
    with db_session_maker() as session:
        # Event 1: sensor.temp, entity 1, 15 min old (EXPIRED)
        session.add(Event(event_id="e1", source_id=1, event_type="sensor.temp", entity_id="1", created_at=now - timedelta(minutes=15)))
        # Event 2: sensor.temp, entity 1, 5 min old (VALID)
        session.add(Event(event_id="e2", source_id=1, event_type="sensor.temp", entity_id="1", created_at=now - timedelta(minutes=5)))
        # Event 3: sensor.temp, entity 1, 2 min old (VALID)
        session.add(Event(event_id="e3", source_id=1, event_type="sensor.temp", entity_id="1", created_at=now - timedelta(minutes=2)))
        
        # Event 4: other.event, entity 2, 15 min old (VALID - uses default 1h TTL)
        session.add(Event(event_id="e4", source_id=1, event_type="other.event", entity_id="2", created_at=now - timedelta(minutes=15)))
        session.commit()
        
    # Extract
    resp = client.get("/pull/extract")
    data = resp.json()
    
    # Results expected:
    # - sensor.temp for entity 1: e1 is expired. e2 and e3 should be coalesced. The result should be e3 (latest).
    # - other.event for entity 2: e4 is not expired.
    assert len(data["events"]) == 2
    event_ids = [e["event_id"] for e in data["events"]]
    assert "e3" in event_ids
    assert "e4" in event_ids
    assert "e1" not in event_ids
    assert "e2" not in event_ids
    
    # Confirm
    client.post(f"/pull/mark-processed?batch_id={data['batch_id']}")
    
    # Verify everything is marked
    with db_session_maker() as session:
        # e3 and e4 are the emitted ones.
        # But e2 MUST also be marked processed because it was coalesced into e3.
        # e1 should NOT be marked because it was EXPIRED and not even fetched/considered.
        
        # Check HttpPullBatchEvent
        stmt = select(HttpPullBatchEvent).where(HttpPullBatchEvent.batch_id == data["batch_id"])
        batch_events = session.scalars(stmt).all()
        linked_ids = [be.event_id for be in batch_events]
        
        # We need to find the internal DB IDs
        e1_id = session.scalar(select(Event.id).where(Event.event_id == "e1"))
        e2_id = session.scalar(select(Event.id).where(Event.event_id == "e2"))
        e3_id = session.scalar(select(Event.id).where(Event.event_id == "e3"))
        e4_id = session.scalar(select(Event.id).where(Event.event_id == "e4"))
        
        assert e3_id in linked_ids
        assert e2_id in linked_ids
        assert e4_id in linked_ids
        assert e1_id not in linked_ids # Expired events are not part of the batch

def test_thorough_remaining_events_count_complex(services, client, db_session_maker):
    """Test remaining_events count with combinations of coalescing and filtering."""
    create_sink("pull", {
        "match": ["important.*", "critical.*"],
        "coalesce": ["important.*"]
    }, services)
    
    # Add events:
    # 5 important.a (entity 1) -> 1 coalesced
    # 5 important.b (entity 2) -> 1 coalesced
    # 5 critical.c (no coalescing) -> 5 events
    # 5 ignored.d (filtered by match) -> 0 events
    
    for i in range(5):
        # All important.a events for entity '1'
        add_events(db_session_maker, 1, event_type="important.a", entity_id_prefix="1", start_id=1, event_id_prefix=f"imp_a_{i}")
        # All important.b events for entity '2'
        add_events(db_session_maker, 1, event_type="important.b", entity_id_prefix="2", start_id=1, event_id_prefix=f"imp_b_{i}")
        # All critical.c events for different entities
        add_events(db_session_maker, 1, event_type="critical.c", entity_id_prefix=f"3_{i}", start_id=1, event_id_prefix=f"crit_c_{i}")
        # All ignored.d events for different entities
        add_events(db_session_maker, 1, event_type="ignored.d", entity_id_prefix=f"4_{i}", start_id=1, event_id_prefix=f"ign_d_{i}")
        
    # Total matchable events = 5 + 5 + 5 = 15
    # Total coalesced matchable events = 1 (imp.a) + 1 (imp.b) + 5 (crit.c) = 7
    
    # Pull with batch_size 2
    resp = client.get("/pull/extract?batch_size=2")
    data = resp.json()
    assert len(data["events"]) == 2
    # When coalescing, remaining_events is the total number of coalesced events available.
    assert data["remaining_events"] == 7 # 1 (imp.a) + 1 (imp.b) + 5 (crit.c)

    # Mark processed
    client.post(f"/pull/mark-processed?batch_id={data['batch_id']}")
    
    # Pull remaining
    resp = client.get("/pull/extract")
    data = resp.json()
    assert len(data["events"]) == 5
    assert data["remaining_events"] == 5 # all 5 critical.c events

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
