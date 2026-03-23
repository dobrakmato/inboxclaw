# Step 3: Configure Sinks

You've got data flowing in from your sources. Now you need to decide where it goes. Sinks are the output side of Inboxclaw — they deliver events to whatever tool, service, or assistant you want.

## What Is a Sink?

A sink is a connector that takes events from Inboxclaw and delivers them somewhere. You can have multiple sinks running at the same time, each delivering to a different destination. They work independently — if one fails, the others keep going.

## Choosing a Sink

| Sink | How it works | Best for |
|:--|:--|:--|
| **[Command](../sink-command.md)** | Runs a program on your computer for each event | OpenClaw (simple setup), scripts, local tools |
| **[Webhook](../sink-webhook.md)** | Sends events over the network via HTTP | OpenClaw (advanced setup), APIs, serverless functions |
| **[SSE](../sink-sse.md)** | Streams events in real-time to connected clients | Dashboards, live monitoring, browser apps |
| **[HTTP Pull](../sink-http-pull.md)** | Lets clients fetch events on their own schedule | Apps behind firewalls, batch processing |
| **[Win11 Toast](../sink-win11toast.md)** | Shows Windows desktop notifications | Personal alerts on your PC |

## Using OpenClaw?

If you're connecting Inboxclaw to OpenClaw as your AI assistant, you have two options:

- **Command** — simpler setup, events go directly into your conversation. The AI sees them with full chat history context.
- **Webhook** — more flexible, uses OpenClaw's hook system for custom routing, separate agents, and wake modes.

We have a dedicated guide for this: **[Getting Started: Inboxclaw + OpenClaw](../getting-started-openclaw.md)**. It walks you through both options with complete configuration examples.

## Basic Sink Configuration

Sinks are defined in your `config.yaml` under the `sink` section. Here's a simple example that sends all events to a webhook:

```yaml
sink:
  my_webhook:
    type: webhook
    url: "https://api.myapp.com/events"
```

And here's one that runs a command for each event:

```yaml
sink:
  my_script:
    type: command
    command: "python process_event.py --data '$root'"
```

### Filtering Events

By default, sinks receive all events (`match: "*"`). You can filter by event type:

```yaml
sink:
  email_alerts:
    type: webhook
    url: "https://api.myapp.com/email-events"
    match:
      - "gmail.*"
      - "calendar.event.created"
```

### Retries and Reliability

All sinks automatically retry failed deliveries. Events are tracked in the database, so nothing is lost if Inboxclaw restarts. You can tune retry behavior per sink:

```yaml
sink:
  my_webhook:
    type: webhook
    url: "https://api.myapp.com/events"
    max_retries: 5
    retry_interval: "1m"
```

For the full details on how sinks work, see [Sinks Overview](../sinks-general.md).

## Next Step

→ [Step 4: Run Your Pipeline](step-4-run.md) — start everything up and verify events are flowing.
