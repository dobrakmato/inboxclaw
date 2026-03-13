from dataclasses import dataclass
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker
from src.config import Config
from src.pipeline.notifier import EventNotifier

@dataclass
class AppServices:
    """Service container for the ingest pipeline application."""
    app: FastAPI
    config: Config
    db_session_maker: sessionmaker
    notifier: EventNotifier
