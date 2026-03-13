import asyncio
import pytest
from datetime import datetime, timezone
from src.pipeline.notifier import EventNotifier
from src.pipeline.coalescer import Coalescer
from src.database import Event

@pytest.mark.asyncio
async def test_event_notifier():
    notifier = EventNotifier()
    event = notifier.subscribe()
    
    # Check that it's not set initially
    assert not event.is_set()
    
    # Notify and check
    notifier.notify()
    assert event.is_set()
    
    # Reset and check
    event.clear()
    assert not event.is_set()
    
    # Unsubscribe
    notifier.unsubscribe(event)
    notifier.notify()
    # Since we unsubscribed, this specific event object won't be set by notifier.notify() 
    # if it's not in the listeners set.
    # Wait, in the current implementation, if we have the reference, we can check.
    assert not event.is_set()

def test_coalescer_simple():
    coalescer = Coalescer()
    now = datetime.now(timezone.utc)
    
    events = [
        Event(event_id="1", event_type="type1", entity_id="a", created_at=now),
        Event(event_id="2", event_type="type1", entity_id="a", created_at=now),
        Event(event_id="3", event_type="type2", entity_id="b", created_at=now),
    ]
    
    coalesced = coalescer.coalesce(events)
    # Should have 2 events: one for (type1, a) and one for (type2, b)
    assert len(coalesced) == 2
    
    types = [e.event_type for e in coalesced]
    assert "type1" in types
    assert "type2" in types

def test_coalescer_empty():
    coalescer = Coalescer()
    assert coalescer.coalesce([]) == []

def test_coalescer_no_match():
    # Coalescer only matches "matched.*"
    coalescer = Coalescer(match_patterns=["matched.*"])
    now = datetime.now(timezone.utc)
    events = [
        Event(event_id="1", event_type="other", entity_id="a", created_at=now),
        Event(event_id="2", event_type="other", entity_id="a", created_at=now),
    ]
    # Should NOT coalesce because "other" doesn't match "matched.*"
    coalesced = coalescer.coalesce(events)
    assert len(coalesced) == 2

def test_coalescer_match_patterns_property():
    coalescer = Coalescer(match_patterns=["a.*"])
    assert coalescer.match_patterns == ["a.*"]
