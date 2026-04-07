import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.database import Event, Source
from src.schemas import NewEvent
from src.pipeline.matcher import EventMatcher

if TYPE_CHECKING:
    from src.services import AppServices

logger = logging.getLogger(__name__)

class EventWriter:
    """
    Common logic for deduplicating and saving events to the database.
    """
    def __init__(self, services: "AppServices"):
        self.services = services

    def _write_event_internal(self, session: Session, source_id: int, event: NewEvent) -> bool:
        """
        Internal method to write a single event to the database if it doesn't already exist.
        Returns True if the event was newly created, False if it was a duplicate.
        """
        existing = session.scalar(
            select(Event).where(
                Event.event_id == event.event_id,
                Event.source_id == source_id
            )
        )
        if existing:
            return False

        new_event = Event(
            event_id=event.event_id,
            source_id=source_id,
            event_type=event.event_type,
            entity_id=event.entity_id,
            data=event.data,
            meta=event.meta,
            occurred_at=event.occurred_at
        )
        session.add(new_event)
        session.flush()
        return True

    def write_events(self, source_id: int, events: List[NewEvent]) -> int:
        """
        Writes a list of events in a single transaction.
        Returns the number of new events created.
        """
        new_count = 0
        seen_ids = set()

        # 1. Get source config to check for coalesce rules
        source_name = None
        with self.services.db_session_maker() as session:
            source = session.scalar(select(Source).where(Source.id == source_id))
            if source:
                source_name = source.name

        coalesce_rules = []
        if source_name and source_name in self.services.config.sources:
            coalesce_rules = self.services.config.sources[source_name].coalesce

        with self.services.db_session_maker() as session:
            for event in events:
                if event.event_id in seen_ids:
                    logger.warning(f"Duplicate event_id {event.event_id} in current batch for source {source_id}, skipping.")
                    continue
                seen_ids.add(event.event_id)

                try:
                    # Use a savepoint to allow continuing on IntegrityError if it happens on flush
                    with session.begin_nested():
                        # 2. Check Coalesce Rules
                        matched_rule = None
                        for rule in coalesce_rules:
                            if EventMatcher(rule.match).matches(event.event_type):
                                matched_rule = rule
                                break
                        
                        if matched_rule:
                            # 3. Path B: Coalesced
                            if self.services.coalescer.handle_event(session, source_id, event, matched_rule):
                                logger.debug(f"Event {event.event_id} routed to CoalescenceManager")
                                continue
                            # If handle_event fails (e.g. no entity_id), fall through to immediate write

                        # 4. Path A: Immediate
                        created = self._write_event_internal(
                            session=session,
                            source_id=source_id,
                            event=event
                        )
                        if created:
                            new_count += 1
                            logger.debug(f"Queued new event: {event.event_id}")
                except IntegrityError:
                    logger.warning(f"Duplicate event_id {event.event_id} for source {source_id} (integrity error), skipping.")
                    continue
            
            if new_count > 0:
                session.commit()
                self.services.notifier.notify()
                logger.info(f"Committed {new_count} new events for source {source_id}")
            else:
                session.commit() # To save pending_events changes
            
        return new_count
