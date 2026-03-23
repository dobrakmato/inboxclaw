import re
import json
import shlex
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

def transform_template(template: Any, context: dict, shell_quote: bool = False) -> Any:
    """
    Recursively transform a template (dict, list, or string) using the provided context.
    
    Supports:
    - #root.path.to.field: Interpolate field as string.
    - $root.path.to.field: Interpolate field as JSON string.
    - If a string starts with # or $ and has no spaces, it's treated as a direct path resolution.
    
    If shell_quote is True, the interpolated values are escaped for safe shell use.
    """
    if isinstance(template, dict):
        return {k: transform_template(v, context, shell_quote) for k, v in template.items()}
    elif isinstance(template, list):
        return [transform_template(i, context, shell_quote) for i in template]
    elif isinstance(template, str):
        # Special case: if the whole string is a path (supports non-string returns)
        if template.startswith("#") and " " not in template:
            val = resolve_path(template[1:], context)
            if shell_quote and val is not None:
                return shlex.quote(str(val))
            return val
        if template.startswith("$") and " " not in template:
            val = resolve_path(template[1:], context)
            json_val = json.dumps(val, ensure_ascii=False)
            if shell_quote:
                return shlex.quote(json_val)
            return json_val

        # String interpolation
        def replace_match(match):
            prefix = match.group(1)
            path = match.group(2)
            val = resolve_path(path, context)
            
            if prefix == "#":
                res = str(val) if val is not None else ""
            else:  # prefix == "$"
                # Use separators=(',', ':') for compact JSON without extra spaces
                res = json.dumps(val, separators=(',', ':'), ensure_ascii=False) if val is not None else "null"
            
            if shell_quote:
                return shlex.quote(res)
            return res

        # Regex to find #path.to.field or $path.to.field
        # We assume paths are alphanumeric with dots, starting with root
        # This matches #root.something or $root.something
        pattern = r"([#\$])(root(?:\.[a-zA-Z0-9_]+)*)"
        return re.sub(pattern, replace_match, template)

    return template
