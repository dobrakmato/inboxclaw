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
    
    coalesced, _ = coalescer.coalesce(events)
    # Should have 2 events: one for (type1, a) and one for (type2, b)
    assert len(coalesced) == 2
    
    types = [e.event_type for e in coalesced]
    assert "type1" in types
    assert "type2" in types

def test_coalescer_empty():
    coalescer = Coalescer()
    assert coalescer.coalesce([]) == ([], {})

def test_coalescer_no_match():
    # Coalescer only matches "matched.*"
    coalescer = Coalescer(match_patterns=["matched.*"])
    now = datetime.now(timezone.utc)
    events = [
        Event(event_id="1", event_type="other", entity_id="a", created_at=now),
        Event(event_id="2", event_type="other", entity_id="a", created_at=now),
    ]
    # Should NOT coalesce because "other" doesn't match "matched.*"
    coalesced, _ = coalescer.coalesce(events)
    assert len(coalesced) == 2

def test_coalescer_meta_and_ordering():
    coalescer = Coalescer()
    t1 = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2023, 1, 1, 12, 5, 0, tzinfo=timezone.utc)
    t3 = datetime(2023, 1, 1, 12, 10, 0, tzinfo=timezone.utc)
    
    events = [
        Event(event_id="2", event_type="type1", entity_id="a", created_at=t2, meta={"initial": "meta"}),
        Event(event_id="1", event_type="type1", entity_id="a", created_at=t1),
        Event(event_id="3", event_type="type1", entity_id="a", created_at=t3),
    ]
    
    coalesced, _ = coalescer.coalesce(events)
    assert len(coalesced) == 1
    
    ev = coalesced[0]
    assert ev.event_id == "3"
    assert ev.meta["coalesced_events"] == 3
    assert ev.meta["first_event_at"] == t1.isoformat()
    assert ev.meta["last_event_at"] == t3.isoformat()
    # Ensure it keeps old meta if latest one had it (oops, in my test case ev id "3" has no meta, 
    # but the one with meta was at t2. Let's adjust the test to check for that).
    
def test_coalescer_meta_preservation():
    coalescer = Coalescer()
    t1 = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2023, 1, 1, 12, 10, 0, tzinfo=timezone.utc)
    
    events = [
        Event(event_id="1", event_type="type1", entity_id="a", created_at=t1),
        Event(event_id="2", event_type="type1", entity_id="a", created_at=t2, meta={"important": "data"}),
    ]
    
    coalesced, _ = coalescer.coalesce(events)
    assert len(coalesced) == 1
    ev = coalesced[0]
    assert ev.meta["important"] == "data"
    assert ev.meta["coalesced_events"] == 2

def test_coalescer_no_entity_id_should_not_coalesce():
    coalescer = Coalescer()
    now = datetime.now(timezone.utc)
    
    events = [
        Event(event_id="1", event_type="type1", entity_id=None, created_at=now),
        Event(event_id="2", event_type="type1", entity_id=None, created_at=now),
    ]
    
    coalesced, _ = coalescer.coalesce(events)
    # They should NOT be coalesced because entity_id is None
    assert len(coalesced) == 2

def test_coalescer_mixed_entities():
    coalescer = Coalescer()
    now = datetime.now(timezone.utc)
    
    events = [
        # To be coalesced (type1, a)
        Event(id=1, event_id="e1", event_type="type1", entity_id="a", created_at=now),
        Event(id=2, event_id="e2", event_type="type1", entity_id="a", created_at=now),
        
        # To be coalesced (type2, b)
        Event(id=3, event_id="e3", event_type="type2", entity_id="b", created_at=now),
        Event(id=4, event_id="e4", event_type="type2", entity_id="b", created_at=now),
        
        # Single event (not coalesced but matches pattern)
        Event(id=5, event_id="e5", event_type="type3", entity_id="c", created_at=now),
        
        # No entity_id (should NOT be coalesced)
        Event(id=6, event_id="e6", event_type="type1", entity_id=None, created_at=now),
        Event(id=7, event_id="e7", event_type="type1", entity_id=None, created_at=now),
    ]
    
    coalesced, _ = coalescer.coalesce(events)
    
    # Expected:
    # 1. Coalesced (type1, a) -> 1 event (id 2)
    # 2. Coalesced (type2, b) -> 1 event (id 4)
    # 3. Single (type3, c)    -> 1 event (id 5)
    # 4. No entity (type1, None) -> 2 events (id 6, 7)
    # Total: 5 events
    
    assert len(coalesced) == 5
    
    ids = [e.id for e in coalesced]
    assert 2 in ids
    assert 4 in ids
    assert 5 in ids
    assert 6 in ids
    assert 7 in ids

def test_coalescer_match_patterns_property():
    coalescer = Coalescer(match_patterns=["a.*"])
    assert coalescer.match_patterns == ["a.*"]
