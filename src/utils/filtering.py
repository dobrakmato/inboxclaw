import re
import html
import logging
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

class FilterItem(Protocol):
    @property
    def contains(self) -> Optional[str]: ...
    @property
    def regex(self) -> Optional[str]: ...

def normalize_text(text: str) -> str:
    """Unescape HTML, strip whitespace, and normalize multiple spaces/newlines."""
    if not text:
        return ""
    
    # Unescape HTML entities
    text = html.unescape(text)
    
    # Replace all whitespace (including newlines, tabs, and multiple spaces) with a single space
    text = re.sub(r'\s+', ' ', text)
    
    return text.strip()

def matches_filter(value: str, filter_item: FilterItem, filter_name: str = "unnamed") -> bool:
    """Check if a normalized value matches the given filter item."""
    if not value:
        return False
        
    normalized_value = normalize_text(value)
    
    # Contains check (case-insensitive)
    if filter_item.contains:
        if filter_item.contains.lower() in normalized_value.lower():
            logger.info(f"Matched filter '{filter_name}' (contains: '{filter_item.contains}')")
            return True
            
    # Regex check
    if filter_item.regex:
        if re.search(filter_item.regex, normalized_value, re.IGNORECASE if "(?i)" not in filter_item.regex else 0):
            logger.info(f"Matched filter '{filter_name}' (regex: '{filter_item.regex}')")
            return True
            
    return False
