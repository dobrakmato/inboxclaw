from typing import List, Dict, Tuple
from src.database import Event
from src.pipeline.matcher import EventMatcher

class Coalescer:
    """Coalesces events of the same type and same entity_id."""
    
    def __init__(self, match_patterns: List[str] = None):
        self.matcher = EventMatcher(match_patterns or ["*"])

    @property
    def match_patterns(self) -> List[str]:
        return self.matcher.patterns

    def coalesce(self, events: List[Event]) -> List[Event]:
        """
        Groups events by (event_type, entity_id).
        Only events matching the matcher patterns are coalesced.
        """
        if not events:
            return []

        to_coalesce: List[Event] = []
        others: List[Event] = []
        
        for event in events:
            if self.matcher.matches(event.event_type):
                to_coalesce.append(event)
            else:
                others.append(event)

        if not to_coalesce:
            return others

        grouped: Dict[Tuple[str, str], List[Event]] = {}
        for event in to_coalesce:
            key = (event.event_type, event.entity_id)
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(event)

        coalesced_events: List[Event] = []
        for ev_list in grouped.values():
            if len(ev_list) > 1:
                # Merge logic - take the latest one
                latest_ev = max(ev_list, key=lambda e: e.created_at)
                coalesced_events.append(latest_ev)
            else:
                coalesced_events.extend(ev_list)

        return others + coalesced_events
