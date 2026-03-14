import logging
from typing import Any, Dict, List, Optional
from fastapi import HTTPException, Query
from sqlalchemy import select, and_, not_, or_
from sqlalchemy.orm import Session
from src.database import Event, HttpPopBatch, HttpPullBatchEvent
from src.services import AppServices
from src.pipeline.coalescer import Coalescer
from src.pipeline.matcher import EventMatcher
from src.config import HttpPopSinkConfig

logger = logging.getLogger(__name__)

class HttpPopSink:
    def __init__(self, name: str, config: HttpPopSinkConfig, services: AppServices):
        self.name = name
        self.config = config
        self.services = services
        
        paths = config.path
        extract_path_suffix = paths.get("extract", "extract").lstrip("/")
        mark_processed_suffix = paths.get("mark_processed", "mark-processed").lstrip("/")
        
        self.extract_path = f"/{name}/{extract_path_suffix}"
        self.mark_processed_path = f"/{name}/{mark_processed_suffix}"
        
        self.matcher = EventMatcher(config.match)
        self.coalescer = None
        if config.coalesce:
            self.coalescer = Coalescer(match_patterns=config.coalesce)
            
        self.setup_endpoints()

    def setup_endpoints(self):
        @self.services.app.get(self.extract_path)
        async def extract(
            event_type: Optional[str] = Query(None, description="Filter by event type (supports * and .*)"),
            batch_size: Optional[int] = Query(None, description="Limit the number of events to extract")
        ):
            return self.handle_extract(event_type=event_type, batch_size=batch_size)

        @self.services.app.post(self.mark_processed_path)
        async def mark_processed(batch_id: int):
            return self.handle_mark_processed(batch_id)

    def handle_extract(self, event_type: Optional[str] = None, batch_size: Optional[int] = None) -> Dict[str, Any]:
        with self.services.db_session_maker() as session:
            # If coalescing, we need to fetch all potentially matching events to coalesce them correctly.
            # If not coalescing, we can apply batch_size directly in the query.
            fetch_size = None if self.coalescer else batch_size
            
            events = self._get_unprocessed_events(session, event_type=event_type, batch_size=fetch_size)
            if not events:
                return {"batch_id": None, "events": [], "remaining_events": 0}
            
            source_ids_to_link: List[int] = []
            if self.coalescer:
                coalesced_events, source_ids_map = self.coalescer.coalesce(events)
                
                # Apply batch_size limit AFTER coalescing
                if batch_size is not None and batch_size > 0:
                    emitted_events = coalesced_events[:batch_size]
                    remaining_coalesced = coalesced_events[batch_size:]
                else:
                    emitted_events = coalesced_events
                    remaining_coalesced = []
                
                # We must link ALL source events that contributed to the EMITTED coalesced events
                for ev in emitted_events:
                    source_ids_to_link.extend(source_ids_map.get(ev.id, []))
                
                events_to_return = emitted_events
                
                # Calculate remaining_count:
                # It should be the number of potential EMITTED events that would remain 
                # AFTER the current batch events are marked processed.
                # Since we fetched ALL unprocessed matching events (fetch_size is None), 
                # remaining_coalesced contains all other potential emitted events.
                remaining_count = len(remaining_coalesced)
            else:
                events_to_return = events
                source_ids_to_link = [e.id for e in events]
                
                # If not coalescing, remaining_count is:
                # total unprocessed events (including those in the current batch because they are not yet processed)
                remaining_count = self._count_unprocessed_events(session, event_type=event_type)
            
            # Create a new batch
            batch = HttpPopBatch()
            session.add(batch)
            session.flush() # Get batch.id
            
            self._link_events_by_id(session, batch.id, source_ids_to_link)
            
            session.commit()
            
            return {
                "batch_id": batch.id,
                "events": [self._format_event(e) for e in events_to_return],
                "remaining_events": max(0, remaining_count)
            }

    def _link_events_by_id(self, session: Session, batch_id: int, event_ids: List[int]):
        for eid in event_ids:
            batch_event = HttpPullBatchEvent(
                batch_id=batch_id,
                event_id=eid,
                processed=False
            )
            session.add(batch_event)

    def handle_mark_processed(self, batch_id: int) -> Dict[str, Any]:
        with self.services.db_session_maker() as session:
            # Check if batch exists
            batch = session.get(HttpPopBatch, batch_id)
            if not batch:
                raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")

            # Mark all events in this batch as processed
            stmt = select(HttpPullBatchEvent).where(
                and_(
                    HttpPullBatchEvent.batch_id == batch_id,
                    HttpPullBatchEvent.processed == False
                )
            )
            batch_events = session.scalars(stmt).all()
            
            for be in batch_events:
                be.processed = True
            
            session.commit()
            
            return {"status": "success", "marked_count": len(batch_events)}

    def _get_unprocessed_events(
        self, 
        session: Session, 
        event_type: Optional[str] = None, 
        batch_size: Optional[int] = None
    ) -> List[Event]:
        # Events that are NOT in ANY HttpPullBatchEvent WHERE processed is True
        subq = select(HttpPullBatchEvent.event_id).where(HttpPullBatchEvent.processed == True)
        
        # Build match clause
        final_match = self.matcher.build_sqlalchemy_clause(event_type)

        stmt = select(Event).where(
            and_(
                not_(Event.id.in_(subq)),
                final_match
            )
        ).order_by(Event.id.asc())
        
        if batch_size is not None and batch_size > 0:
            stmt = stmt.limit(batch_size)
            
        return list(session.scalars(stmt).all())

    def _count_unprocessed_events(self, session: Session, event_type: Optional[str] = None) -> int:
        from sqlalchemy import func
        subq = select(HttpPullBatchEvent.event_id).where(HttpPullBatchEvent.processed == True)
        final_match = self.matcher.build_sqlalchemy_clause(event_type)
            
        stmt = select(func.count(Event.id)).where(
            and_(
                not_(Event.id.in_(subq)),
                final_match
            )
        )
        return session.scalar(stmt) or 0


    @property
    def match_patterns(self) -> List[str]:
        return self.matcher.patterns

    @match_patterns.setter
    def match_patterns(self, value: List[str]):
        self.matcher = EventMatcher(value)

    def _build_match_clauses(self, selector: Optional[str] = None) -> List[Any]:
        # Compatibility method for tests
        clause = self.matcher.build_sqlalchemy_clause(selector)
        # Note: the old test expected a List of clauses, but build_sqlalchemy_clause 
        # returns a single combined clause. For tests that check the return type, 
        # we wrap it in a list to satisfy the 'AttributeError' and return something valid.
        from sqlalchemy import true
        if clause is true():
            return [True]
        return [clause]


    def _format_event(self, event: Event) -> Dict[str, Any]:
        return {
            "id": event.id,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "entity_id": event.entity_id,
            "created_at": event.created_at.isoformat() if event.created_at else None,
            "data": event.data,
            "meta": event.meta
        }
