"""Read-only API endpoints for inspecting events, sources, and sinks."""

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select, func, desc
from sqlalchemy.orm import joinedload

from src.database import Event, PendingEvent, Source, Sink
from src.schemas import (
    EventResponse,
    EventListResponse,
    PendingEventResponse,
    PendingEventListResponse,
    SourceResponse,
    SinkResponse,
)

logger = logging.getLogger("inboxclaw.api")

router = APIRouter(prefix="/api", tags=["read-only"])


def _get_services(request: Request):
    return request.app.state.services


def _event_to_dict(event: Event) -> dict:
    return dict(
        id=event.id,
        event_id=event.event_id,
        event_type=event.event_type,
        entity_id=event.entity_id,
        source_id=event.source_id,
        source_name=event.source.name if event.source else None,
        created_at=event.created_at,
        occurred_at=event.occurred_at,
        data=event.data,
        meta=event.meta or {},
    )


def _pending_to_dict(p: PendingEvent) -> dict:
    return dict(
        id=p.id,
        source_id=p.source_id,
        event_type=p.event_type,
        entity_id=p.entity_id,
        data=p.data,
        meta=p.meta or {},
        count=p.count,
        first_seen_at=p.first_seen_at,
        last_seen_at=p.last_seen_at,
        flush_at=p.flush_at,
        strategy=p.strategy,
        window_seconds=p.window_seconds,
    )


@router.get("/events/recent", response_model=EventListResponse)
def get_recent_events(
    request: Request,
    limit: int = Query(20, ge=1, le=200, description="Number of recent events"),
):
    """Retrieve the most recent events, ordered by creation time descending."""
    services = _get_services(request)
    with services.db_session_maker() as session:
        query = (
            select(Event)
            .join(Event.source)
            .options(joinedload(Event.source))
            .order_by(desc(Event.created_at))
            .limit(limit)
        )
        events = session.execute(query).scalars().unique().all()

        count_query = select(func.count()).select_from(Event)
        total = session.execute(count_query).scalar() or 0

        result = [_event_to_dict(e) for e in events]

    return EventListResponse(
        events=[EventResponse(**r) for r in result],
        total=total,
    )


@router.get("/events/{event_db_id}", response_model=EventResponse)
def get_event_by_id(event_db_id: int, request: Request):
    """Retrieve a single event by its database ID."""
    services = _get_services(request)
    with services.db_session_maker() as session:
        event = session.execute(
            select(Event).options(joinedload(Event.source)).where(Event.id == event_db_id)
        ).scalar()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        result = _event_to_dict(event)

    return EventResponse(**result)


@router.get("/events", response_model=EventListResponse)
def get_events(
    request: Request,
    ids: Optional[str] = Query(None, description="Comma-separated list of event database IDs"),
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    source_name: Optional[str] = Query(None, description="Filter by source name"),
    limit: int = Query(50, ge=1, le=500, description="Maximum number of events to return"),
    offset: int = Query(0, ge=0, description="Number of events to skip"),
):
    """
    Retrieve multiple events with optional filters.

    Use the ``ids`` parameter to fetch specific events by their database IDs
    (comma-separated).  Otherwise events are returned in reverse chronological
    order with optional ``event_type`` and ``source_name`` filters.
    """
    services = _get_services(request)
    with services.db_session_maker() as session:
        query = select(Event).join(Event.source).options(joinedload(Event.source))

        if ids:
            id_list = [int(i.strip()) for i in ids.split(",") if i.strip().isdigit()]
            if not id_list:
                return EventListResponse(events=[], total=0)
            query = query.where(Event.id.in_(id_list))

        if event_type:
            query = query.where(Event.event_type == event_type)

        if source_name:
            query = query.where(Source.name == source_name)

        # Count total matching
        count_query = select(func.count()).select_from(query.subquery())
        total = session.execute(count_query).scalar() or 0

        # Apply ordering and pagination
        query = query.order_by(desc(Event.created_at)).offset(offset).limit(limit)
        events = session.execute(query).scalars().unique().all()

        result = [_event_to_dict(e) for e in events]

    return EventListResponse(
        events=[EventResponse(**r) for r in result],
        total=total,
    )


@router.get("/pending-events", response_model=PendingEventListResponse)
def get_pending_events(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Retrieve pending events that are waiting to be flushed (coalescing)."""
    services = _get_services(request)
    with services.db_session_maker() as session:
        count_query = select(func.count()).select_from(PendingEvent)
        total = session.execute(count_query).scalar() or 0

        query = (
            select(PendingEvent)
            .order_by(desc(PendingEvent.last_seen_at))
            .offset(offset)
            .limit(limit)
        )
        pending = session.execute(query).scalars().all()

        result = [_pending_to_dict(p) for p in pending]

    return PendingEventListResponse(
        events=[PendingEventResponse(**r) for r in result],
        total=total,
    )


@router.get("/sources", response_model=List[SourceResponse])
def list_sources(request: Request):
    """List all configured sources."""
    services = _get_services(request)
    with services.db_session_maker() as session:
        sources = session.execute(select(Source).order_by(Source.name)).scalars().all()
        result = [dict(id=s.id, name=s.name, type=s.type) for s in sources]

    return [SourceResponse(**r) for r in result]


@router.get("/sinks", response_model=List[SinkResponse])
def list_sinks(request: Request):
    """List all configured sinks."""
    services = _get_services(request)
    with services.db_session_maker() as session:
        sinks = session.execute(select(Sink).order_by(Sink.name)).scalars().all()
        result = [dict(id=s.id, name=s.name, type=s.type) for s in sinks]

    return [SinkResponse(**r) for r in result]
