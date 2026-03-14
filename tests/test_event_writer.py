import pytest
import logging
from unittest.mock import MagicMock
from src.pipeline.writer import EventWriter
from src.schemas import NewEvent
from src.database import init_db, Event, Source
from src.services import AppServices
from src.config import Config, DatabaseConfig, ServerConfig
from src.pipeline.notifier import EventNotifier
from fastapi import FastAPI

@pytest.fixture
def session_maker():
    # Use in-memory SQLite for testing
    return init_db(":memory:")

@pytest.fixture
def services(session_maker):
    config = Config(
        server=ServerConfig(),
        database=DatabaseConfig(days=30, db_path=":memory:"),
        sources={},
        sink={}
    )
    notifier = EventNotifier()
    app = FastAPI()
    
    # We need to manually mock AppServices as it's not easily instantiable with real components in a unit test
    # but let's try to use it with our session_maker
    services = AppServices(
        app=app,
        config=config,
        db_session_maker=session_maker,
        notifier=notifier
    )
    return services

def test_event_writer_deduplication(services, session_maker):
    writer = services.writer
    source_id = 1
    
    # Setup: Create a source in the DB to satisfy foreign key (actually not needed if we don't use real FK but Event has ForeignKey('sources.id'))
    with session_maker() as session:
        src = Source(id=source_id, name="test_source", type="mock")
        session.add(src)
        session.commit()

    events = [
        NewEvent(event_id="unique_1", event_type="test.type", data={"key": "val1"}),
        NewEvent(event_id="unique_1", event_type="test.type", data={"key": "val2"}), # Duplicate event_id
        NewEvent(event_id="unique_2", event_type="test.type", data={"key": "val3"}),
    ]
    
    # First write
    new_count = writer.write_events(source_id, events)
    assert new_count == 2 # Only unique_1 and unique_2 should be written
    
    # Verify in DB
    with session_maker() as session:
        db_events = session.query(Event).all()
        assert len(db_events) == 2
        ids = [e.event_id for e in db_events]
        assert "unique_1" in ids
        assert "unique_2" in ids
        
        # Verify val1 was kept (the first one)
        ev1 = session.query(Event).filter_by(event_id="unique_1").first()
        assert ev1.data["key"] == "val1"

    # Second write with same events
    new_count_2 = writer.write_events(source_id, events)
    assert new_count_2 == 0 # No new events should be written
    
def test_multiple_events_single_entity(services, session_maker):
    writer = services.writer
    source_id = 1
    
    with session_maker() as session:
        src = Source(id=source_id, name="test_source", type="mock")
        session.add(src)
        session.commit()

    events = [
        NewEvent(event_id="event_1", event_type="test.type", entity_id="entity_A", data={"v": 1}),
        NewEvent(event_id="event_2", event_type="test.type", entity_id="entity_A", data={"v": 2}),
    ]
    
    new_count = writer.write_events(source_id, events)
    assert new_count == 2 # Both should be written as they have different event_id
    
    with session_maker() as session:
        db_events = session.query(Event).filter_by(entity_id="entity_A").all()
        assert len(db_events) == 2

def test_event_writer_same_id_different_sources(services, session_maker):
    writer = services.writer
    
    # Setup: Create two sources in the DB
    with session_maker() as session:
        src1 = Source(id=1, name="source1", type="mock")
        src2 = Source(id=2, name="source2", type="mock")
        session.add_all([src1, src2])
        session.commit()

    events1 = [
        NewEvent(event_id="common_id", event_type="test.type", data={"source": 1}),
    ]
    events2 = [
        NewEvent(event_id="common_id", event_type="test.type", data={"source": 2}),
    ]
    
    # First write from source 1
    new_count1 = writer.write_events(1, events1)
    assert new_count1 == 1
    
    # Second write from source 2 with SAME event_id
    new_count2 = writer.write_events(2, events2)
    assert new_count2 == 1 # Now allowed as source_id is different
    
    with session_maker() as session:
        db_events = session.query(Event).all()
        assert len(db_events) == 2
        for e in db_events:
            assert e.event_id == "common_id"

def test_event_writer_integrity_error_handling(services, session_maker):
    writer = services.writer
    source_id = 1
    
    with session_maker() as session:
        src = Source(id=source_id, name="test_source", type="mock")
        session.add(src)
        session.commit()
    
    with session_maker() as session:
        # Manually add an event to the DB
        ev = Event(event_id="duplicate", source_id=source_id, event_type="test", data={})
        session.add(ev)
        session.commit()

    # Now try to write a list containing the duplicate and some new ones
    events = [
        NewEvent(event_id="duplicate", event_type="test", data={"v": "new"}),
        NewEvent(event_id="really_new", event_type="test", data={}),
    ]
    
    new_count = writer.write_events(source_id, events)
    
    # Should skip "duplicate" and only write "really_new"
    assert new_count == 1
    
    with session_maker() as session:
        assert session.query(Event).count() == 2
        assert session.query(Event).filter_by(event_id="really_new").first() is not None
