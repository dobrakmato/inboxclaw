import asyncio
import random
import uuid
import logging
from typing import Any, Dict, Union
from datetime import datetime, timezone
from pytimeparse import parse as parse_time
from src.services import AppServices
from src.database import Event

logger = logging.getLogger(__name__)

def parse_interval(interval: Union[int, float, str]) -> float:
    """Parse a numeric or human-readable interval into seconds."""
    if isinstance(interval, (int, float)):
        return float(interval)
    
    parsed = parse_time(interval)
    if parsed is None:
        try:
            return float(interval)
        except (ValueError, TypeError):
            logger.warning(f"Failed to parse interval '{interval}', defaulting to 10s")
            return 10.0
    return float(parsed)

class MockSource:
    def __init__(self, name: str, config: Dict[str, Any], services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.interval = parse_interval(config.get("interval", 10))
        self.task: asyncio.Task | None = None

    async def start(self):
        """Start generating mock events."""
        logger.info(f"Starting MockSource '{self.name}' with interval {self.interval}s")
        self.task = self.services.add_task(self._run())

    async def _run(self):
        while True:
            try:
                await asyncio.sleep(self.interval)
                self._generate_event()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in MockSource '{self.name}': {e}", exc_info=True)
                await asyncio.sleep(5)  # Wait before retrying

    def _generate_event(self):
        random_value = random.randint(1, 100)
        event_id = str(uuid.uuid4())
        
        logger.info(f"MockSource '{self.name}' generating event {event_id} with value {random_value}")
        
        with self.services.db_session_maker() as session:
            event = Event(
                event_id=event_id,
                source_id=self.source_id,
                event_type="mock.random_number",
                entity_id=f"mock-{self.name}",
                data={"number": random_value},
                occurred_at=datetime.now(timezone.utc)
            )
            session.add(event)
            session.commit()
        
        self.services.notifier.notify()

    def stop(self):
        """Stop generating events."""
        if self.task:
            self.task.cancel()
            logger.info(f"MockSource '{self.name}' stopped.")
