import logging
from typing import Optional, TYPE_CHECKING
from sqlalchemy import select, update
from src.database import Source

if TYPE_CHECKING:
    from src.services import AppServices

logger = logging.getLogger(__name__)

class SourceCursor:
    """
    Common logic for managing source cursors (watermarks).
    """
    def __init__(self, services: "AppServices"):
        self.services = services

    def get_last_cursor(self, source_id: int) -> Optional[str]:
        """
        Returns the current cursor value for the given source_id.
        """
        with self.services.db_session_maker() as session:
            source = session.scalar(select(Source).where(Source.id == source_id))
            if source:
                return source.cursor
            return None

    def set_cursor(self, source_id: int, value: str):
        """
        Sets the cursor to a new value for the given source_id.
        """
        with self.services.db_session_maker() as session:
            session.execute(
                update(Source).where(Source.id == source_id).values(cursor=value)
            )
            session.commit()
            logger.debug(f"Set cursor for source {source_id} to {value}")
