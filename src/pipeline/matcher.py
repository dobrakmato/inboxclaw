from typing import Any, List, Union, Optional
from sqlalchemy import or_, and_, true, false
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
