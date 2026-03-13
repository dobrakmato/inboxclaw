import pytest
from sqlalchemy import create_engine, select, true, false, and_, or_
from sqlalchemy.orm import sessionmaker
from src.database import Base, Event
from src.pipeline.matcher import EventMatcher

def test_matcher_init():
    # String
    m1 = EventMatcher("test.*")
    assert m1.patterns == ["test.*"]
    
    # List
    m2 = EventMatcher(["a", "b"])
    assert m2.patterns == ["a", "b"]
    
    # None (should default to "*")
    m3 = EventMatcher(None)
    assert m3.patterns == ["*"]

def test_matcher_matches_memory():
    m = EventMatcher(["user.*", "system.status", "other"])
    
    assert m.matches("user.login") is True
    assert m.matches("user.logout") is True
    assert m.matches("system.status") is True
    assert m.matches("other") is True
    
    assert m.matches("user") is False # prefix.* means it MUST have a dot after prefix
    assert m.matches("system") is False
    assert m.matches("something.else") is False

def test_matcher_matches_wildcard():
    m = EventMatcher("*")
    assert m.matches("anything") is True
    assert m.matches("") is True

def test_matcher_sqlalchemy_clause_simple():
    m = EventMatcher("test.*")
    clause = m.build_sqlalchemy_clause()
    
    # Basic check - it should use startswith (translated to LIKE in sqlite)
    compiled = clause.compile()
    clause_str = str(compiled)
    assert "LIKE" in clause_str
    # startswith("test.") results in "test.%" in many dialects, but let's see what happened
    assert compiled.params["event_type_1"].startswith("test.")

def test_matcher_sqlalchemy_clause_multiple():
    m = EventMatcher(["a", "b.*"])
    clause = m.build_sqlalchemy_clause()
    # Should be an OR of two conditions
    clause_str = str(clause.compile())
    assert " OR " in clause_str

def test_matcher_sqlalchemy_clause_wildcard():
    m = EventMatcher("*")
    assert m.build_sqlalchemy_clause() is true()

def test_matcher_sqlalchemy_with_selector():
    m = EventMatcher("user.*")
    
    # Selector matches subset
    clause = m.build_sqlalchemy_clause("user.login")
    clause_str = str(clause.compile())
    assert " AND " in clause_str
    
    # If matcher is "*", it should return just the selector clause
    m_all = EventMatcher("*")
    clause_all = m_all.build_sqlalchemy_clause("system.*")
    compiled_all = clause_all.compile()
    clause_all_str = str(compiled_all)
    assert "LIKE" in clause_all_str
    assert compiled_all.params["event_type_1"].startswith("system.")
    assert " AND " not in clause_all_str

def test_matcher_no_patterns():
    # This shouldn't happen with normal init but let's test _patterns_to_clause with empty
    m = EventMatcher([])
    assert m.build_sqlalchemy_clause() is false()
    assert m.matches("anything") is False

def test_matcher_complex_interaction():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    
    with Session() as session:
        # Need a source first
        from src.database import Source
        session.add(Source(id=1, name="test", type="test"))
        session.add(Event(event_id="1", source_id=1, event_type="user.login", entity_id="u1"))
        session.add(Event(event_id="2", source_id=1, event_type="system.boot", entity_id="s1"))
        session.commit()
        
        m = EventMatcher("user.*")
        
        # 1. Matcher only
        stmt = select(Event).where(m.build_sqlalchemy_clause())
        results = session.scalars(stmt).all()
        assert len(results) == 1
        assert results[0].event_type == "user.login"
        
        # 2. Matcher + Selector
        stmt = select(Event).where(m.build_sqlalchemy_clause("user.login"))
        results = session.scalars(stmt).all()
        assert len(results) == 1
        
        # 3. Matcher + Non-matching selector
        stmt = select(Event).where(m.build_sqlalchemy_clause("system.boot"))
        results = session.scalars(stmt).all()
        assert len(results) == 0
