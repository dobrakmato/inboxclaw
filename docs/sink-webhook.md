# Webhook Sink

The Webhook sink pushes events to your application in near real-time via HTTP POST. Whenever a matching event enters the pipeline, the sink sends it to the URL you configure and retries automatically on failure.

This is a good fit when you need low-latency delivery and your receiving endpoint is publicly reachable. It works well with serverless functions (AWS Lambda, Google Cloud Functions) and automation platforms (Zapier, Make.com). If your app is behind a firewall or you need to control the pace of delivery, use the [HTTP Pull sink](sink-http-pull.md) instead.

## Getting Started

Add a Webhook sink to your `config.yaml` with the target URL:

```yaml
sink:
  my_webhook:
    type: webhook
    url: "https://api.myapp.com/events"
```

The sink will start delivering all events (`match: "*"`) to that URL immediately. Your endpoint must return a `2xx` status code to confirm receipt.

## Core Concepts

### Delivery and Retries

The sink runs a background loop that watches for new events. When a matching event arrives, it sends an HTTP POST with a JSON body to your URL.

- **Success**: Any `2xx` response marks the event as delivered.
- **Failure**: Any non-`2xx` response (or a timeout after 10 seconds) counts as a failed attempt. The sink retries up to `max_retries` times (default: 3), waiting at least `retry_interval` (default: 10 seconds) between attempts.
- **Persistence**: Delivery state and retry counts are stored in the database, so nothing is lost if the pipeline restarts.

### TTL (Time-To-Live)

TTL is **enabled by default** with a `default_ttl` of `1h`. Events older than their TTL are skipped and never attempted for delivery. This prevents the sink from trying to deliver a large backlog of stale events after a long downtime.

To disable TTL and attempt delivery for all undelivered events regardless of age:

```yaml
sink:
  my_webhook:
    type: webhook
    url: "https://example.com/events"
    ttl_enabled: false
```

TTL is resolved in order: exact match in `event_ttl` → longest prefix match in `event_ttl` → `default_ttl`.

### Multiple Sinks

You can run multiple Webhook sinks simultaneously. Each sink tracks delivery independently — success or failure for one sink has no effect on another, even if they match the same events.

## Configuration

### Minimal Configuration

```yaml
sink:
  my_webhook:
    type: webhook
    url: "https://api.myapp.com/events"
```

Defaults: `match: "*"`, `max_retries: 3`, `retry_interval: 10s`, `ttl_enabled: true`, `default_ttl: "1h"`.

### Full Configuration

```yaml
sink:
  audit_log:
    type: webhook
    url: "https://audit-service.internal/ingest"
    max_retries: 10
    retry_interval: "1m"
    match:
      - "user.auth.*"
      - "payment.processed"
      - "security.breach"
    ttl_enabled: true
    default_ttl: "24h"
    event_ttl:
      "security.*": "30d"
```

### Configuration Reference

| Parameter        | Type           | Default  | Description                                                                                      |
|:-----------------|:---------------|:---------|:-------------------------------------------------------------------------------------------------|
| `type`           | `string`       | —        | Must be `webhook`.                                                                               |
| `url`            | `string`       | Required | The URL to POST events to.                                                                       |
| `match`          | `string\|list` | `"*"`    | Event type filter. Supports `"*"`, `"prefix.*"`, and exact matches.                              |
| `max_retries`    | `int`          | `3`      | Maximum number of delivery attempts per event.                                                   |
| `retry_interval` | `string`       | `10.0`   | Minimum wait between retries. Supports human-readable intervals (e.g. `"1m"`) or seconds.        |
| `ttl_enabled`    | `bool`         | `true`   | Whether to skip events older than their TTL.                                                     |
| `default_ttl`    | `string`       | `"1h"`   | Default TTL for events without a specific rule.                                                  |
| `event_ttl`      | `dict`         | `{}`     | Per-type TTL overrides. Keys use the same matching patterns as `match`.                          |

## Webhook Payload

The sink sends an HTTP POST with `Content-Type: application/json`. The body is a single event in the [standard envelope format](sinks-general.md#event-envelope):

```json
{
  "id": 42,
  "event_id": "evt_12345",
  "event_type": "user.auth.login",
  "entity_id": "user_99",
  "created_at": "2024-03-13T23:52:00+00:00",
  "data": {
    "ip_address": "192.168.1.1",
    "browser": "Chrome"
  },
  "source": {
    "id": 2,
    "name": "main_app"
  },
  "meta": {}
}
```

### Handling the Response

- **`2xx`**: Event is marked as delivered. No further attempts.
- **Any other status** (including `3xx`, `4xx`, `5xx`): Treated as a failure. The sink will retry according to `max_retries` and `retry_interval`.
- **Timeout**: The sink waits up to 10 seconds for a response. No response within that window is treated as a failure.
