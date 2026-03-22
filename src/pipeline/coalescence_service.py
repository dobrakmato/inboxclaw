import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select, delete
from src.database import PendingEvent, Event
from src.services import AppServices

logger = logging.getLogger(__name__)

class CoalescenceBackgroundService:
    def __init__(self, services: AppServices, poll_interval: float = 5.0):
        self.services = services
        self.poll_interval = poll_interval

    async def run(self):
        logger.info(f"Starting CoalescenceBackgroundService (interval: {self.poll_interval}s)")
        while True:
            try:
                await self.flush_expired()
            except Exception as e:
                logger.error(f"Error in CoalescenceBackgroundService: {e}")
            
            await asyncio.sleep(self.poll_interval)

    async def flush_expired(self):
        now = datetime.now(timezone.utc)
        
        with self.services.db_session_maker() as session:
            # Query pending_events where flush_at <= now()
            stmt = select(PendingEvent).where(PendingEvent.flush_at <= now)
            expired_events = session.scalars(stmt).all()
            
            if not expired_events:
                return

            promoted_count = 0
            for pending in expired_events:
                # Promotion: Create a permanent Event
                # event_id for coalesced event: we can generate a stable one or use a composite
                # According to the plan, we create a permanent event.
                # event_id: {event_type}:{entity_id}:{first_seen_at_timestamp}
                event_id = f"coalesced:{pending.event_type}:{pending.entity_id}:{int(pending.first_seen_at.timestamp())}"
                
                # Check for uniqueness just in case (though it should be unique by timestamp)
                existing = session.scalar(
                    select(Event).where(
                        Event.event_id == event_id,
                        Event.source_id == pending.source_id
                    )
                )
                if existing:
                    # This shouldn't happen often, but if it does, we append more entropy or current time
                    event_id += f":{int(now.timestamp())}"

                meta = (pending.meta or {}).copy()
                meta["coalesced_count"] = pending.count
                meta["first_seen_at"] = pending.first_seen_at.isoformat()
                meta["last_seen_at"] = pending.last_seen_at.isoformat()
                meta["coalesced"] = True

                new_event = Event(
                    event_id=event_id,
                    source_id=pending.source_id,
                    event_type=pending.event_type,
                    entity_id=pending.entity_id,
                    data=pending.data,
                    meta=meta,
                    occurred_at=pending.last_seen_at
                )
                session.add(new_event)
                session.delete(pending)
                promoted_count += 1
            
            if promoted_count > 0:
                session.commit()
                self.services.notifier.notify()
                logger.info(f"Promoted {promoted_count} coalesced events to main event table")
