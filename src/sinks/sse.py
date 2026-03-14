import logging
import asyncio
from typing import Any, Dict, List, Optional
from fastapi import Request, Query
from sse_starlette.sse import EventSourceResponse
from sqlalchemy import select, and_
from sqlalchemy.orm import Session
from src.database import Event
from src.pipeline.coalescer import Coalescer
from src.services import AppServices
from src.pipeline.matcher import EventMatcher

logger = logging.getLogger(__name__)

class SSESink:
    def __init__(self, name: str, config: Dict[str, Any], services: AppServices):
        self.name = name
        self.config = config
        self.services = services
        
        # Determine the base path from sink name, then append path from config if provided
        base_path = f"/{name}"
        config_path = config.get("path", "")
        if config_path:
            # Join paths ensuring no double slashes
            self.path = f"{base_path.rstrip('/')}/{config_path.lstrip('/')}"
        else:
            # Ensure path ends with / or not depending on config, but setup_endpoints 
            # might be sensitive to it.
            self.path = f"{base_path}/"
            
        self.matcher = EventMatcher(config.get("match", "*"))
        self.heartbeat_timeout = float(config.get("heartbeat_timeout", 30.0))
        self.coalescer = None
        if "coalesce" in config:
            self.coalescer = Coalescer(match_patterns=config.get("coalesce"))
        self.setup_endpoints()

    @property
    def match(self) -> Any:
        # SSESink test expects it to be a single string if it was initialized as a string
        # but matcher.patterns is always a list.
        if len(self.matcher.patterns) == 1:
            return self.matcher.patterns[0]
        return self.matcher.patterns

    @match.setter
    def match(self, value: Any):
        self.matcher = EventMatcher(value)

    def setup_endpoints(self):
        @self.services.app.get(self.path)
        async def sse_endpoint(
            request: Request,
            event_type: Optional[str] = Query(None, description="Filter by event type (supports * and .*)")
        ):
            return EventSourceResponse(self.event_generator(request, event_type=event_type))

    async def event_generator(self, request: Request, event_type: Optional[str] = None):
        logger.info(f"SSE client connected to {self.path} with event_type={event_type}")
        
        # We only want events that arrive after the client connects
        # Get the latest event ID to use as a starting point BEFORE yielding "connected"
        # to ensure any events injected immediately after this are captured
        last_event_id = self._get_last_event_id()
        logger.debug(f"Starting SSE stream from event_id > {last_event_id}")
        
        yield {"event": "info", "data": "connected"}
        
        notification_event = self.services.notifier.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    logger.info("SSE client disconnected")
                    break
                
                # Fetch any new events since last_event_id
                events = self._get_new_events(last_event_id, event_type=event_type)
                logger.debug(f"Fetched {len(events)} new events")
                
                if events:
                    # Update last_event_id for the next poll
                    last_event_id = events[-1].id
                    
                    if self.coalescer:
                        events = self.coalescer.coalesce(events)
                    
                    for event in events:
                        import json
                        yield {
                            "event": "message",
                            "id": str(event.id),
                            "data": json.dumps(self._format_event(event))
                        }

                # Wait for a notification OR a timeout for heartbeats
                try:
                    await asyncio.wait_for(notification_event.wait(), timeout=self.heartbeat_timeout)
                    notification_event.clear()
                    logger.debug("Woke up by notification")
                except asyncio.TimeoutError:
                    yield {
                        "event": "heartbeat",
                        "data": "ping"
                    }
        except Exception as e:
            if not isinstance(e, asyncio.CancelledError):
                logger.exception(f"Error in SSE generator for {self.path}: {e}")
        finally:
            self.services.notifier.unsubscribe(notification_event)

    def _get_last_event_id(self) -> int:
        with self.services.db_session_maker() as session:
            from sqlalchemy import func
            # Use try-except to handle cases where 'events' table doesn't exist yet
            try:
                stmt = select(func.max(Event.id))
                return session.scalar(stmt) or 0
            except Exception:
                return 0

    def _get_new_events(self, last_id: int, event_type: Optional[str] = None) -> List[Event]:
        with self.services.db_session_maker() as session:
            # Final match must satisfy BOTH:
            # 1. The config-level matcher (self.matcher)
            # 2. The request-level filter (event_type)
            final_match = self.matcher.build_sqlalchemy_clause(event_type)

            stmt = select(Event).where(
                and_(
                    Event.id > last_id,
                    final_match
                )
            ).order_by(Event.id.asc())
            
            return list(session.scalars(stmt).all())

    def _format_event(self, event: Event) -> Dict[str, Any]:
        return {
            "id": event.id,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "entity_id": event.entity_id,
            "created_at": event.created_at.isoformat() if event.created_at else None,
            "data": event.data
        }
