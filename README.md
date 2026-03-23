![Inboxclaw logo](docs/assets/logo.png)

# Inboxclaw

**The event inbox for your AI assistant.**

Your inbox, calendar, files, bank, and devices are all producing signals. Inboxclaw turns them into one clean, durable event stream your assistant can actually use.

Connect the services you already use — email, calendar, cloud storage, banking, device state — and Inboxclaw watches them for you. Instead of every assistant, script, or automation having to integrate with Gmail, Google Calendar, Google Drive, Home Assistant, or bank APIs separately, you connect each source once and consume everything from one place.

## Who is it for

Inboxclaw is built for self-hosters, agent builders, [OpenClaw](docs/getting-started-openclaw.md) users, and automation-heavy power users. It is not trying to be a giant enterprise message bus — it is a lightweight event inbox for personal automation and assistant-facing workflows.

## How it works

The system works in three stages:

1. **Sources watch external systems.** Gmail, Calendar, Drive, bank APIs, Home Assistant — each source detects changes using polling, cursors, or cached snapshots and converts them into normalized events with a consistent shape: event type, entity ID, timestamp, payload, metadata.

2. **The pipeline deduplicates and stores.** Events are checked against `(source_id, event_id)` to avoid double-writes, then persisted in SQLite *before* anything is delivered. For noisy streams like repeated file edits, optional [coalescing](docs/coalescing.md) collapses rapid bursts into one meaningful update.

3. **Sinks deliver to consumers.** Matching events fan out through webhooks, local command execution, SSE streams, or pull-based HTTP batches — each with its own delivery semantics and failure behavior.

```
System change → Source → Normalize → Optional coalesce → Deduplicate → Durable store → Sink matching → Delivery → Consumer
```

## What it guarantees

Practical rather than magical:

* **Idempotent writes** per source event via the `(source_id, event_id)` uniqueness rule.
* **Durability before delivery** — events are persisted before any delivery attempt, so restarts do not erase accepted events.
* **Durable sinks retry** — webhook, command, and HTTP pull keep delivery state and retry after failures.

What it does *not* guarantee is universal exactly-once delivery end to end: SSE is fire-and-forget, webhook delivery depends on HTTP acknowledgement, and TTL rules can intentionally discard stale backlog after downtime.

The right promise is: **durable local intake, controlled fan-out, lower noise, and fewer integration mistakes.**

## For OpenClaw users

Inboxclaw is the part that gives OpenClaw eyes and ears.

OpenClaw is the agent. Inboxclaw is the event intake layer that notices what happened in the outside world — new email, changed calendar event, edited Drive file, bank transaction, Home Assistant state change — and feeds those events into OpenClaw in a structured way.

* For a **simple setup**, Inboxclaw can call the OpenClaw CLI directly so new events land in your main conversation with full context.
* For a **more advanced setup**, it can send webhooks into OpenClaw's hook system so you can route events to separate sessions or agents.

OpenClaw no longer needs to personally integrate with every upstream service. Inboxclaw handles watching, remembering cursors, deduplicating overlaps, reducing noisy bursts, and retrying delivery. OpenClaw gets a cleaner feed of "things worth knowing."

**The simplest mental model: Inboxclaw is OpenClaw's event inbox.**

See the [Inboxclaw + OpenClaw guide](docs/getting-started-openclaw.md) for setup instructions.

## Current shape

In very active development — some features may break at any time. A good fit for personal automation, self-hosted assistant backends, side projects, and lightweight integration glue. Not yet recommended as a conservative "bet the company on it" platform.

## Supported sources

* ✉️ Gmail
* 📅 Google Calendar
* 💾 Google Drive
* 🏦 Fio Banka
* 🧾 Faktury Online
* 🏠 Home Assistant
* 💳 GoCardless Bank Account Data (Nordigen)
* 🧪 Mock source for testing

## Supported sinks

* Webhook
* Server-Sent Events (SSE)
* HTTP Pull
* Command (CLI execution)

## Quick Start

1. **Clone the Repo**:
   ```bash
   git clone https://github.com/your-repo/inboxclaw.git
   cd inboxclaw
   ```

2. **Configure**:
   - Create a `.env` file for your API tokens.
   - Create a `config.yaml` by copying `config.example.yaml` as a template.
   - **Note for Google Services**: If you use Gmail, Google Calendar, or Google Drive, you will need to perform a one-time authentication step using the CLI: `inboxclaw google auth`. An API key is not enough.

3. **Run**:
   ```bash
   python main.py listen
   ```
   *The first run will automatically set up a virtual environment and install dependencies. After that, you can use the `inboxclaw` command instead of `python main.py`.*

## Learn more

**New here?** Follow the [Onboarding Tutorial](docs/onboarding/index.md) — a step-by-step guide from installation to a running pipeline.

Reference docs:

* [Getting started and overview](docs/index.md)
* [Configuration](docs/configuration.md)
* [App lifecycle](docs/app-lifecycle.md)
* [Pipeline](docs/pipeline.md)
* [Data model](docs/data-model.md)
* [Sources overview](docs/sources-general.md)
* [Sinks overview](docs/sinks-general.md)
* [Google auth CLI](docs/google-auth-cli.md)

Source-specific docs:

* [Gmail](docs/source-gmail.md)
* [Google Calendar](docs/source-google-calendar.md)
* [Google Drive](docs/source-google-drive.md)
* [Fio](docs/source-fio.md)
* [Faktury Online](docs/source-faktury-online.md)
* [Home Assistant](docs/source-home-assistant.md)
* [Nordigen](docs/source-nordigen.md)
* [Mock source](docs/source-mock.md)

Sink-specific docs:

* [Webhook](docs/sink-webhook.md)
* [SSE](docs/sink-sse.md)
* [HTTP Pull](docs/sink-http-pull.md)
* [Command](docs/sink-command.md)
* [Win11 Toast](docs/sink-win11toast.md)

## AI Disclaimer

This project is built using AI. It may contain inaccuracies or errors. Please consider this and do not rely on it for critical decisions.
