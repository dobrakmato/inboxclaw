import asyncio
import pytest
from datetime import datetime, timezone
from src.pipeline.notifier import EventNotifier
from src.database import Event

@pytest.mark.asyncio
async def test_event_notifier():
    notifier = EventNotifier()
    event = notifier.subscribe()
    
    # Check that it's not set initially
    assert not event.is_set()
    
    # Notify and check
    notifier.notify()
    assert event.is_set()
    
    # Reset and check
    event.clear()
    assert not event.is_set()
    
    # Unsubscribe
    notifier.unsubscribe(event)
    notifier.notify()
    # Since we unsubscribed, this specific event object won't be set by notifier.notify() 
    # if it's not in the listeners set.
    # Wait, in the current implementation, if we have the reference, we can check.
    assert not event.is_set()
