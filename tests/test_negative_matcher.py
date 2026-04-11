import pytest
from sqlalchemy import create_engine, select, true, false, and_, or_, not_
from sqlalchemy.orm import sessionmaker
from src.database import Base, Event
from src.pipeline.matcher import EventMatcher

def test_negative_matches_memory():
    # subscribe to all gmail.*, but exclude message deletions
    m = EventMatcher(patterns=["gmail.*", "!gmail.message_deleted"])
    
    assert m.matches("gmail.new_message") is True
    assert m.matches("gmail.message_deleted") is False
    assert m.matches("other.event") is False

def test_negative_matches_only_negative_memory():
    # Only negative patterns should imply match all else
    m = EventMatcher(patterns=["!secret.*"])
    
    assert m.matches("public.event") is True
    assert m.matches("secret.event") is False
    assert m.matches("secret.other") is False

def test_negative_matches_multiple():
    m = EventMatcher(
        patterns=["a.*", "b.*", "!a.exclude", "!b.exclude"]
    )
    
    assert m.matches("a.include") is True
    assert m.matches("a.exclude") is False
    assert m.matches("b.include") is True
    assert m.matches("b.exclude") is False

def test_negative_matches_sqlalchemy():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    
    with Session() as session:
        # Need a source first
        from src.database import Source
        session.add(Source(id=1, name="test", type="test"))
        session.add_all([
            Event(event_id="1", source_id=1, event_type="gmail.new", entity_id="1"),
            Event(event_id="2", source_id=1, event_type="gmail.delete", entity_id="2"),
            Event(event_id="3", source_id=1, event_type="other.event", entity_id="3"),
        ])
        session.commit()
        
        # match gmail.*, exclude gmail.delete
        m = EventMatcher(patterns=["gmail.*", "!gmail.delete"])
        
        stmt = select(Event).where(m.build_sqlalchemy_clause())
        results = session.scalars(stmt).all()
        
        event_ids = {e.event_id for e in results}
        assert event_ids == {"1"}
        
        # match *, exclude gmail.*
        m2 = EventMatcher(patterns=["!gmail.*"])
        stmt = select(Event).where(m2.build_sqlalchemy_clause())
        results = session.scalars(stmt).all()
        
        event_ids = {e.event_id for e in results}
        assert event_ids == {"3"}
