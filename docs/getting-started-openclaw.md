# Getting Started: Inboxclaw + OpenClaw

## What Are We Setting Up?

**Inboxclaw** watches your digital life — emails, bank transactions, calendar events — and picks up anything new. **OpenClaw** is your AI assistant that decides what to do with that information.

By connecting them, you get an AI assistant that automatically reacts to real-world events. For example:

- A new email arrives → OpenClaw summarizes it and sends you a message on Telegram.
- A bank transaction posts → OpenClaw logs it in your budget tracker.
- A meeting gets cancelled → OpenClaw updates your to-do list.

::: tip Looking for the full onboarding tutorial?
This page focuses specifically on connecting Inboxclaw to OpenClaw. For a complete step-by-step walkthrough — from installation to running your first pipeline — see the [Onboarding Tutorial](onboarding/index.md).
:::

## How Does the Connection Work?

There are two ways to connect Inboxclaw to OpenClaw:

1. **Command** — Inboxclaw runs the OpenClaw CLI directly on your computer, inserting events as real messages into your conversation.
2. **Webhook** — Inboxclaw sends events over the network to OpenClaw's hook system, which uses the heartbeat mechanism to process them.

Both get the job done. The right choice depends on how you want your AI to handle incoming events.

### The Key Difference

The real distinction is on the **OpenClaw side** — how the AI receives and processes the information:

- **Command** inserts a real user message into your conversation. The AI sees it as part of the ongoing chat, with full context of everything you've discussed before. It's like walking up to someone mid-conversation and handing them a note — they read it with all the context of what you've been talking about.

- **Webhook** works through OpenClaw's hook and heartbeat system. This gives you much more control: you can route events to separate AI agents, customize wake behavior, choose whether the AI acts immediately or waits, and keep different types of events in separate sessions. It's like having a mailroom that sorts incoming letters to the right department.

### Quick Comparison

| | Command | Webhook |
|:--|:--|:--|
| **How the AI sees it** | As a message in your main conversation | As a hook event processed via the heartbeat system |
| **Context** | Full conversation history | Separate session (configurable) |
| **Customization** | Limited — one conversation, one flow | High — separate agents, custom routing, wake modes |
| **Best for** | Simple setups, everything on one machine | Advanced setups, multi-agent workflows |
| **Setup difficulty** | Easier — just point to the OpenClaw program | Moderate — requires configuring hooks on both sides |
| **Reliability** | Very high — events are queued and retried locally | High — retries on failure, depends on network |
| **Works offline?** | Yes — everything is local | No — needs network connectivity |

> **Not sure which to pick?** Start with the **Command** method. It's simpler to set up and keeps everything in one conversation. You can always switch to Webhook later when you need more control.

---

## Option 1: Command (Recommended for Beginners)

### How It Works

Every time Inboxclaw detects a new event (like a new email), it runs the OpenClaw CLI on your computer and passes the event as a message. The event becomes part of your main conversation — the AI reads it with full context of your chat history and can respond naturally.

If something goes wrong — say OpenClaw is busy or crashes — Inboxclaw saves the event and tries again later. Nothing gets lost.

### Pros and Cons

✅ **Pros:**
- Simple to set up — no network configuration needed.
- Events become part of your conversation — the AI has full context when responding.
- Extremely reliable — events are saved to a local queue, surviving crashes and restarts.
- Works completely offline.

❌ **Cons:**
- Both Inboxclaw and OpenClaw must be installed on the same computer.
- Limited flexibility — everything goes into one conversation with one flow.
- Slightly slower since it starts a new process for each event.

### Setup

Add the following to your `config.yaml` file under the `sinks` section:

```yaml
sinks:
  openclaw:
    type: 'command'
    command:
      - "/path/to/openclaw"  # Replace with the actual path to your OpenClaw program
      - "agent"
      - "--session-id"
      - "main"
      - "--deliver"
      - "--message"
      - "New event received: $root"
```

**What each part means:**

| Part | What it does |
|:--|:--|
| `/path/to/openclaw` | The location of the OpenClaw program on your computer. Replace this with your actual path. |
| `agent` | Tells OpenClaw to act as your AI assistant. |
| `--session-id main` | Sends the event to your main conversation, so the AI has full context of your history. |
| `--deliver` | Allows the AI to reply back to you (for example, via Telegram or WhatsApp). |
| `--message "..."` | The actual event content. `$root` is a placeholder that Inboxclaw automatically replaces with the real event data. |

That's it! Once this is saved, Inboxclaw will start sending events to OpenClaw every time something new comes in.

> For the full list of command sink options (batching, retries, TTL), see the [Command Sink Reference](sink-command.md).

---

## Option 2: Webhook (For Advanced Setups)

### How It Works

When Inboxclaw detects a new event, it sends a message over the network to OpenClaw's hook system. OpenClaw then processes the event through its heartbeat mechanism — this is the same system OpenClaw uses internally to check on things periodically.

The big advantage here is flexibility. You can route different events to different AI agents, control whether the AI reacts immediately or waits for its next scheduled check, and keep event processing separate from your main conversation.

### Pros and Cons

✅ **Pros:**
- Highly customizable — route events to separate agents, control wake behavior, use dedicated sessions.
- Fast — events are delivered almost instantly over the network.
- Flexible architecture — Inboxclaw and OpenClaw can run on different machines.
- Scales well for complex multi-agent workflows.

❌ **Cons:**
- More setup — you need to configure hooks on both the Inboxclaw and OpenClaw sides.
- Requires network connectivity between the two systems.
- Events are processed outside your main conversation context (though summaries can be posted back).

### Setup

This method requires two steps: configuring OpenClaw to accept incoming hooks, and configuring Inboxclaw to send them.

#### Step 1: Enable Hooks in OpenClaw

In your OpenClaw configuration, enable the hooks feature and set a secret token (like a password) to keep things secure:

```json
{
  "hooks": {
    "enabled": true,
    "token": "your-secret-token",
    "defaultSessionKey": "hook:inboxclaw",
    "mappings": [
      {
        "match": { "path": "inboxclaw" },
        "action": "wake",
        "wakeMode": "now",
        "allowUnsafeExternalContent": true
      }
    ]
  }
}
```

> **What's the token for?** It's a shared secret between Inboxclaw and OpenClaw. It makes sure only Inboxclaw (and not some random stranger) can send events to your AI assistant.

#### Step 2: Tell Inboxclaw Where to Send Events

Add the following to your `config.yaml`:

```yaml
sinks:
  openclaw_webhook:
    type: "webhook"
    url: "http://127.0.0.1:18789/hooks/inboxclaw"
    headers:
      Authorization: "Bearer your-secret-token"
    payload:
      message: "New event: $root"
```

> **Important:** The `your-secret-token` value must be the same in both configurations. If they don't match, OpenClaw will reject the messages.

> For the full list of webhook sink options (retries, headers, payload rewriting, TTL), see the [Webhook Sink Reference](sink-webhook.md).

---

## Understanding Webhook Modes

If you chose the Webhook method, you can control *how* OpenClaw reacts to each event. OpenClaw's hook system offers three approaches:

### Wake — "Add this to my awareness"

**Endpoint:** `/hooks/wake`

The event is queued as a system event in OpenClaw's main session. The AI becomes aware of it on its next heartbeat — the periodic check OpenClaw does on its own schedule. If you set `mode: "now"`, it triggers an immediate heartbeat so the AI processes it right away.

**Good for:** Low-priority updates, background information gathering, things that don't need a dedicated agent run.

**Example:** A daily weather report, a news digest, or a non-urgent email notification.

### Agent — "Spin up a dedicated agent for this"

**Endpoint:** `/hooks/agent`

OpenClaw starts a dedicated agent session just for this event. The agent processes it independently, posts a summary back to your main session, and can optionally deliver a response to your messaging channel (Telegram, WhatsApp, etc.). You can even route it to a specific agent with its own tools and configuration.

**Good for:** Time-sensitive events that need immediate, focused processing — especially when you want a specialized agent to handle them.

**Example:** An urgent email from your boss, a suspicious bank transaction, or a meeting starting in 5 minutes.

### Mapped — "Route it based on rules" (Recommended)

**Endpoint:** `/hooks/<your-custom-name>` (e.g., `/hooks/inboxclaw`)

Instead of choosing Wake or Agent from Inboxclaw's side, you define routing rules in OpenClaw's configuration. OpenClaw decides how to handle each event based on those rules. This is the approach used in the setup example above.

**Good for:** Most setups. It keeps your Inboxclaw configuration simple and lets you fine-tune the AI's behavior from one central place — the OpenClaw config.

**Example:** You could set up rules that wake for routine emails but spin up a dedicated agent for bank transactions or urgent messages.

---

## What's Next?

- **New to Inboxclaw?** Follow the [Onboarding Tutorial](onboarding/index.md) for a complete walkthrough from installation to your first running pipeline.
- **Ready to add data sources?** See [Sources Overview](sources-general.md) to connect Gmail, your bank, calendar, and more.
- **Want to fine-tune delivery?** Check the [Command Sink](sink-command.md) or [Webhook Sink](sink-webhook.md) reference for all available options.
