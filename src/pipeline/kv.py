import logging
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING, Any
from sqlalchemy import select, delete as sqa_delete
from src.database import SourceKV

if TYPE_CHECKING:
    from src.services import AppServices

logger = logging.getLogger(__name__)

class SourceKVService:
    """
    Simple K/V cache for every source.
    """
    def __init__(self, services: "AppServices"):
        self.services = services

    def get(self, source_id: int, key: str) -> Optional[Any]:
        """
        Get the value for the given key and source_id.
        """
        with self.services.db_session_maker() as session:
            kv = session.scalar(
                select(SourceKV).where(
                    SourceKV.source_id == source_id,
                    SourceKV.key == key
                )
            )
            if kv:
                return kv.value
            return None

    def set(self, source_id: int, key: str, value: Any):
        """
        Set the value for the given key and source_id.
        """
        with self.services.db_session_maker() as session:
            # Upsert logic
            kv = session.scalar(
                select(SourceKV).where(
                    SourceKV.source_id == source_id,
                    SourceKV.key == key
                )
            )
            if kv:
                kv.value = value
                kv.updated_at = datetime.now(timezone.utc)
            else:
                kv = SourceKV(source_id=source_id, key=key, value=value)
                session.add(kv)
            session.commit()
            logger.debug(f"Set KV for source {source_id}: {key}={value}")

    def delete(self, source_id: int, key: str):
        """
        Delete the value for the given key and source_id.
        """
        with self.services.db_session_maker() as session:
            session.execute(
                sqa_delete(SourceKV).where(
                    SourceKV.source_id == source_id,
                    SourceKV.key == key
                )
            )
            session.commit()
            logger.debug(f"Deleted KV for source {source_id}: {key}")

    def delete_all(self, source_id: int):
        """
        Delete all values for the given source_id.
        """
        with self.services.db_session_maker() as session:
            session.execute(
                sqa_delete(SourceKV).where(
                    SourceKV.source_id == source_id
                )
            )
            session.commit()
            logger.debug(f"Deleted all KV for source {source_id}")

    def delete_older_than(self, source_id: int, cutoff: datetime):
        """
        Delete values for the given source_id that were created before the cutoff.
        """
        with self.services.db_session_maker() as session:
            session.execute(
                sqa_delete(SourceKV).where(
                    SourceKV.source_id == source_id,
                    SourceKV.created_at < cutoff
                )
            )
            session.commit()
            logger.debug(f"Deleted old KV for source {source_id} older than {cutoff}")

    def delete_older_than_with_prefix(self, source_id: int, cutoff: datetime, prefix: str):
        """
        Delete values for the given source_id and key prefix that were created before the cutoff.
        """
        with self.services.db_session_maker() as session:
            session.execute(
                sqa_delete(SourceKV).where(
                    SourceKV.source_id == source_id,
                    SourceKV.key.like(f"{prefix}%"),
                    SourceKV.created_at < cutoff
                )
            )
            session.commit()
            logger.debug(f"Deleted old KV for source {source_id} with prefix {prefix} older than {cutoff}")

    def delete_expired_with_prefix(self, source_id: int, cutoff: datetime, prefix: str):
        """
        Delete values for the given source_id and key prefix that were updated before the cutoff.
        """
        with self.services.db_session_maker() as session:
            session.execute(
                sqa_delete(SourceKV).where(
                    SourceKV.source_id == source_id,
                    SourceKV.key.like(f"{prefix}%"),
                    SourceKV.updated_at < cutoff
                )
            )
            session.commit()
            logger.debug(f"Deleted expired KV for source {source_id} with prefix {prefix} older than {cutoff}")

    def list_keys_with_prefix(self, source_id: int, prefix: str) -> list[str]:
        """
        List all keys for the given source_id that start with the prefix.
        """
        with self.services.db_session_maker() as session:
            kv_list = session.scalars(
                select(SourceKV.key).where(
                    SourceKV.source_id == source_id,
                    SourceKV.key.like(f"{prefix}%")
                )
            ).all()
            return list(kv_list)
