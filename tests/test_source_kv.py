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

def test_source_kv_updated_at_refreshed(services, session_maker):
    from datetime import datetime, timedelta, timezone
    kv_service = services.kv
    source_id = 1
    
    with session_maker() as session:
        src = Source(id=source_id, name="test_source", type="mock")
        session.add(src)
        session.commit()

    # Set value
    kv_service.set(source_id, "my_key", "val1")
    
    with session_maker() as session:
        kv = session.scalar(select(SourceKV).where(SourceKV.source_id == source_id, SourceKV.key == "my_key"))
        initial_updated_at = kv.updated_at
        
        # Manually set it back in time
        old_time = datetime.now(timezone.utc) - timedelta(hours=1)
        kv.updated_at = old_time
        session.commit()
        
    # Update value
    kv_service.set(source_id, "my_key", "val2")
    
    with session_maker() as session:
        kv = session.scalar(select(SourceKV).where(SourceKV.source_id == source_id, SourceKV.key == "my_key"))
        # It should be refreshed now
        updated_at = kv.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        
        assert updated_at > (datetime.now(timezone.utc) - timedelta(minutes=1))

def test_delete_expired_with_prefix(services, session_maker):
    from datetime import datetime, timedelta, timezone
    import time
    kv_service = services.kv
    source_id = 1
    
    with session_maker() as session:
        src = Source(id=source_id, name="test_source", type="mock")
        session.add(src)
        session.commit()

    # 1. Create an "old" entry
    kv_service.set(source_id, "snap:old", "old_val")
    
    with session_maker() as session:
        old_time = datetime.now(timezone.utc) - timedelta(days=2)
        kv = session.scalar(select(SourceKV).where(SourceKV.source_id == source_id, SourceKV.key == "snap:old"))
        kv.created_at = old_time
        kv.updated_at = old_time
        session.commit()
        
    # 2. Create an entry that was created long ago, but updated recently
    kv_service.set(source_id, "snap:updated", "initial_val")
    with session_maker() as session:
        old_time = datetime.now(timezone.utc) - timedelta(days=2)
        kv = session.scalar(select(SourceKV).where(SourceKV.source_id == source_id, SourceKV.key == "snap:updated"))
        kv.created_at = old_time
        kv.updated_at = old_time
        session.commit()
    
    # Update it now
    kv_service.set(source_id, "snap:updated", "new_val")
    
    # 3. Create a truly new entry
    kv_service.set(source_id, "snap:new", "new_val")
    
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    
    # Perform cleanup using updated_at
    kv_service.delete_expired_with_prefix(source_id, cutoff, prefix="snap:")
    
    # snap:old should be gone (updated_at is 2 days ago)
    assert kv_service.get(source_id, "snap:old") is None
    # snap:updated should REMAIN (updated_at is now, even if created_at was 2 days ago)
    assert kv_service.get(source_id, "snap:updated") == "new_val"
    # snap:new should remain
    assert kv_service.get(source_id, "snap:new") == "new_val"

    # Verify that delete_older_than_with_prefix (based on created_at) would have deleted snap:updated
    with session_maker() as session:
        kv = session.scalar(select(SourceKV).where(SourceKV.source_id == source_id, SourceKV.key == "snap:updated"))
        # SQLite returns naive datetimes even if stored as UTC in some cases depending on how it's handled,
        # but here the cutoff is aware. Let's make sure we compare correctly.
        created_at = kv.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        assert created_at < cutoff
    
    kv_service.delete_older_than_with_prefix(source_id, cutoff, prefix="snap:")
    assert kv_service.get(source_id, "snap:updated") is None

def test_delete_older_than(services, session_maker):
    from datetime import datetime, timedelta, timezone
    kv_service = services.kv
    source_id = 1

    with session_maker() as session:
        src = Source(id=source_id, name="test_source", type="mock")
        session.add(src)
        session.commit()

    # Set some keys
    kv_service.set(source_id, "old_key", "old_val")
    kv_service.set(source_id, "new_key", "new_val")

    with session_maker() as session:
        # Manually set created_at for "old" key
        old_time = datetime.now(timezone.utc) - timedelta(days=2)
        kv = session.scalar(select(SourceKV).where(SourceKV.source_id == source_id, SourceKV.key == "old_key"))
        kv.created_at = old_time
        session.commit()

    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    kv_service.delete_older_than(source_id, cutoff)

    assert kv_service.get(source_id, "old_key") is None
    assert kv_service.get(source_id, "new_key") == "new_val"

def test_list_keys_with_prefix(services, session_maker):
    kv_service = services.kv
    source_id = 1

    with session_maker() as session:
        src = Source(id=source_id, name="test_source", type="mock")
        session.add(src)
        session.commit()

    kv_service.set(source_id, "prefix:1", "val1")
    kv_service.set(source_id, "prefix:2", "val2")
    kv_service.set(source_id, "other:1", "val3")

    keys = kv_service.list_keys_with_prefix(source_id, "prefix:")
    assert len(keys) == 2
    assert "prefix:1" in keys
    assert "prefix:2" in keys
    assert "other:1" not in keys

    keys = kv_service.list_keys_with_prefix(source_id, "non_existent:")
    assert len(keys) == 0
