import logging
from typing import Any, Dict, List, Optional
from fastapi import HTTPException, Query
from sqlalchemy import select, and_, not_, or_
from sqlalchemy.orm import Session
from src.database import Event, HttpPopBatch, HttpPullBatchEvent
from src.services import AppServices
from src.pipeline.coalescer import Coalescer
from src.pipeline.matcher import EventMatcher

logger = logging.getLogger(__name__)

class HttpPopSink:
    def __init__(self, name: str, config: Dict[str, Any], services: AppServices):
        self.name = name
        self.config = config
        self.services = services
        
        paths = config.get("path", {})
        extract_path_suffix = paths.get("extract", "extract").lstrip("/")
        mark_processed_suffix = paths.get("mark_processed", "mark-processed").lstrip("/")
        
        self.extract_path = f"/{name}/{extract_path_suffix}"
        self.mark_processed_path = f"/{name}/{mark_processed_suffix}"
        
        self.matcher = EventMatcher(config.get("match", ["*"]))
        self.coalescer = None
        if "coalesce" in config:
            self.coalescer = Coalescer(match_patterns=config.get("coalesce"))
            
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
            events = self._get_unprocessed_events(session, event_type=event_type, batch_size=batch_size)
            if not events:
                return {"batch_id": None, "events": [], "remaining_events": 0}
            
            if self.coalescer:
                events = self.coalescer.coalesce(events)
            
            batch = HttpPopBatch()
            session.add(batch)
            session.flush() # Get batch.id
            
            self._link_events_to_batch(session, batch.id, events)
            
            # Count remaining events after this extraction
            remaining_count = self._count_unprocessed_events(session, event_type=event_type)
            
            session.commit()
            
            return {
                "batch_id": batch.id,
                "events": [self._format_event(e) for e in events],
                "remaining_events": remaining_count
            }

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

    def _link_events_to_batch(self, session: Session, batch_id: int, events: List[Event]):
        for event in events:
            batch_event = HttpPullBatchEvent(
                batch_id=batch_id,
                event_id=event.id,
                processed=False
            )
            session.add(batch_event)

    def _format_event(self, event: Event) -> Dict[str, Any]:
        return {
            "id": event.id,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "entity_id": event.entity_id,
            "created_at": event.created_at.isoformat() if event.created_at else None,
            "data": event.data
        }
