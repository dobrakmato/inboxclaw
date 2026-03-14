from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, JSON, ForeignKey, select, text, delete
from sqlalchemy.orm import sessionmaker, relationship, DeclarativeBase
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

class Event(Base):
    __tablename__ = 'events'
    id = Column(Integer, primary_key=True)
    event_id = Column(String, unique=True, nullable=False)
    source_id = Column(Integer, ForeignKey('sources.id'), nullable=False) 
    event_type = Column(String, nullable=False)
    entity_id = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    ingested_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    occurred_at = Column(DateTime)
    data = Column(JSON)
    meta = Column(JSON)

class HttpWebhookDelivery(Base):
    __tablename__ = 'http_webhook_deliveries'
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey('events.id', ondelete="CASCADE"))
    tries = Column(Integer, default=0)
    last_try = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    delivered = Column(Boolean, default=False)

class HttpPopBatch(Base):
    __tablename__ = 'http_pop_batches'
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class HttpPullBatchEvent(Base):
    __tablename__ = 'http_pull_batch_events'
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey('http_pop_batches.id'), nullable=False)
    event_id = Column(Integer, ForeignKey('events.id', ondelete="CASCADE"), nullable=False)
    processed = Column(Boolean, default=False)

def init_db(db_path: str):
    # Ensure directory exists
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir)
        
    engine = create_engine(
        f'sqlite:///{db_path}',
        connect_args={"check_same_thread": False}
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
            logger.info(f"Deleted {result.rowcount} old events (retention: {retention_days} days)")
        except Exception as e:
            session.rollback()
            logger.error(f"Error deleting old events: {e}")
            raise
