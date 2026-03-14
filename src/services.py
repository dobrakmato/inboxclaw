import asyncio
from dataclasses import dataclass, field
from typing import List, Dict, Any
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker
from src.config import Config
from src.pipeline.notifier import EventNotifier
from src.pipeline.writer import EventWriter
from src.pipeline.cursor import SourceCursor

@dataclass
class AppServices:
    """Service container for the ingest pipeline application."""
    app: FastAPI
    config: Config
    db_session_maker: sessionmaker
    notifier: EventNotifier
    writer: EventWriter = field(init=False)
    cursor: SourceCursor = field(init=False)
    background_tasks: List[asyncio.Task] = field(default_factory=list)
    sources: Dict[str, Any] = field(default_factory=dict)
    sinks: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.writer = EventWriter(self)
        self.cursor = SourceCursor(self)

    def add_task(self, coro) -> asyncio.Task:
        """Create and track a background task."""
        task = asyncio.create_task(coro)
        self.background_tasks.append(task)
        # Clean up finished tasks from the list to avoid memory leaks
        task.add_done_callback(lambda t: self.background_tasks.remove(t) if t in self.background_tasks else None)
        return task

    async def stop_tasks(self):
        """Cancel all background tasks and wait for them to finish."""
        if not self.background_tasks:
            return
            
        for task in self.background_tasks[:]:
            task.cancel()
        
        await asyncio.gather(*self.background_tasks, return_exceptions=True)
        self.background_tasks.clear()
