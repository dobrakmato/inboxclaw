import asyncio
import logging
from src.database import delete_old_events
from src.services import AppServices

logger = logging.getLogger("ingest-pipeline.cleanup")

async def cleanup_task(services: AppServices):
    """
    Background task that periodically removes old events from the database.
    Runs immediately at startup and then every hour.
    """
    retention_days = services.config.database.retention_days
    logger.info(f"Cleanup task started with retention period of {retention_days} days.")
    
    while True:
        try:
            logger.info("Running database cleanup...")
            delete_old_events(services.db_session_maker, retention_days)
        except Exception as e:
            logger.error(f"Error during database cleanup: {e}")
        
        # Wait for 1 hour before next run
        # Using 3600 seconds
        await asyncio.sleep(3600)
