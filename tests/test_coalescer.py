import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.database import init_db, PendingEvent, Source
from src.schemas import NewEvent
from src.config import CoalesceRule, CoalesceStrategy
from src.pipeline.coalescer import CoalescenceManager

@pytest.fixture
def db_session_maker():
    # Use in-memory SQLite for testing
    return init_db(":memory:")

@pytest.fixture
def session(db_session_maker):
    with db_session_maker() as s:
        # Create a mock source for FK constraint
        source = Source(id=1, name="test_source", type="mock")
        s.add(source)
        s.commit()
        yield s

@pytest.fixture
def mock_services():
    services = MagicMock()
    # CoalescenceManager doesn't actually use services currently except for storing it
    return services

@pytest.fixture
def coalescer(mock_services):
    return CoalescenceManager(mock_services)

def test_handle_event_new_pending(coalescer, session):
    event = NewEvent(
        event_id="evt1",
        event_type="test_type",
        entity_id="entity1",
        data={"key": "val1"},
        meta={"source": "unit_test"}
    )
    rule = CoalesceRule(
        match="test_type",
        strategy=CoalesceStrategy.DEBOUNCE,
        window=60
    )
    
    success = coalescer.handle_event(session, source_id=1, event=event, rule=rule)
    assert success is True
    
    pending = session.scalar(select(PendingEvent).where(PendingEvent.entity_id == "entity1"))
    assert pending is not None
    assert pending.source_id == 1
    assert pending.event_type == "test_type"
    assert pending.entity_id == "entity1"
    assert pending.data == {"key": "val1"}
    assert pending.meta == {"source": "unit_test"}
    assert pending.count == 1
    assert pending.strategy == "debounce"
    assert pending.window_seconds == 60
    assert pending.first_seen_at == pending.last_seen_at
    # flush_at should be roughly now + 60s
    now = datetime.now(timezone.utc)
    # Ensure both are offset-aware for comparison if SQLite returned aware, 
    # but usually it returns naive. Let's make pending.flush_at aware if it's not.
    flush_at = pending.flush_at
    if flush_at.tzinfo is None:
        flush_at = flush_at.replace(tzinfo=timezone.utc)
        
    assert abs((flush_at - (now + timedelta(seconds=60))).total_seconds()) < 5

def test_handle_event_update_existing(coalescer, session):
    # Setup initial state
    first_seen = datetime.now(timezone.utc) - timedelta(minutes=1)
    # SQLite stores naive datetimes usually, so we strip tz if we want to be safe, 
    # but PendingEvent should handle it. 
    # However, for consistency in tests, let's use naive for DB entry if that's what's expected.
    pending = PendingEvent(
        source_id=1,
        event_type="test_type",
        entity_id="entity1",
        data={"key": "val1"},
        meta={"m1": "v1"},
        count=1,
        first_seen_at=first_seen.replace(tzinfo=None),
        last_seen_at=first_seen.replace(tzinfo=None),
        flush_at=(first_seen + timedelta(seconds=60)).replace(tzinfo=None),
        strategy="debounce",
        window_seconds=60
    )
    session.add(pending)
    session.commit()
    session.expire_all() # Ensure we reload from DB
    
    # Handle new event
    event = NewEvent(
        event_id="evt2",
        event_type="test_type",
        entity_id="entity1",
        data={"key": "val2", "new": "data"},
        meta={"m2": "v2"}
    )
    rule = CoalesceRule(
        match="test_type",
        strategy=CoalesceStrategy.DEBOUNCE,
        window=60
    )
    
    success = coalescer.handle_event(session, source_id=1, event=event, rule=rule)
    assert success is True
    session.commit() # Commit to ensure changes are flushed if needed
    
    pending = session.scalar(select(PendingEvent).where(PendingEvent.entity_id == "entity1"))
    assert pending.count == 2
    assert pending.data == {"key": "val2", "new": "data"} # LatestAggregation
    assert pending.meta == {"m1": "v1", "m2": "v2"} # Merged meta
    
    last_seen = pending.last_seen_at
    if last_seen.tzinfo is None: last_seen = last_seen.replace(tzinfo=timezone.utc)
    assert last_seen > first_seen
    
    # Debounce updates flush_at to now + window
    now = datetime.now(timezone.utc)
    flush_at = pending.flush_at
    if flush_at.tzinfo is None: flush_at = flush_at.replace(tzinfo=timezone.utc)
    assert abs((flush_at - (now + timedelta(seconds=60))).total_seconds()) < 5

def test_debounce_strategy_flush_at(coalescer, session):
    rule = CoalesceRule(match="*", strategy=CoalesceStrategy.DEBOUNCE, window=100)
    
    # Event 1
    e1 = NewEvent(event_id="e1", event_type="t", entity_id="id1", data={})
    coalescer.handle_event(session, 1, e1, rule)
    session.commit()
    p1 = session.scalar(select(PendingEvent).where(PendingEvent.entity_id == "id1"))
    f1 = p1.flush_at
    
    # Event 2 after some time
    import time
    time.sleep(1.1) # Sleep a bit more to be sure
    e2 = NewEvent(event_id="e2", event_type="t", entity_id="id1", data={})
    coalescer.handle_event(session, 1, e2, rule)
    session.commit()
    session.refresh(p1)
    f2 = p1.flush_at
    
    assert f2 > f1 # flush_at should have moved forward

def test_batch_strategy_flush_at(coalescer, session):
    rule = CoalesceRule(match="*", strategy=CoalesceStrategy.BATCH, window=100)
    
    # Event 1
    e1 = NewEvent(event_id="e1", event_type="t", entity_id="id1", data={})
    coalescer.handle_event(session, 1, e1, rule)
    session.commit()
    p1 = session.scalar(select(PendingEvent).where(PendingEvent.entity_id == "id1"))
    f1 = p1.flush_at
    
    # Event 2 after some time
    import time
    time.sleep(0.1)
    e2 = NewEvent(event_id="e2", event_type="t", entity_id="id1", data={})
    coalescer.handle_event(session, 1, e2, rule)
    session.commit()
    session.refresh(p1)
    f2 = p1.flush_at
    
    assert f1 == f2 # Batch strategy should NOT move flush_at

def test_missing_entity_id(coalescer, session):
    event = NewEvent(event_id="e1", event_type="t", entity_id=None, data={})
    rule = CoalesceRule(match="*", strategy=CoalesceStrategy.DEBOUNCE, window=60)
    
    success = coalescer.handle_event(session, 1, event, rule)
    assert success is False
    
    count = session.scalar(select(PendingEvent).where(PendingEvent.source_id == 1))
    assert count is None

def test_isolation(coalescer, session):
    rule = CoalesceRule(match="*", strategy=CoalesceStrategy.DEBOUNCE, window=60)
    
    # Same type, different entity
    coalescer.handle_event(session, 1, NewEvent(event_id="e1", event_type="t", entity_id="id1", data={}), rule)
    coalescer.handle_event(session, 1, NewEvent(event_id="e2", event_type="t", entity_id="id2", data={}), rule)
    
    # Different type, same entity
    coalescer.handle_event(session, 1, NewEvent(event_id="e3", event_type="t2", entity_id="id1", data={}), rule)
    
    # Different source, same type/entity
    source2 = Source(id=2, name="test_source2", type="mock")
    session.add(source2)
    session.commit()
    coalescer.handle_event(session, 2, NewEvent(event_id="e4", event_type="t", entity_id="id1", data={}), rule)
    
    results = session.scalars(select(PendingEvent)).all()
    assert len(results) == 4

def test_unsupported_timing_strategy(coalescer, session):
    # Bypass enum validation for testing if possible, or use a mock
    # Since strategy is an Enum, Pydantic will usually catch this if passed through CoalesceRule
    # But CoalescenceManager.handle_event checks timing_strategies.get(rule.strategy)
    
    class BadRule:
        strategy = "INVALID"
        window = 60
        aggregation = "latest"
        
    event = NewEvent(event_id="e1", event_type="t", entity_id="id1", data={})
    
    success = coalescer.handle_event(session, 1, event, BadRule())
    assert success is False

def test_meta_empty_to_non_empty(coalescer, session):
    rule = CoalesceRule(match="*", strategy=CoalesceStrategy.BATCH, window=60)
    
    # First event no meta
    coalescer.handle_event(session, 1, NewEvent(event_id="e1", event_type="t", entity_id="id1", data={}), rule)
    pending = session.scalar(select(PendingEvent).where(PendingEvent.entity_id == "id1"))
    assert pending.meta == {}
    
    # Second event with meta
    coalescer.handle_event(session, 1, NewEvent(event_id="e2", event_type="t", entity_id="id1", data={}, meta={"m": "v"}), rule)
    session.commit()
    session.expire_all()
    pending = session.scalar(select(PendingEvent).where(PendingEvent.entity_id == "id1"))
    assert pending.meta == {"m": "v"}
