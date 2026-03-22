# SSE Sink

The SSE (Server-Sent Events) sink streams events to your application in real-time over a persistent HTTP connection. Your app opens a connection and receives events as they arrive — no polling, no webhooks, no open ports required on your side.

This is a good fit for browser dashboards, monitoring tools, and any client that can hold a long-lived HTTP connection. It works natively with the browser `EventSource` API. If you need guaranteed delivery with confirmation, use the [HTTP Pull sink](sink-http-pull.md) instead — SSE is fire-and-forget, and events missed during a disconnection are not replayed.

## Getting Started

Add an SSE sink to your `config.yaml`:

```yaml
sink:
  events_stream:
    type: sse
```

This exposes an SSE endpoint at `GET /events_stream/`. Connect to it and start receiving events:

```bash
curl -N http://localhost:8000/events_stream/
```

Or in a browser:

```javascript
const source = new EventSource("http://localhost:8000/events_stream/");
source.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log("New event:", data);
};
```

## Core Concepts

### Streaming Lifecycle

1. Your client opens a `GET` request to the SSE endpoint.
2. The sink responds with `200 OK` and sends an `info` event confirming the connection.
3. From that point on, any new event that matches the sink's filters is pushed immediately.
4. Every 30 seconds (configurable via `heartbeat_timeout`), the sink sends a `heartbeat` event to keep the connection alive.

Only events that arrive *after* the connection is established are streamed. Historical events are not replayed.

### Event Filtering

Filtering works at two levels:

- **Server-side** (`match` in config): Sets the boundary of what this sink is allowed to stream.
- **Client-side** (`event_type` query parameter): Further narrows the stream per connection.

The client parameter can only *restrict* — it cannot bypass the server-side `match` rule.

```
GET /events_stream/?event_type=gmail.message_received
```

## Configuration

### Minimal Configuration

```yaml
sink:
  events_stream:
    type: sse
```

Endpoint: `GET /events_stream/`. Defaults: `match: "*"`, `heartbeat_timeout: 30s`.

### Full Configuration

```yaml
sink:
  alerts:
    type: sse
    path: "/live"
    match: "alert.*"
    heartbeat_timeout: 15
```

Endpoint: `GET /alerts/live`.

### Configuration Reference

| Parameter           | Type           | Default | Description                                                                         |
|:--------------------|:---------------|:--------|:------------------------------------------------------------------------------------|
| `type`              | `string`       | —       | Must be `sse`.                                                                      |
| `match`             | `string\|list` | `"*"`   | Event type filter. Supports `"*"`, `"prefix.*"`, and exact matches.                 |
| `path`              | `string`       | `""`    | URL suffix appended to `/{sink_name}/`. Empty means the endpoint is `/{sink_name}/`.|
| `heartbeat_timeout` | `string`       | `30.0`  | Seconds between heartbeat pings. Supports human-readable intervals (e.g. `"30s"`).  |

## Response Format

The sink uses the standard [SSE protocol](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events). Each message is separated by a double newline.

```text
event: info
data: connected

event: message
id: 42
data: {"id": 42, "event_id": "evt_abc", "event_type": "user.login", "entity_id": "user_1", "created_at": "2024-03-15T10:00:00+00:00", "data": {"ip": "1.2.3.4"}, "source": {"id": 1, "name": "web_frontend"}, "meta": {}}

event: heartbeat
data: ping
```

- `event: info` — connection status messages.
- `event: message` — an event in the [standard envelope format](sinks-general.md#event-envelope), JSON-encoded in the `data` field. The `id` field is the internal database ID.
- `event: heartbeat` — keep-alive ping sent during quiet periods.

### Client-Side Query Parameters

| Parameter    | Type     | Description                                                                                          |
|:-------------|:---------|:-----------------------------------------------------------------------------------------------------|
| `event_type` | `string` | Optional. Filter the stream by event type (e.g. `user.*`). Refines the server-side `match` pattern. |
