# Win11 Toast Sink (Desktop Notifications for Fast Debugging)

The Win11 Toast sink is a local debugging sink that shows a Windows 11 notification for each incoming matching event. It helps you quickly validate that events are flowing and classified correctly without opening dashboards, APIs, or logs.

### When to use this sink

| Good fit                                                                | Tradeoff                                                           |
|:------------------------------------------------------------------------|:-------------------------------------------------------------------|
| You need immediate visual confirmation that events are being produced.  | Notifications are local-only and not a durable delivery mechanism. |
| You are tuning event matching rules and want quick feedback.            | High event volume can become noisy.                                |
| You want to inspect event patterns during source development/debugging. | Best-effort summary text may omit some payload details.            |

---

## How it works

- The sink subscribes to new pipeline events.
- It applies standard `match` filtering (same pattern behavior as other sinks).
- For each matching event, it shows a Windows 11 toast:
  - **Title** = `event_type`
  - **Body** = best-effort summary from `data` JSON
- No click action is attached.

### Body summary strategy (best effort)

The sink tries to extract meaningful fields first (for example `title`, `summary`, `message`, `status`, `filename`).
If that fails, it falls back to a compact scalar extraction from nested JSON.
If payload is still hard to summarize, it falls back to a truncated JSON snippet.

---

## Configuration (`config.yaml`)

### Minimal Configuration

```yaml
sink:
  desktop_debug:
    type: win11toast
```

### Filtered Configuration

```yaml
sink:
  calendar_alerts:
    type: win11toast
    match:
      - "google.calendar.*"
      - "gmail.message_received"
```

### Full Configuration

```yaml
sink:
  debug_notifications:
    type: win11toast
    match: "*"
    max_body_length: 220
```

### Configuration options explained

| Option            | What it controls                                                 | Practical guidance                                               |
|:------------------|:-----------------------------------------------------------------|:-----------------------------------------------------------------|
| `type`            | Sink implementation selector.                                    | Must be `win11toast`.                                            |
| `match`           | Event type filter (`*`, exact match, or prefix like `google.*`). | Narrow this during debugging to reduce notification noise.       |
| `max_body_length` | Maximum notification body length before truncation.              | Increase if your event payload summaries are frequently cut off. |

---

## Notes

- This sink is intended mostly for local debugging and development workflows.
- Requires Windows 11 notifications and the `win11toast` Python package.