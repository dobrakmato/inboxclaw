![Inboxclaw Logo](assets/logo.png)

# Inboxclaw

**The event inbox for your AI assistant.**

Your inbox, calendar, files, bank, and devices are all producing signals. Inboxclaw turns them into one clean, durable event stream your assistant can actually use.

Connect the services you already use — email, calendar, cloud storage, banking, device state — and Inboxclaw watches them for you. Instead of every assistant, script, or automation having to integrate with Gmail, Google Calendar, Google Drive, Home Assistant, or bank APIs separately, you connect each source once and consume everything from one place.

## Who is it for

Inboxclaw is built for self-hosters, agent builders, [OpenClaw](getting-started-openclaw.md) users, and automation-heavy power users. It is not trying to be a giant enterprise message bus — it is a lightweight event inbox for personal automation and assistant-facing workflows.

## How it works

The system works in three stages:

1. **Sources watch external systems.** Gmail, Calendar, Drive, bank APIs, Home Assistant — each source detects changes using polling, cursors, or cached snapshots and converts them into normalized events with a consistent shape: event type, entity ID, timestamp, payload, metadata.

2. **The pipeline deduplicates and stores.** Events are checked against `(source_id, event_id)` to avoid double-writes, then persisted in SQLite *before* anything is delivered. For noisy streams like repeated file edits, optional [coalescing](coalescing.md) collapses rapid bursts into one meaningful update.

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

**The simplest mental model: Inboxclaw is OpenClaw's event inbox.**

See the [Inboxclaw + OpenClaw guide](getting-started-openclaw.md) for setup instructions.

## Current shape

In very active development — some features may break at any time. A good fit for personal automation, self-hosted assistant backends, side projects, and lightweight integration glue. Not yet recommended as a conservative "bet the company on it" platform.

## Getting Started

Follow the **[Onboarding Tutorial](onboarding/index.md)** for a complete step-by-step walkthrough:

1. [Install and run Inboxclaw](onboarding/step-1-install.md) with a minimal test config
2. [Configure sources](onboarding/step-2-sources.md) — connect Gmail, your bank, calendar, etc.
3. [Configure sinks](onboarding/step-3-sinks.md) — decide where events go
4. [Run your pipeline](onboarding/step-4-run.md) — verify everything works
5. [Maintenance](onboarding/step-5-maintenance.md) — updates, restarts, and upkeep

## Core Concepts

- **Source**: connector that reads external changes and emits normalized events. This is the point where many incompatible APIs become one consistent internal language.
- **Pipeline**: deduplicates and stores events durably, then notifies sinks. Durability happens before delivery.
- **Sink**: connector that delivers matched events to consumers. Each sink has its own delivery semantics — webhook pushes and retries, command executes locally, HTTP pull lets consumers fetch and confirm batches, SSE streams only live events.
- **[Coalescing](coalescing.md)**: optional reduction of noisy update bursts into the latest meaningful state. Useful for "same thing changed ten times in thirty seconds" — not for transactional events where every occurrence matters.

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
- [Command](sink-command.md)
- [Win11 Toast](sink-win11toast.md)

## Additional Guides

- [Configuration](configuration.md)
- [CLI Reference](cli.md)
- [Key/Value Storage](kv-general.md)
- [Google Auth CLI](google-auth-cli.md)
