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
        """Notify all listeners that new events are available.
        
        Thread-safe: if called from a thread other than the event loop thread,
        uses call_soon_threadsafe to schedule the set() calls.
        """
        logger.debug(f"Notifying {len(self.listeners)} listeners")
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is not None and not _is_loop_thread(running_loop):
            # Called from a worker thread — schedule on the loop thread
            for event in self.listeners:
                running_loop.call_soon_threadsafe(event.set)
        else:
            # Called from the loop thread (or no loop) — set directly
            for event in self.listeners:
                event.set()


def _is_loop_thread(loop: asyncio.AbstractEventLoop) -> bool:
    """Check if the current thread is the thread running the given event loop."""
    import threading
    # CPython stores the thread id on the loop; fall back to True if unavailable
    loop_tid = getattr(loop, '_thread_id', None)
    if loop_tid is None:
        return True
    return threading.current_thread().ident == loop_tid
