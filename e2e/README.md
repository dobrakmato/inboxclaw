# End-to-End (E2E) Testing

This directory contains the end-to-end tests for the Ingest Pipeline. These tests verify the entire system's behavior by spinning up the application with specific configurations and observing its effects on various sinks.

## Approach

Our E2E tests follow these principles:

1.  **Isolation**: Each test case runs with its own isolated configuration and SQLite database under `e2e/runs/`.
2.  **Real-World Simulation**: We start the actual application process using `subprocess` to ensure we are testing the real code path.
3.  **Mock Sources**: We use the `mock` source to generate predictable event streams for testing.
4.  **Mock Receivers**: For sinks like Webhooks, the test suite spins up a temporary FastAPI server to receive and verify the outgoing data.
5.  **Clean State**: Before each test, the database is reset, and the application's configuration is pointed to an isolated file using `CONFIG_PATH`.

## Directory Structure

-   `e2e/test_webhook.py`: Tests for the Webhook sink.
-   `e2e/test_sse.py`: Tests for the Server-Sent Events (SSE) sink.
-   `e2e/test_http_pop.py`: Tests for the HTTP Pop sink.
-   `e2e/utils.py`: Common utilities for E2E testing (e.g., `E2EApp`).
-   `e2e/run_e2e.py`: Orchestrator script to run all tests.
-   `e2e/runs/`: (Generated) Temporary directory for test case artifacts (configs and databases).

## How to Run

To run all E2E tests, you can use the provided Python script from the project root:

```bash
python e2e/run_e2e.py
```

This script sets up the necessary environment and executes `pytest` for all E2E suites.

### Manual execution
Alternatively, you can run them via `pytest`:

```powershell
pytest e2e
```

## Troubleshooting

-   **Port Conflicts**: The E2E tests use high port ranges (8100+ for the app, 8200+ for receivers). Ensure no stale processes are running if you see "Address already in use".
-   **Cleanup**: `E2EApp` handles process termination and configuration restoration. On Windows, it uses `taskkill` to ensure the entire process tree is cleaned up.
-   **Timeouts**: If tests fail under heavy load, you may need to increase `time.sleep()` or timeout values in the test files.
