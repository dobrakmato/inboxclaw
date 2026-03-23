# Step 4: Run Your Pipeline

Your sources and sinks are configured. Time to start Inboxclaw for real and make sure everything is working.

## Start Inboxclaw

```bash
inboxclaw listen
```

Inboxclaw will start all your configured sources and sinks. You should see log output showing each source starting its polling loop and each sink becoming ready to deliver.

## Verify Events Are Flowing

### Check Recent Events

In a separate terminal, run:

```bash
inboxclaw events
```

This shows the latest events that have been processed and published. You should see events from your configured sources appearing here.

### Check Pending Events

If you have coalescing enabled, some events may be waiting to settle before being published:

```bash
inboxclaw pending-events
```

### Check System Status

For a full overview of what's running:

```bash
inboxclaw status
```

This shows whether the service is running, any recent errors, and database statistics.

## Troubleshooting

### No events appearing?

- **Check your source configuration.** Make sure API tokens and credentials are correct.
- **Check the poll interval.** If you set `poll_interval: "1h"`, you'll need to wait up to an hour for the first poll. Try a shorter interval like `"1m"` for testing.
- **Google sources:** Make sure you ran `inboxclaw google auth` first. See the [Google Auth CLI Guide](../google-auth-cli.md).
- **Check the logs.** Look for error messages in the terminal output. If running as a service, use `inboxclaw logs`.

### Events appear but aren't being delivered?

- **Check your sink configuration.** Make sure URLs, tokens, and paths are correct.
- **Check the match filter.** If your sink has a `match` filter, make sure it matches the event types your sources produce.
- **Webhook sinks:** Make sure the target server is running and reachable. Check for authentication errors in the logs.
- **Command sinks:** Make sure the command path is correct and the program is executable.

## Running as a Background Service

For long-term use, you'll want Inboxclaw running as a background service that starts automatically. On Linux with systemd:

```bash
# The CLI can manage the service for you
inboxclaw restart
```

The `restart` command validates your configuration before restarting, so you won't accidentally break a running service with a bad config.

For more details on the application lifecycle, see [App Lifecycle](../app-lifecycle.md).

## Next Step

→ [Step 5: Maintenance](step-5-maintenance.md) — keeping Inboxclaw running smoothly over time.
