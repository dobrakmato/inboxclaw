# Event Coalescing & Debouncing

## Overview

Coalescing is the process of combining multiple related events into a single, summarized event. This is particularly useful for reducing noise from systems that emit frequent updates for the same entity (e.g., a file being saved multiple times, or a sensor reporting rapid state changes).

By using coalescing, you can ensure that your [Sinks](sinks-general.md) receive a "pre-optimized" stream of events, reducing the processing load on your downstream applications.

### Why use Coalescing?

*   **Noise Reduction**: Suppress rapid-fire updates (like "File Updated" events during an active edit session).
*   **Efficiency**: Sinks process one "final" state instead of dozens of intermediate updates.
*   **Deduplication**: Effectively deduplicate events that represent the same logical change.

---

## Core Concepts

### Coalescing vs. Debouncing vs. Batching

While often used interchangeably, these terms have specific meanings in the context of Inboxclaw:

1.  **Coalescing**: The general act of merging multiple events into one.
2.  **Debouncing**: A strategy where the "flush" timer resets every time a new event arrives. If you keep sending events within the window, the timer keeps moving forward. The event is only delivered after a period of "silence".
3.  **Batching**: A strategy where a fixed window starts from the *first* event seen. All subsequent events within that window are merged, and the result is flushed exactly when the window expires, regardless of when the last event arrived.

> **Note on Maximum Latency**: To prevent an event from being held indefinitely by a constant stream of "debounced" updates, the system handles each event window separately based on its initial trigger. 

### The "In-Flight" State

When an event matches a coalescing rule, it doesn't go to the main event table immediately. Instead, it enters a **Pending** state in the database. During this time:
- It is visible in the `pending_events` table (for observability).
- It is NOT yet sent to any sinks.
- Subsequent matching events will update this pending record (e.g., merging the data blob, incrementing a counter).

Once the window (Debounce or Batch) expires, a background service "promotes" the pending event to the main event table and notifies all matching sinks.

---

## Configuration

Coalescing is configured at the **Source** level. This means the optimization happens once at the entry point, and all connected sinks benefit from it.

### Minimal Configuration (Debounce)

The most common use case is debouncing "File Updated" events.

```yaml
sources:
  gdrive:
    type: google_drive
    coalesce:
      - match: "google.drive.file_updated"
        strategy: "debounce"
        window: "30s"
```

### Batch Configuration

Use batching when you want updates at a fixed interval.

```yaml
sources:
  my_sensor:
    type: home_assistant
    coalesce:
      - match: "ha.state_changed"
        strategy: "batch"
        window: "5m"
```

### Full Configuration Example

You can define multiple rules for a single source using glob patterns or a list of event types.

```yaml
sources:
  noisy_source:
    type: some_type
    coalesce:
      - match: ["*.updated", "*.changed", "google.drive.file_moved"]
        strategy: "debounce"
        window: "1m"
        aggregation: "latest" # Default: take the data from the last seen event
      - match: "system.heartbeat"
        strategy: "batch"
        window: "1h"
```

---

## Aggregation Details

When events are coalesced, the system needs to know how to merge their `data` payloads.

*   **`latest` (Default)**: The data from the most recent event overwrites previous data. This is ideal for "State" updates where only the current value matters.
*   **`merge`**: (Future) Merges JSON objects together.

### Metadata

Promoted events include special metadata fields to help you understand what happened:

| Field | Description |
| :--- | :--- |
| `meta.coalesced_count` | Total number of raw events merged into this one. |
| `meta.first_seen_at` | Timestamp of the very first event in the window. |
| `meta.last_seen_at` | Timestamp of the last event that contributed to this one. |

---

## When to Use Which Strategy?

| Strategy | Best For... | Example |
| :--- | :--- | :--- |
| **Debounce** | Rapid, unpredictable bursts of activity. | A user hitting "Save" every few seconds on a document. |
| **Batch** | Constant streams of data where you want periodic summaries. | A temperature sensor reporting every 10 seconds. |
| **None (Default)** | Transactional events where every single occurrence is critical. | Bank transactions, Login attempts. |

---

## Event Definitions

Coalescing rules match against the `event_type`. Here are some common types you might want to coalesce:

| Source | Event Type | Recommended Strategy |
| :--- | :--- | :--- |
| Google Drive | `google.drive.file_updated` | Debounce (30s - 1m) |
| Home Assistant | `ha.state_changed` | Debounce or Batch |
| Gmail | `gmail.message_updated` | Debounce |
