# Data Model

This page documents the current SQLite schema used by the ingest pipeline.

> Source of truth: `src/database.py`

## Why This Matters

The data model is the contract between sources, the internal pipeline, and sinks. Understanding these tables helps with debugging delivery issues, writing custom tooling, and reasoning about reliability.

## Entity Overview

| Table | Purpose |
|:--|:--|
| `sources` | Registered source instances from config. |
| `source_kv` | Per-source key/value state storage. |
| `sinks` | Registered sink instances from config. |
| `events` | Durable event store. |
| `http_webhook_deliveries` | Webhook delivery attempts and status per `(event, sink)`. |
| `http_pull_batches` | HTTP Pull batch headers. |
| `http_pull_batch_events` | Membership + processed flag for events in HTTP Pull batches. |

## Tables

### `sources`

| Column | Type | Notes |
|:--|:--|:--|
| `id` | integer PK | Internal source ID. |
| `name` | string unique | Source instance name (config key). |
| `type` | string | Source implementation type. |
| `cursor` | string nullable | Legacy cursor field (many sources now use `source_kv`). |

### `source_kv`

| Column | Type | Notes |
|:--|:--|:--|
| `id` | integer PK | Row ID. |
| `source_id` | integer FK -> `sources.id` | Owning source. |
| `key` | string | State key name. |
| `value` | JSON | State value. |
| `created_at` | datetime | Insert timestamp (UTC). |
| `updated_at` | datetime | Last update timestamp (UTC). |

Constraints:

- Unique: `(source_id, key)`

### `sinks`

| Column | Type | Notes |
|:--|:--|:--|
| `id` | integer PK | Internal sink ID. |
| `name` | string unique | Sink instance name (config key). |
| `type` | string | Sink implementation type. |

### `events`

| Column | Type | Notes |
|:--|:--|:--|
| `id` | integer PK | Internal event ID. |
| `event_id` | string | Source-provided event identifier. |
| `source_id` | integer FK -> `sources.id` | Producer source. |
| `event_type` | string | Dot-separated type (`gmail.message_received`, etc.). |
| `entity_id` | string nullable | Logical entity reference for coalescing. |
| `created_at` | datetime | Persist timestamp (UTC). |
| `occurred_at` | datetime nullable | Original timestamp from source system. |
| `data` | JSON nullable | Event payload. |
| `meta` | JSON | Transient metadata (empty by default). |

Constraints:

- Unique: `(source_id, event_id)` (deduplication)

### `http_webhook_deliveries`

| Column | Type | Notes |
|:--|:--|:--|
| `id` | integer PK | Row ID. |
| `event_id` | integer FK -> `events.id` | Delivered event. |
| `sink_id` | integer FK -> `sinks.id` | Target webhook sink. |
| `tries` | integer | Number of attempts. |
| `last_try` | datetime nullable | Last attempt timestamp. |
| `created_at` | datetime | Row creation timestamp (UTC). |
| `delivered` | boolean | Delivery success state. |

Constraints:

- Unique: `(event_id, sink_id)`

### `http_pull_batches`

| Column | Type | Notes |
|:--|:--|:--|
| `id` | integer PK | Batch ID returned to clients. |
| `sink_id` | integer FK -> `sinks.id` | Owning HTTP Pull sink. |
| `created_at` | datetime | Batch creation timestamp (UTC). |

### `http_pull_batch_events`

| Column | Type | Notes |
|:--|:--|:--|
| `id` | integer PK | Row ID. |
| `batch_id` | integer FK -> `http_pull_batches.id` | Parent batch. |
| `event_id` | integer FK -> `events.id` | Included event. |
| `processed` | boolean | Whether client confirmed this event. |

## Relationship Notes

- One source produces many events.
- One sink can have many webhook delivery rows and many HTTP pull batches.
- One event can be linked to multiple sinks through delivery/batch tables.
