import re
import json
from typing import Any, Dict, Optional

def resolve_path(path: str, context: dict) -> Any:
    """Resolve a dot-separated path in a dictionary context."""
    parts = path.split(".")
    current = context
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current

def transform_template(template: Any, context: dict) -> Any:
    """
    Recursively transform a template (dict, list, or string) using the provided context.
    
    Supports:
    - #root.path.to.field: Interpolate field as string.
    - $root.path.to.field: Interpolate field as JSON string.
    - If a string starts with # or $ and has no spaces, it's treated as a direct path resolution.
    """
    if isinstance(template, dict):
        return {k: transform_template(v, context) for k, v in template.items()}
    elif isinstance(template, list):
        return [transform_template(i, context) for i in template]
    elif isinstance(template, str):
        # Special case: if the whole string is a path (supports non-string returns)
        if template.startswith("#") and " " not in template:
            return resolve_path(template[1:], context)
        if template.startswith("$") and " " not in template:
            val = resolve_path(template[1:], context)
            return json.dumps(val)

        # String interpolation
        def replace_match(match):
            prefix = match.group(1)
            path = match.group(2)
            val = resolve_path(path, context)
            if prefix == "#":
                return str(val) if val is not None else ""
            else:  # prefix == "$"
                return json.dumps(val) if val is not None else "null"

        # Regex to find #path.to.field or $path.to.field
        # We assume paths are alphanumeric with dots, starting with root
        # This matches #root.something or $root.something
        pattern = r"([#\$])(root(?:\.[a-zA-Z0-9_]+)*)"
        return re.sub(pattern, replace_match, template)

    return template
