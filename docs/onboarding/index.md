# Onboarding Tutorial

Welcome! This tutorial walks you through setting up Inboxclaw from scratch. By the end, you'll have a working pipeline that watches your accounts and delivers events to your AI assistant or any other tool.

Work through the steps in order — each one builds on the previous.

## Steps

### [Step 1: Install and Run Inboxclaw](step-1-install.md)

Get Inboxclaw running on your machine with a minimal test configuration. You'll also get a quick introduction to the CLI — the command-line tool you'll use to manage everything.

### [Step 2: Configure Sources](step-2-sources.md)

Connect your first data source — Gmail, your bank, Google Calendar, or anything else Inboxclaw supports. Learn how the configuration file works, what sources are, and how coalescing keeps things tidy.

### [Step 3: Configure Sinks](step-3-sinks.md)

Decide where your events should go. If you're using OpenClaw, this is where you choose between the Command and Webhook methods. If you're building your own integration, you'll set up a webhook, SSE stream, or HTTP pull endpoint.

### [Step 4: Run Your Pipeline](step-4-run.md)

Start Inboxclaw for real and verify that events flow from your sources to your sinks. Learn how to monitor what's happening and troubleshoot common issues.

### [Step 5: Maintenance](step-5-maintenance.md)

Keep things running smoothly. Learn how to update Inboxclaw, restart safely, and manage your data over time.
