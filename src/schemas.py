from datetime import datetime
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, Union

class EventWithMeta(BaseModel):
    """
    Data Transfer Object representing an event with transient metadata.
    Used for communication between the pipeline, coalescer, and sinks.
    """
    id: Optional[int] = None
    event_id: str
    event_type: str
    entity_id: Optional[str] = None
    created_at: Union[datetime, str, None] = None
    data: Optional[Dict[str, Any]] = None
    meta: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_event(cls, event: Any, meta: Optional[Dict[str, Any]] = None) -> "EventWithMeta":
        return cls(
            id=getattr(event, "id", None),
            event_id=event.event_id,
            event_type=event.event_type,
            entity_id=event.entity_id,
            created_at=event.created_at,
            data=event.data,
            meta=meta or getattr(event, "meta", {})
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict with ISO format datetime."""
        res = self.model_dump()
        if isinstance(self.created_at, datetime):
            res["created_at"] = self.created_at.isoformat()
        return res

class NewEvent(BaseModel):
    """
    Data Transfer Object for a new event.
    """
    event_id: str
    event_type: str
    data: Dict[str, Any]
    entity_id: Optional[str] = None
    occurred_at: Optional[datetime] = None
