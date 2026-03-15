from datetime import datetime, timezone, timedelta
from typing import Any, List, Union, Optional, Dict
from sqlalchemy import or_, and_, true, false, not_
from src.database import Event

class EventMatcher:
    """
    Common logic for matching event types based on patterns.
    Supported patterns:
      - '*' : matches everything
      - 'prefix.*' : matches any event type starting with 'prefix.'
      - 'exact_match' : matches only 'exact_match'
    """

    def __init__(self, patterns: Union[str, List[str]]):
        if isinstance(patterns, str):
            self.patterns = [patterns]
        elif patterns is None:
            self.patterns = ["*"]
        else:
            self.patterns = list(patterns)

    def matches(self, event_type: str) -> bool:
        """Checks if a single event_type matches any of the patterns (in-memory)."""
        for pattern in self.patterns:
            if pattern == "*":
                return True
            if pattern.endswith(".*"):
                prefix = pattern[:-1]  # "user.*" -> "user."
                if event_type.startswith(prefix):
                    return True
            if event_type == pattern:
                return True
        return False

    def build_sqlalchemy_clause(self, selector: Optional[str] = None) -> Any:
        """
        Builds an SQLAlchemy OR clause for filtering events by type.
        If a selector is provided, it must match BOTH the selector AND the matcher's patterns.
        """
        # 1. Build clause for internal patterns
        matcher_clause = self._patterns_to_clause(self.patterns)

        # 2. If no selector, just return matcher_clause
        if not selector:
            return matcher_clause

        # 3. Build clause for selector
        selector_clause = self._patterns_to_clause([selector])

        # 4. Combine: (match selector) AND (match internal patterns)
        if matcher_clause is true():
            return selector_clause
        if matcher_clause is false():
            return false()
        
        return and_(selector_clause, matcher_clause)

    def _patterns_to_clause(self, patterns: List[str]) -> Any:
        """Converts a list of patterns to a single SQLAlchemy clause."""
        clauses = []
        for pattern in patterns:
            if pattern == "*":
                return true()
            if pattern.endswith(".*"):
                prefix = pattern[:-1]
                clauses.append(Event.event_type.startswith(prefix))
            else:
                clauses.append(Event.event_type == pattern)
        
        if not clauses:
            return false()
        if len(clauses) == 1:
            return clauses[0]
        return or_(*clauses)

    @staticmethod
    def build_ttl_clause(
        ttl_enabled: bool,
        default_ttl: float,
        event_ttl: Dict[str, float]
    ) -> Any:
        """
        Builds an SQLAlchemy clause for TTL filtering.
        """
        if not ttl_enabled:
            return true()

        now = datetime.now(timezone.utc)
        ttl_clauses = []

        exact_rules = []
        prefix_rules = []
        wildcard_rules = []
        for pattern, ttl in event_ttl.items():
            if pattern == "*":
                wildcard_rules.append((pattern, ttl))
            elif pattern.endswith(".*"):
                prefix_rules.append((pattern, ttl))
            else:
                exact_rules.append((pattern, ttl))

        matched_exact_clauses = []
        for pattern, ttl in exact_rules:
            pattern_clause = EventMatcher._pattern_to_clause(pattern)
            ttl_clauses.append(and_(pattern_clause, Event.created_at > now - timedelta(seconds=ttl)))
            matched_exact_clauses.append(pattern_clause)

        matched_prefix_clauses = []
        for pattern, ttl in sorted(prefix_rules, key=lambda item: len(item[0]), reverse=True):
            pattern_clause = EventMatcher._pattern_to_clause(pattern)
            exclusions = []
            if matched_exact_clauses:
                exclusions.append(or_(*matched_exact_clauses))
            if matched_prefix_clauses:
                exclusions.append(or_(*matched_prefix_clauses))

            if exclusions:
                pattern_clause = and_(pattern_clause, not_(or_(*exclusions)))

            ttl_clauses.append(and_(pattern_clause, Event.created_at > now - timedelta(seconds=ttl)))
            matched_prefix_clauses.append(EventMatcher._pattern_to_clause(pattern))

        matched_specific_clauses = matched_exact_clauses + matched_prefix_clauses
        if wildcard_rules:
            wildcard_ttl = wildcard_rules[0][1]
            wildcard_clause = true()
            if matched_specific_clauses:
                wildcard_clause = not_(or_(*matched_specific_clauses))
            ttl_clauses.append(and_(wildcard_clause, Event.created_at > now - timedelta(seconds=wildcard_ttl)))
        elif matched_specific_clauses:
            ttl_clauses.append(
                and_(
                    not_(or_(*matched_specific_clauses)),
                    Event.created_at > now - timedelta(seconds=default_ttl),
                )
            )
        else:
            ttl_clauses.append(Event.created_at > now - timedelta(seconds=default_ttl))

        return or_(*ttl_clauses)

    @staticmethod
    def _pattern_to_clause(pattern: str) -> Any:
        if pattern == "*":
            return true()
        if pattern.endswith(".*"):
            return Event.event_type.startswith(pattern[:-1])
        return Event.event_type == pattern
