import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from fastapi import FastAPI

from src.database import init_db, Source, SourceKV
from src.services import AppServices
from src.config import Config, DatabaseConfig, ServerConfig
from src.pipeline.notifier import EventNotifier

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

def test_source_kv_operations(services, session_maker):
    kv_service = services.kv
    source_id = 1
    
    # Setup: Create a source in the DB
    with session_maker() as session:
        src = Source(id=source_id, name="test_source", type="mock")
        session.add(src)
        session.commit()

    # Initial get should be None
    assert kv_service.get(source_id, "my_key") is None

    # Set value
    kv_service.set(source_id, "my_key", "my_value")
    assert kv_service.get(source_id, "my_key") == "my_value"

    # Set structured value
    structured_data = {"a": 1, "b": [1, 2, 3]}
    kv_service.set(source_id, "struct_key", structured_data)
    assert kv_service.get(source_id, "struct_key") == structured_data

    # Verify in DB
    with session_maker() as session:
        kv_db = session.scalar(
            select(SourceKV).where(SourceKV.source_id == source_id, SourceKV.key == "struct_key")
        )
        assert kv_db is not None
        assert kv_db.value == structured_data

    # Update value
    kv_service.set(source_id, "my_key", "new_value")
    assert kv_service.get(source_id, "my_key") == "new_value"

    # Set another key
    kv_service.set(source_id, "another_key", "another_value")
    assert kv_service.get(source_id, "another_key") == "another_value"
    assert kv_service.get(source_id, "my_key") == "new_value"

    # Delete key
    kv_service.delete(source_id, "my_key")
    assert kv_service.get(source_id, "my_key") is None
    assert kv_service.get(source_id, "another_key") == "another_value"

    # Delete all keys for source
    kv_service.delete_all(source_id)
    assert kv_service.get(source_id, "another_key") is None

def test_source_kv_non_existent_source(services):
    assert services.kv.get(999, "any") is None
    
    # Should fail due to Foreign Key constraint
    with pytest.raises(IntegrityError):
         services.kv.set(999, "key", "value")

def test_delete_older_than_with_prefix(services, session_maker):
    from datetime import datetime, timedelta, timezone
    kv_service = services.kv
    source_id = 1
    
    with session_maker() as session:
        src = Source(id=source_id, name="test_source", type="mock")
        session.add(src)
        session.commit()

    # Set some keys
    kv_service.set(source_id, "sync_token:123", "token_val")
    kv_service.set(source_id, "snap:123:abc", {"data": "snap_val"})
    
    with session_maker() as session:
        # Manually set created_at for "old" keys
        old_time = datetime.now(timezone.utc) - timedelta(days=2)
        objs = session.scalars(select(SourceKV).where(SourceKV.source_id == source_id)).all()
        for obj in objs:
            obj.created_at = old_time
        session.commit()

    # Set a "new" key that should NOT be deleted regardless of prefix
    kv_service.set(source_id, "snap:new", {"data": "new_snap"})
    
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    
    # We WANT an API that only deletes snaps
    kv_service.delete_older_than_with_prefix(source_id, cutoff, prefix="snap:")
    
    # sync_token should remain even if it's old because it doesn't match the prefix
    assert kv_service.get(source_id, "sync_token:123") == "token_val"
    # snap that is old should be gone
    assert kv_service.get(source_id, "snap:123:abc") is None
    # snap that is new should remain
    assert kv_service.get(source_id, "snap:new") == {"data": "new_snap"}
