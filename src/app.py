import logging
import contextlib
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.config import load_config
from src.services import AppServices
from src.database import init_db
from src.pipeline.notifier import EventNotifier
from src.initialization import init_sources, init_sinks
from src.pipeline.cleanup import cleanup_task
from src.pipeline.coalescence_service import CoalescenceBackgroundService

logger = logging.getLogger("inboxclaw")

# Event notifier singleton (or at least stable across lifespan)
event_notifier = EventNotifier()

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan for startup and shutdown events."""
    logger.info("Starting inboxclaw...")
    # Get config path from app state if it was set via CLI
    config_path = getattr(app.state, "config_path", None)
    config = load_config(config_path)
    db_session_maker = init_db(config.database.db_path, echo=config.database.echo)
    
    services = AppServices(
        app=app,
        config=config,
        db_session_maker=db_session_maker,
        notifier=event_notifier
    )
    
    app.state.services = services
    init_sources(services)
    init_sinks(services)
    
    # Start cleanup task
    services.add_task(cleanup_task(services))
    
    # Start Coalescence Background Service
    coalescence_service = CoalescenceBackgroundService(services)
    services.add_task(coalescence_service.run())
    
    logger.info("App initialized.")
    yield
    logger.info("Shutting down inboxclaw...")
    await services.stop_tasks()

app = FastAPI(title="Inboxclaw", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/healthcheck")
async def healthcheck():
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "ok"}
