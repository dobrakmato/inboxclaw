import logging
from sqlalchemy import select
from src.services import AppServices
from src.database import Source
from src.sources.gmail import GmailSource
from src.sources.google_drive import GoogleDriveSource
from src.sources.google_calendar import GoogleCalendarSource
from src.sources.faktury_online import FakturyOnlineSource
from src.sources.mock import MockSource
from src.sources.home_assistant import HomeAssistantSource
from src.sinks.sse import SSESink
from src.sinks.webhook import WebhookSink
from src.sinks.http_pull import HttpPullSink
from src.sinks.win11toast import Win11ToastSink

logger = logging.getLogger("ingest-pipeline")

def init_sources(services: AppServices):
    """Initialize sources based on configuration."""
    with services.db_session_maker() as session:
        for name, s_config in services.config.sources.items():
            s_type = s_config.type
            
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
                source_instance = GmailSource(name, s_config, services, source_id)
                services.sources[name] = source_instance
                services.add_task(source_instance.run())
            elif s_type == "google_drive":
                logger.info(f"Initializing Google Drive source: {name} (id={source_id})")
                source_instance = GoogleDriveSource(name, s_config, services, source_id)
                services.sources[name] = source_instance
                services.add_task(source_instance.run())
            elif s_type == "google_calendar":
                logger.info(f"Initializing Google Calendar source: {name} (id={source_id})")
                source_instance = GoogleCalendarSource(name, s_config, services, source_id)
                services.sources[name] = source_instance
                services.add_task(source_instance.run())
            elif s_type == "faktury_online":
                logger.info(f"Initializing Faktury Online source: {name} (id={source_id})")
                source_instance = FakturyOnlineSource(name, s_config, services, source_id)
                services.sources[name] = source_instance
                services.add_task(source_instance.run())
            elif s_type == "mock":
                logger.info(f"Initializing Mock source: {name} (id={source_id})")
                source_instance = MockSource(name, s_config, services, source_id)
                services.sources[name] = source_instance
                services.add_task(source_instance.start())
            elif s_type == "home_assistant":
                logger.info(f"Initializing Home Assistant source: {name} (id={source_id})")
                source_instance = HomeAssistantSource(name, s_config, services, source_id)
                services.sources[name] = source_instance
                services.add_task(source_instance.run())
            else:
                logger.warning(f"Unknown source type {s_type} for {name}")

def init_sinks(services: AppServices):
    """Initialize sinks based on configuration."""
    for name, snk_config in services.config.sink.items():
        snk_type = snk_config.type
        
        if snk_type == "sse":
            logger.info(f"Initializing SSE sink: {name}")
            services.sinks[name] = SSESink(name, snk_config, services)
        elif snk_type == "webhook":
            logger.info(f"Initializing Webhook sink: {name}")
            sink = WebhookSink(name, snk_config, services)
            services.sinks[name] = sink
            # Start the background task
            services.add_task(sink.start())
        elif snk_type == "http_pull":
            logger.info(f"Initializing HTTP Pull sink: {name}")
            services.sinks[name] = HttpPullSink(name, snk_config, services)
        elif snk_type == "win11toast":
            logger.info(f"Initializing Win11 toast sink: {name}")
            sink = Win11ToastSink(name, snk_config, services)
            services.sinks[name] = sink
            services.add_task(sink.start())
        else:
            logger.warning(f"Sink type {snk_type} for {name} not implemented yet.")
