# Read-Only API

Inboxclaw exposes a set of **read-only HTTP endpoints** that let external applications inspect events, sources, and sinks without modifying any state. All endpoints live under the `/api` prefix.

## Why use the Read-Only API?

A common pattern is to have Inboxclaw deliver lightweight event notifications (via [Webhook](/sink-webhook), [SSE](/sink-sse), or [HTTP Pull](/sink-http-pull)) to another system — for example an LLM agent — which then decides whether an event is interesting. The agent only needs the event ID to reference it; the full event payload can be fetched later through this API. This avoids forcing the consumer to copy or store large JSON payloads.

**Example flow:**

1. Inboxclaw sends a webhook notification containing the event ID to an LLM.
2. The LLM decides the event is relevant and stores just the ID.
3. A downstream application calls `GET /api/events/{id}` to retrieve the full event details on demand.

Other use cases include:

- **Dashboards** — display recent events, pending coalescing events, and configured sources/sinks.
- **Debugging** — inspect what events have been ingested and what is waiting to be flushed.
- **Auditing** — retrieve events by ID for compliance or record-keeping.

## Endpoints

### Get Event by ID

Retrieve a single event by its database ID.

```
GET /api/events/{event_db_id}
```

**Response** (`200 OK`):

```json
{
  "id": 42,
  "event_id": "msg_abc123",
  "event_type": "mail.new",
  "entity_id": "thread_xyz",
  "source_id": 1,
  "source_name": "gmail",
  "created_at": "2026-04-13T10:00:00",
  "occurred_at": null,
  "data": {"subject": "Hello", "from": "alice@example.com"},
  "meta": {}
}
```

Returns `404` if the event does not exist.

---

### List Events

Retrieve multiple events with optional filters and pagination.

```
GET /api/events
```

**Query parameters:**

| Parameter     | Type   | Default | Description                                              |
|---------------|--------|---------|----------------------------------------------------------|
| `ids`         | string | —       | Comma-separated list of event database IDs to fetch.     |
| `event_type`  | string | —       | Filter by exact event type (e.g. `mail.new`).            |
| `source_name` | string | —       | Filter by source name (e.g. `gmail`).                    |
| `limit`       | int    | 50      | Maximum number of events to return (1–500).              |
| `offset`      | int    | 0       | Number of events to skip for pagination.                 |

**Response** (`200 OK`):

```json
{
  "events": [ ... ],
  "total": 128
}
```

The `total` field reflects the total number of matching events (before pagination), so you can calculate how many pages remain.

::: tip Fetching by IDs
When you already know the event IDs (e.g. from a webhook notification), pass them as `ids=1,2,3` to retrieve exactly those events in one call.
:::

---

### Recent Events

A convenience endpoint that returns the most recent events ordered by creation time.

```
GET /api/events/recent
```

**Query parameters:**

| Parameter | Type | Default | Description                          |
|-----------|------|---------|--------------------------------------|
| `limit`   | int  | 20      | Number of recent events (1–200).     |

**Response** (`200 OK`): Same shape as [List Events](#list-events).

---

### Pending Events

Retrieve events that are currently waiting in the [coalescing](/coalescing) buffer.

```
GET /api/pending-events
```

**Query parameters:**

| Parameter | Type | Default | Description                              |
|-----------|------|---------|------------------------------------------|
| `limit`   | int  | 50      | Maximum number of events (1–500).        |
| `offset`  | int  | 0       | Number of events to skip for pagination. |

**Response** (`200 OK`):

```json
{
  "events": [
    {
      "id": 1,
      "source_id": 1,
      "event_type": "mail.new",
      "entity_id": "thread_xyz",
      "data": {"subject": "Hello"},
      "meta": {},
      "count": 3,
      "first_seen_at": "2026-04-13T09:50:00",
      "last_seen_at": "2026-04-13T10:00:00",
      "flush_at": "2026-04-13T10:05:00",
      "strategy": "debounce",
      "window_seconds": 300
    }
  ],
  "total": 1
}
```

The `count` field shows how many raw events have been coalesced into this pending entry. See [Coalescing](/coalescing) for more details.

---

### List Sources

List all configured event sources.

```
GET /api/sources
```

**Response** (`200 OK`):

```json
[
  {"id": 1, "name": "gmail", "type": "gmail"},
  {"id": 2, "name": "calendar", "type": "google_calendar"}
]
```

---

### List Sinks

List all configured event sinks.

```
GET /api/sinks
```

**Response** (`200 OK`):

```json
[
  {"id": 1, "name": "my_webhook", "type": "webhook"},
  {"id": 2, "name": "my_pull", "type": "http_pull"}
]
```
