# Guidelines for Ingest Pipeline Project

## Principles

1. **Strong Typing**
   - Use Python's `typing` module for all function signatures and class definitions.
   - Prefer Pydantic models for configuration and data structures.
   - Use `SQLAlchemy`'s modern ORM features (e.g. `Mapped`, `mapped_column`) where possible.
   - Avoid `Any` when a more specific type can be used.

2. **Clean Design**
   - Separate concerns into distinct modules (sources, sinks, pipeline, database).
   - Use a "service container" or `AppServices` object to encapsulate shared dependencies like the database session maker, the FastAPI application, and the event notifier.
   - Follow the established architecture in `ARCHITECTURE.md`.
   - Keep functions small and focused on a single task.

3. **High Test Coverage**
   - Write unit tests for core logic (e.g. `Coalescer`, `EventNotifier`).
   - Write integration tests for sources and sinks using `fastapi.testclient.TestClient`.
   - Aim for high coverage, including success and error scenarios.
   - Use a separate in-memory SQLite database for testing to ensure isolated and fast tests.

4. **Modern Practices**
   - Use FastAPI's `lifespan` for application startup and shutdown.
   - Avoid deprecated functions (e.g. `datetime.utcnow()` should be `datetime.now(timezone.utc)`).
   - Use Pydantic's `v2` syntax for models and validation.
   - Organize files in a clear, modular structure under `src/`.
   - **Background Task Management**: Always use `AppServices.add_task(coroutine)` to register background tasks (polling, delivery loops) instead of `asyncio.create_task()` directly. This ensures tasks are tracked and correctly shut down.
   - **Configurable Intervals**: Support human-readable interval strings (e.g., "5s", "1m", "1d") in configuration using `pytimeparse`.

5. **Logging**
   - Use the standard `logging` module.
   - Provide informative log messages that include context where helpful (e.g., source name, event IDs).

6. **Effective Documentation**
   - **Human-Centric Tone**: Write for humans, not for computers. Explain *why* a feature exists and *what* it accomplishes before detailing *how* to use it.
   - **Explain Core Concepts**: Never assume the reader knows internal jargon (e.g., "coalescing", "batching"). Provide a dedicated section explaining these concepts in plain English with real-world examples.
   - **Pros & Cons**: Always include a "Pros and Cons" or "When to use" section at the top to help the reader decide if the component fits their needs.
   - **Progressive Disclosure of Complexity**: 
     - Start with a **Minimal Configuration** example (the simplest way to get it running).
     - Show **Implicit vs Explicit** configuration patterns where applicable.
     - Provide a **Full Configuration** example showing all possible options.
   - **Clear API Examples**: Use consistent, realistic examples for requests and responses. Include notes on what to do based on response fields (e.g., "if `remaining_events > 0`, call again").
   - **Event Definitions**: Always include a "Event Definitions" section in source documentation, using a table for `Type`, `Entity ID`, and `Description`. Keep this in sync with the source implementation.
