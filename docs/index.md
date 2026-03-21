# Ingest Pipeline Documentation

Ingest Pipeline is a small event hub: it pulls changes from external systems (sources), stores them durably, and delivers them to your apps (sinks).

If you are integrating multiple APIs and want one reliable stream of normalized events, this project is built for that use case.

## Getting Started

1. Create a minimal `config.yaml` (note that the top-level sink key is singular: `sink`):

```yaml
sources:
  demo:
    type: mock
    poll_interval: "5s"

sink:
  out:
    type: sse
    match: "*"
```

2. Run the app:

```bash
python main.py listen
```

3. Open the API docs at `http://127.0.0.1:8000/docs` and connect a sink client (for example SSE).

Then continue with [Configuration](configuration.md), [Sources](sources-general.md), and [Sinks](sinks-general.md).

## Core Concepts

- **Source**: connector that reads external changes and emits normalized events.
- **Pipeline**: deduplicates and stores events durably, then notifies sinks.
- **Sink**: connector that delivers matched events to consumers.
- **Coalescing**: optional reduction of many updates for one entity into the latest state.

## Architecture and Internals

- [App Lifecycle](app-lifecycle.md)
- [Event Pipeline](pipeline.md)
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
