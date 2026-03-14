from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, JSON, ForeignKey, select, text
from sqlalchemy.orm import sessionmaker, relationship, DeclarativeBase
from datetime import datetime, timezone
import os

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
    event_id = Column(Integer, ForeignKey('events.id'))
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
    event_id = Column(Integer, ForeignKey('events.id'), nullable=False)
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
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)
