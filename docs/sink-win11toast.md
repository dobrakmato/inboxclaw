# Win11 Toast Sink

The Win11 Toast sink shows a Windows 11 desktop notification for each matching event. It is a **debugging tool** — use it to quickly verify that events are flowing through the pipeline and that your matching rules work correctly.

This sink is local-only and best-effort. It is not a durable delivery mechanism and should not be used in production. For actual event delivery, use the [Webhook](sink-webhook.md), [HTTP Pull](sink-http-pull.md), or [SSE](sink-sse.md) sinks.

## Getting Started

Add a Win11 Toast sink to your `config.yaml`:

```yaml
sink:
  desktop_debug:
    type: win11toast
```

Each matching event will produce a Windows notification where the **title** is the `event_type` and the **body** is a best-effort summary of the `data` payload.

Requires Windows 11 and the `win11toast` Python package.

## Configuration

### Minimal Configuration

```yaml
sink:
  desktop_debug:
    type: win11toast
```

Defaults: `match: "*"`, `max_body_length: 220`.

### Full Configuration

```yaml
sink:
  calendar_alerts:
    type: win11toast
    match:
      - "google.calendar.*"
      - "gmail.message_received"
    max_body_length: 300
```

### Configuration Reference

| Parameter         | Type           | Default | Description                                                                     |
|:------------------|:---------------|:--------|:--------------------------------------------------------------------------------|
| `type`            | `string`       | —       | Must be `win11toast`.                                                           |
| `match`           | `string\|list` | `"*"`   | Event type filter. Supports `"*"`, `"prefix.*"`, and exact matches.             |
| `max_body_length` | `int`          | `220`   | Maximum notification body length before truncation.                             |

## How the Notification Body is Built

The sink tries to extract meaningful fields from the event `data` in this order:

1. Looks for well-known keys: `summary`, `title`, `subject`, `name`, `filename`, `message`, `description`, `snippet`, `status`, `action`.
2. Falls back to extracting scalar values from nested JSON.
3. Falls back to a truncated JSON snippet.

The body is prefixed with `entity={entity_id}` when available.
