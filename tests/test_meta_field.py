import pytest
from src.pipeline.writer import EventWriter
from src.schemas import NewEvent, EventWithMeta
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
    
    services = AppServices(
        app=app,
        config=config,
        db_session_maker=session_maker,
        notifier=notifier
    )
    return services

def test_event_meta_persistence(services, session_maker):
    writer = services.writer
    source_id = 1
    
    with session_maker() as session:
        src = Source(id=source_id, name="test_source", type="mock")
        session.add(src)
        session.commit()

    meta_data = {"test_key": "test_value"}
    events = [
        NewEvent(
            event_id="meta_test_1", 
            event_type="test.type", 
            data={"foo": "bar"}, 
            meta=meta_data
        ),
        NewEvent(
            event_id="no_meta_test", 
            event_type="test.type", 
            data={"foo": "baz"}
            # meta defaults to {}
        ),
    ]
    
    writer.write_events(source_id, events)
    
    with session_maker() as session:
        # Check event with meta
        ev1 = session.query(Event).filter_by(event_id="meta_test_1").first()
        assert ev1.meta == meta_data
        
        # Check event without meta (should be default {})
        ev2 = session.query(Event).filter_by(event_id="no_meta_test").first()
        assert ev2.meta == {}
        
        # Test EventWithMeta.from_event
        dto1 = EventWithMeta.from_event(ev1)
        assert dto1.meta == meta_data
        
        # Test DTO with extra transient meta
        extra_meta = {"transient": True}
        dto1_extra = EventWithMeta.from_event(ev1, meta=extra_meta)
        assert dto1_extra.meta["test_key"] == "test_value"
        assert dto1_extra.meta["transient"] is True
