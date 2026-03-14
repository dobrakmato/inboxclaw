import logging
import uvicorn
import contextlib
import asyncio
from typing import Dict, Any
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from src.config import load_config, Config
from src.services import AppServices
from src.database import init_db, Source
from src.sources.gmail import GmailSource
from src.sources.mock import MockSource
from src.sinks.sse import SSESink
from src.sinks.webhook import WebhookSink
from src.sinks.http_pop import HttpPopSink
from src.pipeline.notifier import EventNotifier

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ingest-pipeline")

# In-memory storage for initialized components
sources: Dict[str, Any] = {}
sinks: Dict[str, Any] = {}
event_notifier = EventNotifier()

def init_sources(services: AppServices):
    """Initialize sources based on configuration."""
    with services.db_session_maker() as session:
        for name, s_config in services.config.sources.items():
            if s_config is None:
                s_config = {}
            s_type = s_config.get("type", name)
            
            # Ensure source exists in DB
            source = session.scalar(select(Source).where(Source.name == name))
            if not source:
                source = Source(name=name, type=s_type)
                session.add(source)
                session.commit()
                session.refresh(source)
            
            source_id = source.id
            
            if s_type == "gmail":
                logger.info(f"Initializing Gmail source: {name} (id={source_id})")
                sources[name] = GmailSource(name, s_config, services, source_id)
            elif s_type == "mock":
                logger.info(f"Initializing Mock source: {name} (id={source_id})")
                source_instance = MockSource(name, s_config, services, source_id)
                sources[name] = source_instance
                # Since MockSource runs in background, we need to start it.
                # In FastAPI app we can create task here if it's already running, 
                # but init_sources is called from lifespan.
                services.add_task(source_instance.start())
            else:
                logger.warning(f"Unknown source type {s_type} for {name}")

def init_sinks(services: AppServices):
    """Initialize sinks based on configuration."""
    for name, snk_config in services.config.sink.items():
        snk_type = snk_config.get("type", name)
        
        if snk_type == "sse":
            logger.info(f"Initializing SSE sink: {name}")
            sinks[name] = SSESink(name, snk_config, services)
        elif snk_type == "webhook":
            logger.info(f"Initializing Webhook sink: {name}")
            sink = WebhookSink(name, snk_config, services)
            sinks[name] = sink
            # Start the background task
            services.add_task(sink.start())
        elif snk_type == "http_pop":
            logger.info(f"Initializing HTTP Pop sink: {name}")
            sinks[name] = HttpPopSink(name, snk_config, services)
        else:
            logger.warning(f"Sink type {snk_type} for {name} not implemented yet.")

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan for startup and shutdown events."""
    logger.info("Starting ingest pipeline...")
    config = load_config()
    db_session_maker = init_db(config.database.db_path)
    
    services = AppServices(
        app=app,
        config=config,
        db_session_maker=db_session_maker,
        notifier=event_notifier
    )
    
    # Extract source and sink initialization
    app.state.services = services
    init_sources(services)
    init_sinks(services)
    
    logger.info("App initialized.")
    yield
    logger.info("Shutting down ingest pipeline...")
    await services.stop_tasks()

app = FastAPI(title="Ingest Pipeline", lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "ok"}

if __name__ == "__main__":
    conf = load_config()
    uvicorn.run(app, host=conf.server.host, port=conf.server.port)
