# Mock Source

The Mock source generates a continuous stream of random events at a configurable interval. It is a **testing and diagnostic tool** — use it to verify that your sinks are working, to test matching rules, or as a heartbeat to confirm the pipeline is alive.

This source is not intended for production use. It produces a single event type (`mock.random_number`) with a random integer payload.

## Getting Started

Add a Mock source to your `config.yaml`:

```yaml
sources:
  test_source:
    type: mock
```

This will generate one event every 10 seconds (the default interval).

## Configuration

### Minimal Configuration

```yaml
sources:
  test_source:
    type: mock
```

Default: `interval: "10s"`.

### Full Configuration

```yaml
sources:
  fast_test:
    type: mock
    interval: "5s"

  daily_heartbeat:
    type: mock
    interval: "24h"
```

### Configuration Reference

| Parameter  | Type     | Default | Description                                                                    |
|:-----------|:---------|:--------|:-------------------------------------------------------------------------------|
| `interval` | `string` | `"10s"` | Time between events. Supports human-readable intervals (e.g. `"5s"`, `"1m"`). |

Short intervals lead to rapid database growth. For long-term monitoring, use intervals of `"1m"` or longer.

## Event Definitions

| Type                 | Entity ID              | Description                          |
|:---------------------|:-----------------------|:-------------------------------------|
| `mock.random_number` | `mock-{source_name}`   | A random number between 1 and 100.   |

### Event Example

```json
{
  "id": 1,
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "event_type": "mock.random_number",
  "entity_id": "mock-test_source",
  "created_at": "2026-03-14T12:00:00+00:00",
  "data": {
    "number": 42
  },
  "meta": {}
}
```

The `entity_id` is `mock-` followed by the source name from your `config.yaml`.
