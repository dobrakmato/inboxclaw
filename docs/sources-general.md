# Sources

Sources connect external systems (Google APIs, bank APIs, home automation, etc.) to Inboxclaw. Each source
periodically checks for new data, converts it into structured **events**, and stores them in the database for
sinks to consume.

If you want to get data *into* the pipeline, you configure a source. If you want to get data *out*,
see [Sinks](sinks-general.md).

For detailed information on common configuration options, human-readable intervals, and environment variable expansion,
see the [Configuration Guide](configuration.md).

## Getting Started

Add a source to the `sources` section of your `config.yaml`. Each source needs a unique name (the YAML key) and a `type`
that tells the pipeline which connector to use. If the name matches the type, you can omit `type`.

```yaml
sources:
  my_gmail:
    type: gmail
    token_file: "data/google_token.json"
```

The pipeline will start polling the source automatically on startup.

## How Sources Work

Sources can be implemented as polling connectors (most common) or as push-style integrations that receive events from
external callbacks/endpoints.

1. **Polling**: Most sources run on a timer (`poll_interval`). Each tick, they call the external API and look for
   changes since the last poll.
2. **Cursor**: After each successful poll, the source saves a cursor (sync token, timestamp, or sequence number) in the
   database. On the next poll it picks up where it left off, so no data is fetched twice and nothing is lost if the
   pipeline restarts.
3. **Deduplication**: Every event carries a unique `event_id`. The pipeline ignores events whose `event_id` has already
   been stored.
4. **Event Writing**: New events are written to the database via the internal `EventWriter`. Connected sinks are
   notified immediately.

## Event Fields

Every event produced by a source contains these fields:

| Field         | Description                                                                                                    |
|:--------------|:---------------------------------------------------------------------------------------------------------------|
| `event_id`    | Globally unique identifier for this event instance (e.g. a message ID, a change ID combined with a timestamp). |
| `event_type`  | Dot-separated string describing what happened (e.g. `gmail.message_received`, `fio.transaction.income`).       |
| `entity_id`   | Identifier of the object the event is about (e.g. file ID, email ID). One entity can produce many events.      |
| `data`        | JSON payload with the event-specific details.                                                                  |
| `occurred_at` | Timestamp of when the event actually happened in the source system.                                            |

## Coalescing & Debouncing

Inboxclaw features a centralized **In-Flight Coalescing** system. Multiple rapid events can be merged at the source level before they are stored or delivered to sinks. This is particularly useful for reducing noise from systems that emit frequent updates (e.g., file saves).

Coalescing is configured at the source level using `coalesce` rules.

See the dedicated [Event Coalescing](coalescing.md) page for detailed explanation and examples.

## Available Sources

| Source                                                      | Type              | Description                                | Website                                                    |
|:------------------------------------------------------------|:------------------|:-------------------------------------------|:-----------------------------------------------------------|
| :email: [Gmail](source-gmail.md)                            | `gmail`           | Emails received, sent, deleted, labels.    | [mail.google.com](https://mail.google.com)                 |
| :calendar: [Google Calendar](source-google-calendar.md)     | `google_calendar` | Calendar events created, updated, deleted. | [calendar.google.com](https://calendar.google.com)         |
| :file_folder: [Google Drive](source-google-drive.md)        | `google_drive`    | File changes with debounced updates.       | [drive.google.com](https://drive.google.com)               |
| :bank: [Fio Banka](source-fio.md)                           | `fio`             | Bank transactions.                         | [fio.cz](https://www.fio.cz)                               |
| :page_with_curl: [Faktury Online](source-faktury-online.md) | `faktury_online`  | Invoice changes.                           | [faktury-online.com](https://www.faktury-online.com)       |
| :house: [Home Assistant](source-home-assistant.md)          | `home_assistant`  | Device tracker and sensor updates.         | [home-assistant.io](https://www.home-assistant.io)         |
| :credit_card: [GoCardless / Nordigen](source-nordigen.md)   | `nordigen`        | Bank transactions (GoCardless).            | [gocardless.com](https://gocardless.com/bank-account-data) |
| :test_tube: [Mock](source-mock.md)                          | `mock`            | Random test events for pipeline testing.   | -                                                          |
