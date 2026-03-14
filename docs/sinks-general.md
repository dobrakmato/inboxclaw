# Sinks in Ingest Pipeline

Sinks are the exit points of data from the system. They are responsible for delivering processed events to external systems (via webhooks, SSE, or direct HTTP polling).

### How they work

Sinks operate based on **Matches**. Each sink is configured with a set of `event_type` patterns (e.g., `gmail.*`, `drive.file_change`). When an event matches one of these patterns, the sink takes responsibility for delivering that event.

There are three primary delivery models:

1.  **Push (Webhook)**: The system actively sends the event data to a pre-configured URL.
2.  **Streaming (SSE)**: Clients connect to the system and receive events in real-time as they are written to the database.
3.  **Pull (HTTP Pop)**: Clients periodically poll the system for a batch of new events. The system marks them as "processed" to ensure each event is delivered only once per client.


### Delivery Reliability

-   **Webhooks**: Include a retry mechanism for failed delivery attempts.
-   **HTTP Pop**: Ensures events are not lost by requiring them to be fetched in batches and marked as processed.
-   **SSE**: Provides real-time delivery but requires a stable connection; missed events must be handled by the client.

### Pros and Cons of Sinks

**Pros:**
-   **Flexibility**: Support multiple delivery protocols to suit different client needs.
-   **Scalability**: Separate workers handle webhook retries; SSE and Pop endpoints are efficient.
-   **Filtering**: Clients only receive the events they are interested in.

**Cons:**
-   **Complexity**: Managing delivery state and retries adds overhead.
-   **Consistency**: Ensuring "exactly-once" delivery can be challenging (this system focuses on "at-least-once").

### Configuration Example

#### Webhook (Push)
```yaml
sink:
  my_webhook:
    type: webhook
    match: "gmail.*"
    url: "https://api.example.com/webhooks/gmail"
    max_retries: 5
```

#### SSE (Streaming)
```yaml
sink:
  live_updates:
    type: sse
    match: "*"
    path: "/events"
```

#### HTTP Pull
```yaml
sink:
  batch_processor:
    type: http_pull
    match: ["drive.file_change", "docs.*"]
    path:
      extract: "/get-batch"
      mark_processed: "/ack-batch"
```
