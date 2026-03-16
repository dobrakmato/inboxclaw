# Sinks

Sinks deliver events from the pipeline to your applications. Each sink watches for events that match its configured patterns and delivers them using a specific transport (HTTP push, pull, streaming, or desktop notifications).

If you want to get data *out* of the pipeline, you configure a sink. If you want to get data *in*, see [Sources](sources-general.md).

For detailed information on common configuration options, human-readable intervals, and environment variable expansion, see the [Configuration Guide](configuration.md).

## Getting Started

Add a sink to the `sink` section of your `config.yaml`. Each sink needs a unique name (the YAML key) and a `type`. If the name matches the type, you can omit `type`.

```yaml
sink:
  my_webhook:
    type: webhook
    url: "https://api.example.com/events"
```

## Delivery Models

| Sink                                    | Type         | How it works                                                                 |
|:----------------------------------------|:-------------|:-----------------------------------------------------------------------------|
| [Webhook](sink-webhook.md)              | `webhook`    | Pushes each event to a URL via HTTP POST. Retries on failure.                |
| [HTTP Pull](sink-http-pull.md)          | `http_pull`  | Your app polls for batches of events and confirms receipt.                   |
| [SSE](sink-sse.md)                      | `sse`        | Streams events in real-time over a persistent HTTP connection.               |
| [Win11 Toast](sink-win11toast.md)       | `win11toast` | Shows a Windows 11 desktop notification per event. For debugging only.       |

## Event Matching

Every sink has a `match` parameter that controls which event types it receives. The matching supports three patterns:

- `"*"` — matches all events (default).
- `"prefix.*"` — matches any event type starting with `prefix.` (e.g. `gmail.*` matches `gmail.message_received`).
- `"exact.type"` — matches only that exact event type.

You can provide a single pattern or a list:

```yaml
sink:
  alerts:
    type: webhook
    url: "https://example.com/alerts"
    match:
      - "gmail.message_received"
      - "fio.transaction.*"
```

## Event Envelope

All sinks deliver events using the same JSON structure:

```json
{
  "id": 42,
  "event_id": "evt_12345",
  "event_type": "gmail.message_received",
  "entity_id": "msg_99",
  "created_at": "2024-03-13T23:52:00+00:00",
  "data": {
    "subject": "Hello",
    "from": "alice@example.com"
  },
  "meta": {}
}
```

| Field        | Description                                                        |
|:-------------|:-------------------------------------------------------------------|
| `id`         | Internal database ID of the event.                                 |
| `event_id`   | Unique identifier assigned by the source.                          |
| `event_type` | Dot-separated event type string.                                   |
| `entity_id`  | Identifier of the object the event is about.                       |
| `created_at` | ISO 8601 timestamp of when the event was stored in the pipeline.   |
| `data`       | Source-specific payload (see individual source docs).              |
| `meta`       | Additional metadata (usually empty).                               |

## Coalescing

Some sinks (HTTP Pull, SSE) support **coalescing**. When enabled for specific event types, multiple events with the same `event_type` and `entity_id` are merged into a single event containing only the latest state. This reduces noise when a source produces many rapid updates for the same object.

Events are coalesced at sink processing time.
- HTTP Pull: When a request is made, all selected events from the database are considered for coalescing
- SSE: When the SSE component receives notification about new events in the database

```yaml
sink:
  ui_updates:
    type: sse
    coalesce:
      - "google.drive.file_updated"
```

## TTL (Time-To-Live)

The Webhook and HTTP Pull sinks support TTL. When enabled, events older than their TTL are skipped during delivery. This prevents stale events from being delivered after a long downtime.

- `ttl_enabled`: Whether TTL filtering is active (default: `true` for webhook and http_pull).
- `default_ttl`: Fallback TTL for events without a specific rule (default: `"1h"`).
- `event_ttl`: Per-type TTL overrides using the same matching patterns (`"exact.type"`, `"prefix.*"`, `"*"`).

```yaml
sink:
  my_webhook:
    type: webhook
    url: "https://example.com/events"
    ttl_enabled: true
    default_ttl: "2h"
    event_ttl:
      "critical.*": "7d"
      "stats.update": "15m"
```

TTL is resolved in order: exact match → longest prefix match → `default_ttl`.
