# HTTP Pull Sink

The HTTP Pull sink lets your application fetch events from the pipeline on its own schedule. Your app requests a batch of events, processes them, and then confirms receipt. Events stay available until confirmed (or until they expire under TTL).

This is a good fit when your app cannot receive incoming requests (e.g. behind a firewall), when you need backpressure control, or when you want to guarantee processing before events disappear. It is not ideal if you need sub-second latency — for that, use the [SSE sink](sink-sse.md) or [Webhook sink](sink-webhook.md).

## Getting Started

Add an HTTP Pull sink to your `config.yaml`:

```yaml
sink:
  http_pull:
    type: http_pull
```

This exposes two endpoints on the pipeline's HTTP server:

- `GET /http_pull/extract` — fetch a batch of unprocessed events.
- `POST /http_pull/mark-processed?batch_id={id}` — confirm a batch has been processed.

### Basic workflow

1. **Extract**: `GET /http_pull/extract` → returns a `batch_id` and a list of events.
2. **Process**: Your app processes the events (save to DB, trigger logic, etc.).
3. **Confirm**: `POST /http_pull/mark-processed?batch_id=42` → marks those events as done.

If your app crashes before confirming, the same events will appear again on the next extract (as long as they haven't expired under TTL).

## Core Concepts

### Batching

Each call to `/extract` returns events grouped into a **batch** identified by a `batch_id`. You confirm the entire batch at once. You can control how many events are returned per batch with the `batch_size` query parameter.

### Coalescing

When enabled, multiple events with the same `event_type` and `entity_id` are merged into a single event containing only the latest state. This is useful when a source produces many rapid updates for the same object. Coalescing is applied *after* fetching from the database and *before* returning results to you.

### TTL (Time-To-Live)

TTL is **enabled by default** with a `default_ttl` of `1h`. Events older than their TTL are excluded from `/extract` results, even if never confirmed. This prevents a backlog of stale events from building up.

To get classic queue behavior where unconfirmed events reappear indefinitely, disable TTL:

```yaml
sink:
  http_pull:
    ttl_enabled: false
```

TTL is resolved in order: exact match in `event_ttl` → longest prefix match in `event_ttl` → `default_ttl`.

### Multiple Sinks

You can run multiple HTTP Pull sinks simultaneously. Each sink tracks its own "processed" state independently. If `sink_a` and `sink_b` both match the same event, confirming it in `sink_a` has no effect on `sink_b`.

## Configuration

### Minimal Configuration

```yaml
sink:
  http_pull:
    type: http_pull
```

Endpoints: `GET /http_pull/extract`, `POST /http_pull/mark-processed`.
Defaults: `ttl_enabled: true`, `default_ttl: "1h"`, `match: "*"`.

### Full Configuration

```yaml
sink:
  data_warehouse:
    type: http_pull
    path:
      extract: "/fetch"
      mark_processed: "/acknowledge"
    match:
      - "sales.order.*"
      - "inventory.update"
    coalesce:
      - "inventory.update"
    ttl_enabled: true
    default_ttl: "2h"
    event_ttl:
      "sales.order.created": "24h"
      "inventory.*": "15m"
```

Endpoints: `GET /data_warehouse/fetch`, `POST /data_warehouse/acknowledge`.

### Configuration Reference

| Parameter        | Type           | Default                                                    | Description                                                                                      |
|:-----------------|:---------------|:-----------------------------------------------------------|:-------------------------------------------------------------------------------------------------|
| `type`           | `string`       | —                                                          | Must be `http_pull`.                                                                             |
| `match`          | `string\|list` | `"*"`                                                      | Event type filter. Supports `"*"`, `"prefix.*"`, and exact matches.                              |
| `path`           | `dict`         | `{"extract": "extract", "mark_processed": "mark-processed"}` | URL suffixes for the two endpoints. Prefixed with `/{sink_name}/`.                              |
| `coalesce`       | `list`         | `null`                                                     | Event type patterns to coalesce. Events with same type and entity_id are merged to latest only.  |
| `ttl_enabled`    | `bool`         | `true`                                                     | Whether to filter out events older than their TTL.                                               |
| `default_ttl`    | `string`       | `"1h"`                                                     | Default TTL for events without a specific rule. Supports human-readable intervals (e.g. `"2h"`). |
| `event_ttl`      | `dict`         | `{}`                                                       | Per-type TTL overrides. Keys use the same matching patterns as `match`.                          |

## API Reference

### Extract Events

`GET /{sink_name}/extract`

Returns a batch of unprocessed events, ordered oldest first.

**Query Parameters:**

| Parameter    | Type     | Description                                                                                  |
|:-------------|:---------|:---------------------------------------------------------------------------------------------|
| `event_type` | `string` | Optional. Filter by event type (e.g. `?event_type=sales.order.created`). Supports wildcards. |
| `batch_size` | `int`    | Optional. Maximum number of events to return (must be ≥ 1).                                  |

**Response:**

```json
{
  "batch_id": 2048,
  "events": [
    {
      "id": 12345,
      "event_id": "evt_abc",
      "event_type": "sales.order.created",
      "entity_id": "order_789",
      "created_at": "2024-03-15T10:00:00+00:00",
      "data": { "order_id": "ORD-789", "amount": 150.00 },
      "source": {
        "id": 3,
        "name": "ecommerce_shop"
      },
      "meta": {}
    }
  ],
  "remaining_events": 42
}
```

- `batch_id`: Use this value when confirming. `null` if there are no events.
- `events`: List of events in the [standard envelope format](sinks-general.md#event-envelope).
- `remaining_events`: Total number of unprocessed events currently available for this sink (after subtracting the ones in this response). If coalescing is enabled, this count reflects coalesced output. Keep calling extract and confirming until this reaches `0`.

### Confirm Processing

`POST /{sink_name}/mark-processed?batch_id={id}`

Marks all events in the given batch as processed. Always call this *after* your app has safely committed the data.

An event confirmed in *any* batch for this sink stops appearing in future extracts, even if an older overlapping batch was never confirmed.

**Response:**

```json
{
  "status": "success",
  "marked_count": 50
}
```
