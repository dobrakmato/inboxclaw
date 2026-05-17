# Folder Sink

The Folder sink writes events as JSONL files to a local directory. For every day a new file is created, named by date (`YYYY-MM-DD.jsonl`). Incoming events are appended to the file matching their `created_at` date.

This is useful for archiving events to disk, creating local backups, or feeding downstream batch-processing tools that consume line-delimited JSON.

## Getting Started

Add a Folder sink to your `config.yaml` with the target directory:

```yaml
sink:
  archive:
    type: folder
    path: "./data/events"
```

The sink will start writing all events (`match: "*"`) as JSONL to the given folder. A new file is created for each calendar day.

## Core Concepts

### File Naming

Files are named using the event's `created_at` date in `YYYY-MM-DD` format with a `.jsonl` extension:

```
./data/events/2025-03-15.jsonl
./data/events/2025-03-16.jsonl
```

### JSONL Format

Each line in the file is a single JSON object representing one event in the [standard envelope format](sinks-general.md#event-envelope):

```json
{"id": 42, "event_id": "evt_12345", "event_type": "gmail.message_received", "entity_id": "msg_99", "created_at": "2025-03-15T10:00:00+00:00", "data": {"subject": "Hello"}, "source": {"id": 1, "name": "gmail_primary"}, "meta": {}}
```

### Directory Creation

The sink automatically creates the target directory (including parent directories) on startup if it does not exist.

## Configuration

### Minimal Configuration

```yaml
sink:
  archive:
    type: folder
    path: "./data/events"
```

Defaults: `match: "*"`.

### Full Configuration

```yaml
sink:
  gmail_archive:
    type: folder
    path: "${EVENT_ARCHIVE_DIR}"
    match:
      - "gmail.*"
      - "google.calendar.*"
```

### Configuration Reference

| Parameter | Type           | Default | Description                                                         |
|:----------|:---------------|:--------|:--------------------------------------------------------------------|
| `type`    | `string`       | —       | Must be `folder`.                                                   |
| `path`    | `string`       | Required | Directory to write JSONL files to. Supports env vars via `${VAR}`. |
| `match`   | `string\|list` | `"*"`   | Event type filter. Supports `"*"`, `"prefix.*"`, and exact matches. |
