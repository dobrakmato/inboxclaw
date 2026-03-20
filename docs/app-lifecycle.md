# App Lifecycle

This page describes what the ingest pipeline does from process start to shutdown.

If you are new to the project, read this first, then continue with [Pipeline](pipeline.md), [Sources](sources-general.md), and [Sinks](sinks-general.md).

## Startup Sequence

On startup, the FastAPI lifespan (`src/app.py`) performs these steps:

1. Load configuration from `config.yaml` (or a CLI-provided path).
2. Initialize the database session maker.
3. Build a shared `AppServices` container with:
   - FastAPI app instance
   - parsed config
   - database session maker
   - event notifier
4. Initialize sources from `config.sources`.
5. Initialize sinks from `config.sink`.
6. Start the retention cleanup background task.

## Source and Sink Initialization

Initialization is handled in `src/initialization.py`.

- Each configured source/sink gets a **name** from the YAML key.
- `type` selects the concrete implementation class.
- If `type` is omitted in config, the config layer resolves it from the key name.
- A corresponding `sources` / `sinks` row is ensured in the database.

Some components start background loops immediately during initialization (for example polling or delivery workers). These loops are always registered through `AppServices.add_task(...)` so they can be tracked and stopped cleanly.

## Runtime

During runtime:

- Sources collect and emit events.
- Events are persisted and deduplicated by the pipeline writer.
- Sinks consume matching events from the database and deliver them outward.
- The notifier wakes up real-time or worker-style sinks when new events arrive.

See [Pipeline](pipeline.md) for the event flow details.

## Shutdown

On application shutdown (FastAPI lifespan exit):

1. The app calls `services.stop_tasks()`.
2. Registered background tasks are cancelled.
3. Cancellation is awaited to ensure graceful shutdown.

This prevents orphan background jobs and keeps shutdown deterministic.
