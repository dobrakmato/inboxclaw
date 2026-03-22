# Event Pipeline

The pipeline is the core path from incoming source data to outbound sink delivery.

In plain terms: sources produce events, the pipeline stores them safely, and sinks deliver them to consumers.

## End-to-End Flow

1. A source detects a change in an external system.
2. The source converts that change into a normalized event (`event_id`, `event_type`, `entity_id`, `occurred_at`, `data`, `meta`).
3. The pipeline writer deduplicates events using `(source_id, event_id)`.
4. New events are stored durably in SQLite.
5. The notifier signals sinks that new events are available.
6. Sinks pull matching events and deliver them through their transport.

## Why Deduplication Exists

External APIs are often eventually consistent and can return overlapping windows of data. Without deduplication, restarts or retries could create duplicates and trigger duplicate downstream actions.

The uniqueness constraint on `(source_id, event_id)` guarantees idempotent writes per source.

## Matching and Delivery

Sinks receive only events that match their `match` patterns (`*`, `prefix.*`, or exact type).

Depending on sink type:

- **Webhook** pushes event-by-event with retry behavior.
- **SSE** streams in near real-time to connected clients.
- **HTTP Pull** creates batches that clients fetch and confirm.
- **Win11 Toast** shows local desktop notifications (debug/developer usage).

## Coalescing (What It Means)

Coalescing reduces noise when many updates happen quickly for the same entity.

- Group key: `(event_type, entity_id)`
- Result: keep only the latest event in that group for delivery

Example: if one file is updated 10 times in 30 seconds, a coalescing-enabled sink can deliver only the latest state instead of all 10 intermediate updates.

## Reliability Notes

- Events are persisted before delivery attempts.
- Delivery state is sink-specific and stored in dedicated tables (for example webhook delivery attempts and HTTP pull batch state).
- Background tasks are tracked by `AppServices` and cancelled during graceful shutdown.
