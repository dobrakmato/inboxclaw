# Step 5: Maintenance

Inboxclaw is designed to run unattended, but here are the things you'll want to do occasionally to keep it healthy.

## Updating Inboxclaw

Check for updates and install them:

```bash
inboxclaw update
```

This pulls the latest code from the repository and installs any new dependencies. If you're running Inboxclaw as a service, restart it afterward:

```bash
inboxclaw restart
```

## Restarting Safely

The `restart` command validates your configuration before restarting. This means if you made a typo in `config.yaml`, it will catch it and refuse to restart — keeping your current working version running.

```bash
inboxclaw restart
```

Always use this instead of manually killing and restarting the process.

## Checking Status

Get a quick overview of how things are running:

```bash
inboxclaw status
```

This shows service status, recent errors, and database statistics like how many events have been processed.

## Viewing Logs

If running as a systemd service on Linux:

```bash
# Show recent logs
inboxclaw logs

# Follow logs in real-time
inboxclaw logs -f

# Show more lines
inboxclaw logs -n 100
```

## Database Management

Inboxclaw stores events in a local SQLite database. The `retention_days` setting in your config controls how long events are kept:

```yaml
database:
  retention_days: 7
  db_path: ./data/data.db
```

Old events are automatically cleaned up. If you need to change the retention period, update the config and restart.

## Re-authenticating Google

Google OAuth tokens expire periodically. If your Google sources stop working, re-run the authentication:

```bash
inboxclaw google auth
```

## Further Reading

- [CLI Reference](../cli.md) — full list of all available commands.
- [Configuration](../configuration.md) — all configuration options explained.
- [App Lifecycle](../app-lifecycle.md) — how Inboxclaw starts, runs, and shuts down.
