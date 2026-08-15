import logging
from sqlalchemy import select
from src.services import AppServices
from src.database import Source, Sink
from src.sources.gmail import GmailSource
from src.sources.google_drive import GoogleDriveSource
from src.sources.google_calendar import GoogleCalendarSource
from src.sources.faktury_online import FakturyOnlineSource
from src.sources.mock import MockSource
from src.sources.home_assistant import HomeAssistantSource
from src.sources.fio import FioSource
from src.sources.nordigen import NordigenSource
from src.sources.jira import JiraSource
from src.sources.asana import AsanaSource
from src.sources.google_health import GoogleHealthSource
from src.sources.strava import StravaSource
from src.sources.filesystem import FilesystemSource
from src.sinks.sse import SSESink
from src.sinks.webhook import WebhookSink
from src.sinks.http_pull import HttpPullSink
from src.sinks.win11toast import Win11ToastSink
from src.sinks.command import CommandSink
from src.sinks.folder import FolderSink
from src.sinks.diary import DiarySink

logger = logging.getLogger("inboxclaw")

SOURCE_TYPES = {
    "gmail": GmailSource,
    "google_drive": GoogleDriveSource,
    "google_calendar": GoogleCalendarSource,
    "faktury_online": FakturyOnlineSource,
    "mock": MockSource,
    "home_assistant": HomeAssistantSource,
    "fio": FioSource,
    "nordigen": NordigenSource,
    "jira": JiraSource,
    "asana": AsanaSource,
    "google_health": GoogleHealthSource,
    "strava": StravaSource,
    "filesystem": FilesystemSource,
}


def _health_interval(source_config) -> float | None:
    if getattr(source_config, "watch_mode", None) == "watch":
        return None
    if hasattr(source_config, "effective_poll_interval"):
        return float(source_config.effective_poll_interval)
    if hasattr(source_config, "poll_interval"):
        return float(source_config.poll_interval)
    if hasattr(source_config, "interval"):
        return float(source_config.interval)
    return None

def init_sources(services: AppServices):
    """Initialize sources based on configuration."""
    if "inboxclaw" in services.config.sources:
        raise ValueError("'inboxclaw' is reserved for internal Inboxclaw events")

    with services.db_session_maker() as session:
        internal_source = session.scalar(select(Source).where(Source.name == "inboxclaw"))
        if internal_source is None:
            internal_source = Source(name="inboxclaw", type="inboxclaw")
            session.add(internal_source)
            session.commit()
            session.refresh(internal_source)
        elif internal_source.type != "inboxclaw":
            raise ValueError("Database source name 'inboxclaw' is reserved for internal events")
        services.health.set_internal_source(internal_source.id)

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
            
            services.health.register(
                name,
                s_type,
                source_id,
                expected_interval=_health_interval(s_config),
            )

            source_class = SOURCE_TYPES.get(s_type)
            if source_class is None:
                logger.warning(f"Unknown source type {s_type} for {name}")
                services.health.unhealthy(
                    name,
                    "configuration",
                    f"Source type '{s_type}' is not implemented.",
                )
                continue

            try:
                logger.info("Initializing %s source: %s (id=%s)", s_type, name, source_id)
                source_instance = source_class(name, s_config, services, source_id)
                services.sources[name] = source_instance
                task = services.add_task(source_instance.run())
                services.health.attach_task(name, task)
            except Exception:
                logger.exception("Failed to initialize source '%s'", name)
                services.health.unhealthy(
                    name,
                    "initialization",
                    "The source could not be initialized.",
                    action="Check the Inboxclaw logs for the initialization error.",
                )

def init_sinks(services: AppServices):
    """Initialize sinks based on configuration."""
    with services.db_session_maker() as session:
        for name, snk_config in services.config.sink.items():
            snk_type = snk_config.type
            
            # Ensure sink exists in DB
            sink_row = session.scalar(select(Sink).where(Sink.name == name))
            if not sink_row:
                sink_row = Sink(name=name, type=snk_type)
                session.add(sink_row)
                session.commit()
                session.refresh(sink_row)
            
            sink_id = sink_row.id
            
            if snk_type == "sse":
                logger.info(f"Initializing SSE sink: {name}")
                services.sinks[name] = SSESink(name, snk_config, services)
            elif snk_type == "webhook":
                logger.info(f"Initializing Webhook sink: {name} (id={sink_id})")
                sink = WebhookSink(name, snk_config, services, sink_id)
                services.sinks[name] = sink
                # Start the background task
                services.add_task(sink.start())
            elif snk_type == "http_pull":
                logger.info(f"Initializing HTTP Pull sink: {name} (id={sink_id})")
                services.sinks[name] = HttpPullSink(name, snk_config, services, sink_id)
            elif snk_type == "win11toast":
                logger.info(f"Initializing Win11 toast sink: {name}")
                sink = Win11ToastSink(name, snk_config, services)
                services.sinks[name] = sink
                services.add_task(sink.start())
            elif snk_type == "command":
                logger.info(f"Initializing Command sink: {name} (id={sink_id})")
                sink = CommandSink(name, snk_config, services, sink_id)
                services.sinks[name] = sink
                sink.start()
            elif snk_type == "folder":
                logger.info(f"Initializing Folder sink: {name}")
                sink = FolderSink(name, snk_config, services)
                services.sinks[name] = sink
                services.add_task(sink.start())
            elif snk_type == "diary":
                logger.info(f"Initializing Diary sink: {name}")
                sink = DiarySink(name, snk_config, services)
                services.sinks[name] = sink
                services.add_task(sink.start())
            else:
                logger.warning(f"Sink type {snk_type} for {name} not implemented yet.")
