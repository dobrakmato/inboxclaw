import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Protocol, Dict, Any, TYPE_CHECKING, Union
from sqlalchemy.orm import Session
from sqlalchemy import select

from src.database import PendingEvent
from src.schemas import NewEvent
from src.config import CoalesceRule, CoalesceStrategy

if TYPE_CHECKING:
    from src.services import AppServices

logger = logging.getLogger(__name__)

class TimingStrategy(Protocol):
    def calculate_flush_at(self, now: datetime, window_seconds: int, first_seen_at: datetime, last_seen_at: datetime) -> datetime:
        ...

class DebounceTiming:
    def calculate_flush_at(self, now: datetime, window_seconds: int, first_seen_at: datetime, last_seen_at: datetime) -> datetime:
        return now + timedelta(seconds=window_seconds)

class BatchTiming:
    def calculate_flush_at(self, now: datetime, window_seconds: int, first_seen_at: datetime, last_seen_at: datetime) -> datetime:
        return first_seen_at + timedelta(seconds=window_seconds)

class AggregationStrategy(Protocol):
    def aggregate(self, current_data: Dict[str, Any], new_data: Dict[str, Any]) -> Dict[str, Any]:
        ...

class LatestAggregation:
    def aggregate(self, current_data: Dict[str, Any], new_data: Dict[str, Any]) -> Dict[str, Any]:
        return new_data

class CoalescenceManager:
    """
    Handles the logic of merging new events into the pending_events table.
    """
    def __init__(self, services: "AppServices"):
        self.services = services
        self.timing_strategies: Dict[CoalesceStrategy, TimingStrategy] = {
            CoalesceStrategy.DEBOUNCE: DebounceTiming(),
            CoalesceStrategy.BATCH: BatchTiming(),
        }
        self.aggregation_strategies: Dict[str, AggregationStrategy] = {
            "latest": LatestAggregation(),
        }

    def handle_event(self, session: Session, source_id: int, event: NewEvent, rule: CoalesceRule) -> bool:
        """
        Processes an event that matches a coalescence rule.
        Returns True if handled.
        """
        if event.entity_id is None:
            logger.warning(f"Event {event.event_id} matches coalesce rule but has no entity_id. Skipping coalescence.")
            return False

        now = datetime.now(timezone.utc)
        
        # Check if a PendingEvent exists
        pending = session.scalar(
            select(PendingEvent).where(
                PendingEvent.source_id == source_id,
                PendingEvent.event_type == event.event_type,
                PendingEvent.entity_id == event.entity_id
            )
        )

        timing = self.timing_strategies.get(rule.strategy)
        aggregation = self.aggregation_strategies.get(rule.aggregation, LatestAggregation())

        if not timing:
             logger.error(f"Unsupported timing strategy: {rule.strategy}")
             return False

        if pending:
            # Update existing
            pending.data = aggregation.aggregate(pending.data, event.data)
            pending.count += 1
            pending.last_seen_at = now
            pending.flush_at = timing.calculate_flush_at(
                now, int(rule.window), pending.first_seen_at, pending.last_seen_at
            )
            # Ensure meta is updated if needed, or preserved
            if event.meta:
                pending.meta = {**pending.meta, **event.meta}
            
            logger.debug(f"Updated pending event for {event.event_type}:{event.entity_id} (count: {pending.count})")
        else:
            # Create new
            first_seen_at = now
            flush_at = timing.calculate_flush_at(now, int(rule.window), first_seen_at, now)
            
            pending = PendingEvent(
                source_id=source_id,
                event_type=event.event_type,
                entity_id=event.entity_id,
                data=event.data,
                meta=event.meta,
                count=1,
                first_seen_at=first_seen_at,
                last_seen_at=now,
                flush_at=flush_at,
                strategy=rule.strategy.value,
                window_seconds=int(rule.window)
            )
            session.add(pending)
            logger.debug(f"Created new pending event for {event.event_type}:{event.entity_id}, flushing at {flush_at}")

        return True
