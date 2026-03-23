# Templating Engine

Inboxclaw includes a powerful templating engine used by various sinks (like Webhook and Command sinks) to rewrite payloads or construct command lines dynamically.

## Core Concepts

The templating engine allows you to inject data from the event envelope into your configuration. It supports both native type injection and JSON stringification.

### Syntax

- **Literal values**: Any value that doesn't start with `#` or `$` is treated as a literal and sent as-is.
- **`#path` (Native Injection)**: Resolves the value at the specified path from the root event and injects it using its native type (string, number, boolean, object, or array).
- **`$path` (JSON Stringification)**: Resolves the value at the path and injects it as a JSON-stringified value. This is particularly useful for APIs or CLI commands that expect a JSON string.

### The `root` Object

The `root` object corresponds to the [Standard Event Envelope](sinks-general.md#event-envelope). You can traverse nested objects using dot notation.

#### Example Envelope
```json
{
  "id": 42,
  "event_id": "evt_12345",
  "event_type": "user.auth.login",
  "data": {
    "ip_address": "192.168.1.1"
  }
}
```

#### Template Examples
- `#root.event_id` → `"evt_12345"`
- `#root.data.ip_address` → `"192.168.1.1"`
- `$root.data` → `"{\"ip_address\": \"192.168.1.1\"}"`

## Usage in Sinks

### Webhook Sink
In the [Webhook sink](sink-webhook.md), the `payload` configuration uses this engine to transform the JSON body sent to the endpoint.

```yaml
payload:
  id: "#root.event_id"
  raw_data: "$root.data"
```

### Command Sink
In the [Command sink](sink-command.md), the `command` and `batch_command` strings are processed through the template engine before execution.

```yaml
command: "echo 'New event #root.event_id received'"
```

In batch mode, `root` is a **list of events** instead of a single object. You should use appropriate templates to handle the list (usually by stringifying the whole list).

```yaml
batch_command: "my_script.sh --data '$root'"
```
