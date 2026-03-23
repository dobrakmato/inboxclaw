# Command Sink

The Command sink executes a CLI command for every event that matches its filter. It runs commands sequentially from an internal queue, ensuring that one command finishes before the next one starts.

This is ideal for integrating with existing scripts, legacy systems, or performing local actions like sending notifications via a custom CLI tool, updating local files, or triggering deployments.

## Getting Started

Add a Command sink to your `config.yaml` with the command you want to run:

```yaml
sink:
  notify_admin:
    type: command
    command: "mail -s 'New event: #root.event_id' admin@example.com"
```

The sink will execute this command for every event. It uses the [templating engine](templating.md) for customizing the command string.

## Core Concepts

### Real-time Delivery and Retries
The Command sink listens for new events in real-time. When a matching event arrives, it is added to an internal queue and processed sequentially.

- **Success**: A command is considered successful if it returns an exit code of `0`.
- **Failure**: Any non-zero exit code is treated as a failure. The sink will retry failed commands up to `max_retries` (default: 3).
- **Circuit Breaker**: To prevent runaway processes or system overload during persistent failures, the sink includes a circuit breaker. If 5 commands fail in a row, the sink will stop executing any commands for 10 minutes (the "cool-off" period).

### Batch Processing
If many events accumulate in the queue (controlled by `batch_threshold`), the sink can switch to "batch mode". In this mode, it executes a single command for all events in the current batch.

```yaml
sink:
  bulk_process:
    type: command
    command: "process_one.sh --id #root.event_id"
    batch_command: "process_batch.sh --payloads '$root'"
    batch_threshold: 10
```

- **Implicit Batching**: If `batch_command` is not provided, it uses the regular `command` template, but the `root` object in the template context becomes a **list of events** instead of a single event.

::: tip Note
If you enable batching (`batch_threshold > 1`) and do not provide a `batch_command`, your primary `command` must be compatible with both a single object and a list of objects. For example, `echo #root.event_id` will fail when `root` is a list. In most cases, it is recommended to provide an explicit `batch_command` when using batching.
:::

- **Explicit Batching**: Providing a `batch_command` allows you to use a different CLI tool or different flags optimized for bulk operations.

### TTL (Time-To-Live)
TTL is **enabled by default** with a `default_ttl` of `1h`. Events older than their TTL are skipped and marked as processed with a skip message. This prevents the sink from running thousands of stale commands after a long downtime.

### Persistence
All executions are recorded in the `command_sink_deliveries` table. Every received event has an entry with a `processed` flag. If the command fails, the error is logged, and the sink will periodically retry the event.

## Configuration

### Minimal Configuration

```yaml
sink:
  my_cmd:
    type: command
    command: "echo #root.event_id"
```

Defaults: `match: "*"`, `batch_threshold: 10`, `max_retries: 3`, `ttl_enabled: true`, `default_ttl: "1h"`.

### Full Configuration

```yaml
sink:
  advanced_cmd:
    type: command
    command: "python process.py --data '$root.data'"
    batch_command: "python process_batch.py --json '$root'"
    batch_threshold: 50
    max_retries: 5
    retry_interval: "5m"
    match:
      - "user.auth.*"
      - "payment.processed"
    ttl_enabled: true
    default_ttl: "12h"
    event_ttl:
      "user.auth.*": "1h"
```

### Configuration Reference

| Parameter         | Type           | Default  | Description                                                                                       |
|:------------------|:---------------|:---------|:--------------------------------------------------------------------------------------------------|
| `type`            | `string`       | —        | Must be `command`.                                                                                |
| `command`         | `string`       | Required | The shell command to execute. Supports template interpolation.                                     |
| `batch_command`   | `string`       | —        | Command to use when `batch_threshold` is reached. If omitted, `command` is used.                  |
| `batch_threshold` | `int`          | `10`     | Number of events in queue required to trigger batch processing.                                   |
| `max_retries`     | `int`          | `3`      | Maximum number of retries for a failed command.                                                   |
| `retry_interval`  | `string`       | `"10s"`  | Minimum wait between retries.                                                                     |
| `match`           | `string|list` | `"*"`    | Event type filter. Supports `"*"`, `"prefix.*"`, and exact matches.                               |
| `ttl_enabled`     | `bool`         | `true`   | Whether to skip events older than their TTL.                                                      |
| `default_ttl`     | `string`       | `"1h"`   | Default TTL for events without a specific rule.                                                   |
| `event_ttl`       | `dict`         | `{}`     | Per-type TTL overrides. Keys use the same matching patterns as `match`.                           |

## Template Interpolation

The Command sink uses the [templating engine](templating.md) to dynamically construct shell commands.

### Single Event Context
When processing one by one, `root` is a single event object:
- `#root.event_id` -> `evt_123`
- `$root.data` -> `{"key": "value"}`

### Batch Context
When processing a batch, `root` is a **list** of event objects:
- `$root` -> `[{"event_id": "evt_1", ...}, {"event_id": "evt_2", ...}]`
- `#root.0.event_id` -> `evt_1` (Accessing by index)
