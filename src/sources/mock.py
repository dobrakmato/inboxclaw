import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone

from src.config import MockSourceConfig
from src.schemas import NewEvent
from src.services import AppServices

logger = logging.getLogger(__name__)


class MockSource:
    def __init__(self, name: str, config: MockSourceConfig, services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        if isinstance(config, dict):
            from src.config import parse_interval
            self.interval = parse_interval(config.get("interval", 10))
        else:
            self.interval = config.interval
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
        
        self.services.writer.write_events(self.source_id, [NewEvent(
            event_id=event_id,
            event_type="mock.random_number",
            entity_id=f"mock-{self.name}",
            data={"number": random_value},
            occurred_at=datetime.now(timezone.utc)
        )])

    def stop(self):
        """Stop generating events."""
        if self.task:
            self.task.cancel()
            logger.info(f"MockSource '{self.name}' stopped.")
