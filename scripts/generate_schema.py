"""
Generates config.schema.json from the Pydantic Config model.
Run this script whenever the config model changes, or let the pre-commit hook do it automatically.

Post-processing applied to the raw Pydantic schema:
- Interval fields (poll_interval, retry_interval, etc.) are widened to accept both
  numbers and human-readable strings like "1m", "6h", "30s" — matching runtime behaviour.
- The `type` discriminator field is made optional in all source/sink config definitions,
  because load_config() injects it automatically from the YAML key name at runtime,
  so `faktury_online: {}` is valid even though the field has no default in the model.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import Config, Interval

INTERVAL_SCHEMA = {"oneOf": [{"type": "number"}, {"type": "string"}]}

# Names of fields whose runtime type is Interval (number after BeforeValidator).
# We detect them by inspecting the model fields directly.
def _collect_interval_field_names() -> set[str]:
    from src.config import (
        GoogleSourceConfig, GmailSourceConfig, GoogleDriveSourceConfig,
        GoogleCalendarSourceConfig, FakturyOnlineSourceConfig, FioSourceConfig,
        MockSourceConfig, NordigenSourceConfig, WebhookSinkConfig,
        HttpPullSinkConfig, SSESinkConfig, TTLConfig,
    )
    names: set[str] = set()
    for model in [
        GoogleSourceConfig, GmailSourceConfig, GoogleDriveSourceConfig,
        GoogleCalendarSourceConfig, FakturyOnlineSourceConfig, FioSourceConfig,
        MockSourceConfig, NordigenSourceConfig, WebhookSinkConfig,
        HttpPullSinkConfig, SSESinkConfig, TTLConfig,
    ]:
        for field_name, field_info in model.model_fields.items():
            # Interval fields have annotation float (after BeforeValidator strips the wrapper)
            # but their metadata contains BeforeValidator — easiest to check the default type
            ann = field_info.annotation
            if ann is float:
                names.add(field_name)
    return names


def _fix_interval_fields(schema: dict, interval_field_names: set[str]) -> None:
    """Recursively widen Interval fields from {type: number} to oneOf[number, string]."""
    if isinstance(schema, dict):
        props = schema.get("properties", {})
        for field_name, field_schema in props.items():
            if field_name in interval_field_names:
                # Replace in-place; handle both direct schema and $ref-wrapped schemas
                if field_schema.get("type") == "number":
                    props[field_name] = INTERVAL_SCHEMA
                elif "anyOf" in field_schema or "oneOf" in field_schema:
                    # Already a union — ensure string is included
                    key = "anyOf" if "anyOf" in field_schema else "oneOf"
                    types = field_schema[key]
                    if not any(t.get("type") == "string" for t in types):
                        types.append({"type": "string"})
        for value in schema.values():
            if isinstance(value, dict):
                _fix_interval_fields(value, interval_field_names)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        _fix_interval_fields(item, interval_field_names)


def _make_type_optional(schema: dict) -> None:
    """
    Remove 'type' from the required list of every $defs entry that has it,
    because load_config() injects the type discriminator from the YAML key at runtime.
    Also, for models that have a 'type' property with a 'const' value,
    make that property optional so that empty objects {} are valid.
    """
    for def_schema in schema.get("$defs", {}).values():
        required = def_schema.get("required", [])
        if "type" in required:
            required.remove("type")
            if not required:
                def_schema.pop("required", None)
        
        # If 'type' has a 'const', it means it's a fixed value for this model.
        # We can make it optional in the schema because it's injected at runtime.
        # This helps avoid "Validates to more than one variant" if the IDE
        # doesn't use the discriminator property name for selection when it's missing.
        props = def_schema.get("properties", {})
        if "type" in props and "const" in props["type"]:
            # We don't remove it, but by not being in 'required' it's already optional.
            # However, some IDEs might still struggle if multiple variants match {}.
            pass

def _remove_discriminators(schema: dict) -> None:
    """
    Remove 'discriminator' from 'sources' and 'sink' additionalProperties.
    When the user doesn't provide 'type' in YAML, the IDE's YAML language server
    often fails to use the discriminator correctly or complains about multiple matches.
    By removing it and relying on 'oneOf' with 'additionalProperties: false' (which Pydantic
    already provides via ConfigDict(extra="forbid")), the IDE can often disambiguate 
    better based on other unique required fields, or at least show all options.
    
    Additionally, we set a high-priority 'type' constraint for each variant in 'oneOf'
    if possible, but since we already made 'type' optional, we rely on 'const'.
    """
    for field in ["sources", "sink"]:
        if field in schema.get("properties", {}):
            add_props = schema["properties"][field].get("additionalProperties", {})
            if "discriminator" in add_props:
                add_props.pop("discriminator")
            
            # To fix "Validates to more than one variant" for {} or minimal objects,
            # we can add a 'default' or hint, but the most robust way is to ensure
            # that variants that CAN match {} are not ambiguous.
            # However, in this project, load_config() defines which variant it is
            # based on the KEY. The JSON schema doesn't know about this runtime injection.
            # 
            # If we have multiple models that allow {}, the IDE won't know which one to pick
            # without the 'type' field. By removing 'discriminator', we at least let the IDE
            # try to match other properties.


def _add_key_named_variant_properties(schema: dict) -> None:
    """
    Add explicit property schemas for keys equal to variant discriminator values.

    This enables configs like:
      sources:
        faktury_online: {}
      sink:
        sse:
          match: '*'

    without requiring an explicit `type`, while still keeping additionalProperties
    for custom key names (e.g. `fio_apsi`, `all`, ...).
    """
    field_to_variant_defs = {
        "sources": [
            "GmailSourceConfig",
            "GoogleDriveSourceConfig",
            "GoogleCalendarSourceConfig",
            "FakturyOnlineSourceConfig",
            "FioSourceConfig",
            "MockSourceConfig",
            "HomeAssistantSourceConfig",
            "NordigenSourceConfig",
        ],
        "sink": [
            "WebhookSinkConfig",
            "HttpPullSinkConfig",
            "SSESinkConfig",
            "Win11ToastSinkConfig",
        ],
    }

    defs = schema.get("$defs", {})
    root_props = schema.get("properties", {})

    for field_name, variant_defs in field_to_variant_defs.items():
        field_schema = root_props.get(field_name)
        if not isinstance(field_schema, dict):
            continue

        explicit_props = field_schema.setdefault("properties", {})
        for def_name in variant_defs:
            def_schema = defs.get(def_name, {})
            type_prop = def_schema.get("properties", {}).get("type", {})
            type_value = type_prop.get("const")
            if isinstance(type_value, str):
                explicit_props[type_value] = {"$ref": f"#/$defs/{def_name}"}


def build_schema() -> dict:
    schema = Config.model_json_schema()
    interval_fields = _collect_interval_field_names()
    _fix_interval_fields(schema, interval_fields)
    _make_type_optional(schema)
    _remove_discriminators(schema)
    _add_key_named_variant_properties(schema)
    return schema


if __name__ == "__main__":
    output = Path(__file__).parent.parent / "config.schema.json"
    schema = build_schema()
    output.write_text(json.dumps(schema, indent=2))
    print(f"Schema written to {output}")
