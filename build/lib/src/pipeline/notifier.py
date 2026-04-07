import asyncio
import logging
from typing import Set

logger = logging.getLogger(__name__)

class EventNotifier:
    """A simple in-memory pub/sub to notify sinks about new events."""
    def __init__(self):
        self.listeners: Set[asyncio.Event] = set()

    def subscribe(self) -> asyncio.Event:
        event = asyncio.Event()
        self.listeners.add(event)
        return event

    def unsubscribe(self, event: asyncio.Event):
        self.listeners.discard(event)

    def notify(self):
        """Notify all listeners that new events are available."""
        logger.debug(f"Notifying {len(self.listeners)} listeners")
        for event in self.listeners:
            event.set()
