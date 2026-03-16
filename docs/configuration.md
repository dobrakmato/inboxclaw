# Configuration

This document explains how to configure the `ingest-pipeline`. The configuration is stored in a YAML file (default `config.yaml`).

## Overview

The configuration is divided into four main sections:

1.  **Server**: FastAPI server settings (host, port).
2.  **Database**: SQLite database path and retention policy.
3.  **Sources**: Definitions of data sources to poll from (Gmail, Fio, etc.).
4.  **Sinks**: Definitions of where to deliver the processed events (Webhook, SSE, etc.).

## Human-Readable Intervals

To make configuration more intuitive, many settings that require a duration (like `poll_interval` or `ttl`) support human-readable strings.

Examples:
- `"5s"` - 5 seconds
- `"1m"` - 1 minute
- `"1h"` - 1 hour
- `"1d"` - 1 day
- `"1h 30m"` - 1 hour and 30 minutes

These strings are automatically converted to numerical seconds by the pipeline.

## Environment Variable Expansion

For sensitive information like API tokens or keys, you should avoid hardcoding them directly in the YAML file. The pipeline supports environment variable expansion using the `${VAR}` or `$VAR` syntax.

### Example

**.env file:**
```env
FIO_TOKEN_PERSONAL=your_personal_token
FIO_TOKEN_BUSINESS=your_business_token
```

**config.yaml:**
```yaml
sources:
  personal_acc:
    type: fio
    token: ${FIO_TOKEN_PERSONAL}
    poll_interval: "1h"
  
  business_acc:
    type: fio
    token: ${FIO_TOKEN_BUSINESS}
    poll_interval: "30m"
```

## Section Details

### Server

| Option | Description | Default |
| :--- | :--- | :--- |
| `host` | The interface to bind to. | `0.0.0.0` |
| `port` | The port to listen on. | `8000` |

### Database

| Option | Description | Default |
| :--- | :--- | :--- |
| `db_path` | Path to the SQLite database file. | `./data/data.db` |
| `days` | Number of days to keep processed events before cleanup. | `30` |

### Sources

Sources are defined as a dictionary where the key is a unique name for the source instance.

#### Common Source Options
| Option | Description |
| :--- | :--- |
| `type` | The type of source (e.g., `fio`, `gmail`, `home_assistant`). |
| `poll_interval` | How often to check for new data (e.g., `"10m"`). |

Each source type has its own specific configuration. See the dedicated documentation for each source:
- [Fio Banka](source-fio.md)
- [Faktury Online](source-faktury-online.md)
- [Home Assistant](source-home-assistant.md)
- [Google Services](google-auth-cli.md) (Gmail, Calendar, Drive)

### Sinks

Sinks define where events are sent. Like sources, they are defined in a dictionary.

#### Common Sink Options
| Option | Description |
| :--- | :--- |
| `type` | The type of sink (e.g., `webhook`, `sse`, `http_pull`). |
| `match` | A glob pattern or list of patterns to match event types (e.g., `fio.*`). |

#### TTL (Time To Live)
Many sinks support TTL for events. If an event is not consumed within its TTL, it may be cleaned up or marked as expired depending on the sink type.

| Option | Description | Default |
| :--- | :--- | :--- |
| `ttl_enabled` | Whether to use TTL for this sink. | `true` |
| `default_ttl` | Default TTL for all events (e.g., `"1h"`). | `"1h"` |
| `event_ttl` | A map of specific event types to their TTL. | `{}` |

See the dedicated documentation for each sink:
- [Webhook](sink-webhook.md)
- [SSE](sink-sse.md)
- [HTTP Pull](sink-http-pull.md)
- [Windows 11 Toast](sink-win11toast.md)

## Full Example

```yaml
server:
  host: "127.0.0.1"
  port: 9000

database:
  db_path: "./data/production.db"
  days: 60

sources:
  my_gmail:
    type: gmail
    token_file: "data/google_token.json"
    poll_interval: "5m"
  
  fio_personal:
    type: fio
    token: ${FIO_TOKEN}
    poll_interval: "30m"

sink:
  pushover_hook:
    type: webhook
    url: "https://api.pushover.net/1/messages.json"
    match: 
      - "fio.transaction.income"
      - "google_calendar.event.started"
    ttl_enabled: false
  
  local_sse:
    type: sse
    match: "*"
    coalesce: ["fio.transaction.*"]
```
