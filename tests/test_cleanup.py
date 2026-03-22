import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from src.database import init_db, Event, HttpWebhookDelivery, delete_old_events

def test_delete_old_events():
    # Use in-memory database for testing
    session_maker = init_db(":memory:")
    
    with session_maker() as session:
        # Create a source and a sink
        from src.database import Source, Sink
        source = Source(name="test", type="mock")
        sink = Sink(name="sink", type="webhook")
        session.add_all([source, sink])
        session.commit()
        
        # Create events
        now = datetime.now(timezone.utc)
        old_event = Event(
            event_id="old",
            source_id=source.id,
            event_type="test",
            created_at=now - timedelta(days=40)
        )
        new_event = Event(
            event_id="new",
            source_id=source.id,
            event_type="test",
            created_at=now - timedelta(days=20)
        )
        session.add_all([old_event, new_event])
        session.commit()
        
        # Create delivery for old event to test CASCADE
        old_delivery = HttpWebhookDelivery(event_id=old_event.id, sink_id=sink.id)
        session.add(old_delivery)
        session.commit()
        
        old_delivery_id = old_delivery.id

    # Run deletion with 30 days retention
    delete_old_events(session_maker, 30)
    
    with session_maker() as session:
        # Verify old event is gone
        events = session.scalars(select(Event)).all()
        assert len(events) == 1
        assert events[0].event_id == "new"
        
        # Verify old delivery is gone (CASCADE check)
        delivery = session.get(HttpWebhookDelivery, old_delivery_id)
        assert delivery is None

def test_delete_old_events_no_deletion():
    session_maker = init_db(":memory:")
    with session_maker() as session:
        from src.database import Source
        source = Source(name="test", type="mock")
        session.add(source)
        session.commit()
        
        now = datetime.now(timezone.utc)
        event = Event(
            event_id="test",
            source_id=source.id,
            event_type="test",
            created_at=now - timedelta(days=10)
        )
        session.add(event)
        session.commit()

    # Retention 30 days, event is 10 days old -> should stay
    delete_old_events(session_maker, 30)
    
    with session_maker() as session:
        events = session.scalars(select(Event)).all()
        assert len(events) == 1
