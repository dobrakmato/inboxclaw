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

    def coalesce(self, events: List[Event]) -> Tuple[List[Event], Dict[int, List[int]]]:
        """
        Groups events by (event_type, entity_id).
        Only events matching the matcher patterns are coalesced.
        Returns a tuple of (coalesced_events, source_ids_map).
        source_ids_map: {coalesced_event_id: [original_event_ids]}
        """
        if not events:
            return [], {}

        to_coalesce: List[Event] = []
        others: List[Event] = []
        
        for event in events:
            if self.matcher.matches(event.event_type):
                to_coalesce.append(event)
            else:
                others.append(event)

        source_ids_map: Dict[int, List[int]] = {}
        for ev in others:
            source_ids_map[ev.id] = [ev.id]

        if not to_coalesce:
            return others, source_ids_map

        grouped: Dict[Tuple[str, str], List[Event]] = {}
        for event in to_coalesce:
            key = (event.event_type, event.entity_id)
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(event)

        coalesced_events: List[Event] = []
        for ev_list in grouped.values():
            if len(ev_list) > 1:
                # Sequence by created_at to ensure we find the latest one correctly
                # and have the first and last timestamps.
                sorted_evs = sorted(ev_list, key=lambda e: e.created_at)
                
                # Merge logic - take the latest one
                latest_ev = sorted_evs[-1]
                
                # Update meta
                if latest_ev.meta is None:
                    latest_ev.meta = {}
                
                latest_ev.meta["coalesced_events"] = len(ev_list)
                latest_ev.meta["first_event_at"] = sorted_evs[0].created_at.isoformat()
                latest_ev.meta["last_event_at"] = sorted_evs[-1].created_at.isoformat()
                
                coalesced_events.append(latest_ev)
                source_ids_map[latest_ev.id] = [ev.id for ev in ev_list]
            else:
                coalesced_events.extend(ev_list)
                for ev in ev_list:
                    source_ids_map[ev.id] = [ev.id]

        return others + coalesced_events, source_ids_map
