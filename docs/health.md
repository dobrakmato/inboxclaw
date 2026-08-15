# Source Health

Inboxclaw sources report whether their real operational work is succeeding. Health checking does not run a second copy of a source poll, consume extra API quota, advance cursors, or block event ingestion.

## States

| State | Meaning |
|:--|:--|
| `starting` | The source has not completed its first operation in this process. This is not treated as a failure. |
| `healthy` | The source has completed an operational cycle and has no confirmed failure. |
| `unhealthy` | An actionable failure occurred, transient failures were confirmed on two consecutive cycles, the source runner stopped, or a polling source stopped reporting within its expected interval. |

A successful poll with no new events is healthy. It proves that Inboxclaw successfully checked the upstream system; it does not require the upstream system to contain a new event.

## Failure confirmation and flapping

Inboxclaw does not change a source to `unhealthy` because of one transient
request failure. Connectivity, timeout, upstream, internal, and partial
processing failures require two consecutive failed operational cycles. A
success between them clears the pending failure without emitting an unhealthy
or recovered event.

Actionable or locally confirmed failures are immediate: configuration,
authentication, authorization, expired access, rate limiting, persisted
backoff, a stopped runner, and watchdog stale/not-reporting failures. Once a
source is confirmed unhealthy, one complete successful cycle recovers it.

The HTTP response exposes an unconfirmed first failure as `pending_failure` on
the source row. `inboxclaw healthcheck` shows it as a warning, while the
aggregate remains at its previously confirmed state.

## What each source verifies

Health comes from the same work that produces events:

| Source | Healthy means |
|:--|:--|
| Gmail | Credentials, Gmail history retrieval, required message metadata, event writes, and cursor handling completed. |
| Google Calendar | Every configured calendar completed its synchronization cycle. A failure in one calendar makes the source unhealthy. |
| Google Drive | The real Drive change-log cycle, including required file reads, event writes, cache changes, and cursor handling, completed. |
| Google Health | Every configured data type was retrieved. Successful types may still write events, but any failed type makes the cycle unhealthy and prevents cursor advancement. |
| Asana | Every configured project was listed and all detected task changes were processed. Partial processing failures are unhealthy. |
| Jira | The configured JQL search and all detected issue changes were processed. Partial processing failures are unhealthy. |
| Faktury Online | Session setup, invoice listing, required invoice details, event writes, and cache updates completed. |
| Fio | The real transaction request and processing completed. A rate-limit rejection is unhealthy. No separate quota-consuming probe is made. |
| GoCardless / Nordigen | The scheduled token and transaction operation completed. Authentication failures, consent expiry, rate limits, and upstream backoff are unhealthy. |
| Home Assistant | The WebSocket authenticated and the entity subscription was acknowledged. Disconnects and failed reconnects are unhealthy. |
| Filesystem | The configured directory exists and the baseline/periodic scan or watcher is operating. |
| Mock | Event generation and writing completed. |

Inboxclaw also watches source runner tasks locally. This watchdog never calls an external API; it only detects a stopped task or a polling source that has not reported for more than twice its expected interval plus a small grace period.

## CLI

```bash
inboxclaw healthcheck
```

Use `--json` for machine-readable output and `--timeout SECONDS` to change the HTTP timeout.

Exit codes:

| Code | Meaning |
|:--|:--|
| `0` | Every source is healthy. |
| `1` | At least one source is unhealthy. |
| `2` | Sources are still starting, configuration is invalid, or the service cannot be reached. |

`inboxclaw status` includes the same per-source state alongside service, log, version, and database information.

## HTTP endpoints

`GET /healthcheck` returns the aggregate and per-source state. It returns HTTP `503` only when a source is confirmed unhealthy. A genuine `starting` state returns HTTP `200`.

`GET /healthcheck/live` is process liveness only. It returns `200` when the Inboxclaw HTTP process is responsive, regardless of source failures.

## Internal health events

Inboxclaw creates a reserved database source named `inboxclaw`. It has no connector or runner; it exists so internal messages can use the normal event pipeline and sinks.

The health registry emits:

- `inboxclaw.source.unhealthy` when a source first becomes unhealthy
- `inboxclaw.source.recovered` when that source later becomes healthy

These generic transitions are additive. They do not replace more detailed
connector events such as Nordigen's `nordigen.error.*` events.

The failing source name is the event `entity_id`. Repeated failed polls do not create repeated events, and a single transient failure followed by success creates no transition events. The current health state and pending-failure counter remain in memory; only a small notification latch is stored in the existing per-source KV storage so a restart does not repeat an unchanged unhealthy notification.

To route these messages, configure any sink with a match such as:

```yaml
match: inboxclaw.source.*
```
