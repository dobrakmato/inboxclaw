from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, JSON, ForeignKey, select, text, delete, UniqueConstraint
from sqlalchemy.orm import sessionmaker, relationship, DeclarativeBase, reconstructor
from sqlalchemy import event
from datetime import datetime, timezone, timedelta
import os
import logging

logger = logging.getLogger("ingest-pipeline.database")

class Base(DeclarativeBase):
    pass

class Source(Base):
    __tablename__ = 'sources'
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    type = Column(String, nullable=False)
    cursor = Column(String)

class SourceKV(Base):
    __tablename__ = 'source_kv'
    __table_args__ = (
        UniqueConstraint('source_id', 'key', name='_source_key_uc'),
    )
    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey('sources.id'), nullable=False)
    key = Column(String, nullable=False)
    value = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class Sink(Base):
    __tablename__ = 'sinks'
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    type = Column(String, nullable=False)

class Event(Base):
    __tablename__ = 'events'
    __table_args__ = (
        UniqueConstraint('source_id', 'event_id', name='_source_event_uc'),
    )
    id = Column(Integer, primary_key=True)
    event_id = Column(String, nullable=False)
    source_id = Column(Integer, ForeignKey('sources.id'), nullable=False) 
    event_type = Column(String, nullable=False)
    entity_id = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    occurred_at = Column(DateTime)
    data = Column(JSON)
    meta = Column(JSON, default=dict)
    source = relationship("Source")

class PendingEvent(Base):
    __tablename__ = 'pending_events'
    __table_args__ = (
        UniqueConstraint('source_id', 'event_type', 'entity_id', name='_pending_event_key_uc'),
    )
    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey('sources.id'), nullable=False)
    event_type = Column(String, nullable=False)
    entity_id = Column(String, nullable=True)  # Events without entity_id won't be coalesced
    
    # Aggregated Event Data
    data = Column(JSON, nullable=False)
    meta = Column(JSON, default=dict)
    
    # State & Counters
    count = Column(Integer, default=1)  # Number of raw events coalesced
    first_seen_at = Column(DateTime, nullable=False)
    last_seen_at = Column(DateTime, nullable=False)
    flush_at = Column(DateTime, nullable=False, index=True)
    
    # Strategy configuration (stored for the flusher)
    strategy = Column(String, nullable=False)  # "debounce", "batch"
    window_seconds = Column(Integer, nullable=False)

class HttpWebhookDelivery(Base):
    __tablename__ = 'http_webhook_deliveries'
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey('events.id', ondelete="CASCADE"), nullable=False)
    sink_id = Column(Integer, ForeignKey('sinks.id', ondelete="CASCADE"), nullable=False)
    tries = Column(Integer, default=0)
    last_try = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    delivered = Column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint('event_id', 'sink_id', name='_event_sink_webhook_uc'),
    )

class HttpPullBatch(Base):
    __tablename__ = 'http_pull_batches'
    id = Column(Integer, primary_key=True)
    sink_id = Column(Integer, ForeignKey('sinks.id', ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class HttpPullBatchEvent(Base):
    __tablename__ = 'http_pull_batch_events'
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey('http_pull_batches.id', ondelete="CASCADE"), nullable=False)
    event_id = Column(Integer, ForeignKey('events.id', ondelete="CASCADE"), nullable=False)
    processed = Column(Boolean, default=False)

def init_db(db_path: str, echo: bool = False):
    # Ensure directory exists
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir)
        
    engine = create_engine(
        f'sqlite:///{db_path}',
        connect_args={"check_same_thread": False},
        echo=echo
    )
    
    # Enable foreign keys for SQLite
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)

def delete_old_events(session_maker: sessionmaker, retention_days: int):
    """Delete events older than retention_days."""
    if retention_days <= 0:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    
    with session_maker() as session:
        try:
            # We delete events; related entities should be deleted by CASCADE
            stmt = delete(Event).where(Event.created_at < cutoff)
            result = session.execute(stmt)
            session.commit()
            if result.rowcount > 0:
                logger.info(f"Deleted {result.rowcount} old events (retention: {retention_days} days)")
        except Exception as e:
            session.rollback()
            logger.error(f"Error deleting old events: {e}")
            raise
