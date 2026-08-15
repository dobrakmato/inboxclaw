import logging
from typing import List, TYPE_CHECKING
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
        with self.services.db_session_maker() as session:
            new_count = self.write_events_in_session(session, source_id, events)
            session.commit()

        if new_count > 0:
            self.services.notifier.notify()
            logger.info(f"Committed {new_count} new events for source {source_id}")
        return new_count

    def write_events_in_session(
        self,
        session: Session,
        source_id: int,
        events: List[NewEvent],
        *,
        use_savepoints: bool = True,
    ) -> int:
        """Write events without committing, for a caller-owned transaction."""
        source = session.scalar(select(Source).where(Source.id == source_id))
        source_name = source.name if source else None
        coalesce_rules = []
        if source_name and source_name in self.services.config.sources:
            coalesce_rules = self.services.config.sources[source_name].coalesce

        new_count = 0
        seen_ids: set[str] = set()
        for event in events:
            if event.event_id in seen_ids:
                logger.warning(
                    f"Duplicate event_id {event.event_id} in current batch for source {source_id}, skipping."
                )
                continue
            seen_ids.add(event.event_id)

            def write_one() -> bool:
                matched_rule = None
                for rule in coalesce_rules:
                    if EventMatcher(rule.match).matches(event.event_type):
                        matched_rule = rule
                        break

                if matched_rule and self.services.coalescer.handle_event(
                    session,
                    source_id,
                    event,
                    matched_rule,
                ):
                    logger.debug(f"Event {event.event_id} routed to CoalescenceManager")
                    return False

                created = self._write_event_internal(session, source_id, event)
                if created:
                    logger.debug(f"Queued new event: {event.event_id}")
                return created

            if not use_savepoints:
                if write_one():
                    new_count += 1
                continue

            try:
                with session.begin_nested():
                    if write_one():
                        new_count += 1
            except IntegrityError:
                logger.warning(
                    f"Duplicate event_id {event.event_id} for source {source_id} (integrity error), skipping."
                )

        return new_count
