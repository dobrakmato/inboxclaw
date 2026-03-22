![Inboxclaw Logo](assets/logo.png)

# Inboxclaw

Inboxclaw is a small self-hosted event hub for your personal digital life.

It watches the services you already use, turns their changes into a clean local stream of events, deduplicates noisy updates, stores them durably, and makes them easy to consume from your own apps, automations, and assistants.

Instead of wiring every API to every downstream tool, you connect each source once and get one place where new emails, calendar changes, bank transactions, file updates, and other signals show up in a consistent way.

## Why use it

Modern personal tooling is fragmented. Your inbox lives in one place, your calendar in another, files in a third, banking in a fourth, and device state somewhere else again.

Inboxclaw gives you a single event layer across those systems.

That makes it useful when you want to:

* power a personal assistant or LLM workflow with real-world events
* build automations without re-implementing polling and deduplication for every API
* keep a durable local record of interesting changes
* expose those changes through webhooks, SSE, or pull-based consumers
* prototype a personal operations hub without standing up heavy infrastructure

## What it does

Inboxclaw does:

* polls or subscribes to supported external services
* converts changes into normalized events
* stores them in SQLite
* deduplicates repeated fetches
* delivers matching events to one or more sinks
* supports coalescing for noisy update streams

In practice, it is best thought of as an event inbox for personal systems and assistant-facing workflows.

## Current shape

In very active development, some features are missing, might break at any time.

It is a good fit for:

* personal automation
* local or self-hosted assistant backends
* side projects and internal tools
* lightweight integration glue

## Getting Started

1. **Clone and Configure**:
   - Clone the repository: `git clone ... && cd inboxclaw`
   - Copy `config.example.yaml` to `config.yaml` to use as a starting point.
   - Create a `.env` file with your API tokens.
   - **Important for Google**: If you're using Google services (Gmail, Calendar, Drive), you must run `python main.py google auth` to authenticate. A simple API key is not enough. Check the [Google Auth CLI Guide](google-auth-cli.md) for more details.
   - Or create a minimal `config.yaml` (note that the top-level sink key is singular: `sink`):

```yaml
$schema: ./config.schema.json
server:
  host: 0.0.0.0
  port: 8001

database:
  retention_days: 7
  db_path: ./data/data.db

sources:
  demo:
    type: mock
    poll_interval: "5s"

sink:
  out:
    type: sse
    match: "*"
```

2. **Run the Application**:
   Just run `python main.py listen`. On the first run, Inboxclaw will automatically create a virtual environment and install all necessary dependencies for you.

```bash
python main.py listen
```

3. **Explore the API**:
   Open the API docs at `http://127.0.0.1:8000/docs` and connect a sink client (for example SSE).

Then continue with [Configuration](configuration.md), [Sources](sources-general.md), and [Sinks](sinks-general.md).

## Core Concepts

- **Source**: connector that reads external changes and emits normalized events.
- **Pipeline**: deduplicates and stores events durably, then notifies sinks.
- **Sink**: connector that delivers matched events to consumers.
- **[Coalescing](coalescing.md)**: optional reduction of many updates for one entity into the latest state.

## Architecture and Internals

- [App Lifecycle](app-lifecycle.md)
- [Event Pipeline](pipeline.md)
- [Event Coalescing](coalescing.md)
- [Data Model](data-model.md)

## Event Sources

- [Sources Overview](sources-general.md)
- [Gmail](source-gmail.md)
- [Google Calendar](source-google-calendar.md)
- [Google Drive](source-google-drive.md)
- [Fio Banka](source-fio.md)
- [Faktury Online](source-faktury-online.md)
- [Home Assistant](source-home-assistant.md)
- [GoCardless / Nordigen](source-nordigen.md)
- [Mock Source](source-mock.md)

## Event Sinks

- [Sinks Overview](sinks-general.md)
- [Webhook](sink-webhook.md)
- [SSE](sink-sse.md)
- [HTTP Pull](sink-http-pull.md)
- [Win11 Toast](sink-win11toast.md)

## Additional Guides

- [Configuration](configuration.md)
- [CLI Reference](cli.md)
- [Key/Value Storage](kv-general.md)
- [Google Auth CLI](google-auth-cli.md)
