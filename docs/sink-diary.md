# Diary Sink

The Diary sink writes incoming events as daily raw JSONL files and maintains a diary tree with editable daily notes, generated daily/weekly/monthly summaries, and convenience symlinks.

It is modeled after the [Folder sink](sink-folder.md), but uses diary rotation rules and reconciliation instead of acting as a Folder mode.

## Getting Started

```yaml
sink:
  diary:
    type: diary
    path: "./data/diary"
    timezone: "UTC"      # optional; defaults to UTC
    cutoff_time: "04:00" # optional
```

Raw events are appended under `raw/YYYY-MM-DD.md` using the same JSONL event envelope as the Folder sink. The date is resolved by the diary cutoff time, so with the default `04:00` cutoff an event at `03:30` belongs to the previous diary day.

## Layout

```text
raw/YYYY-MM-DD.md
user/YYYY-MM-DD.md
daily/YYYY-MM-DD.md
weekly/YYYY-Www.md
monthly/YYYY-MM.md

today.md
yesterday.md
last_week.md
last_month.md

.diary/lock
```

`user/YYYY-MM-DD.md` files are for editable notes. The sink creates the current diary day's user note only if it is missing and never overwrites existing user notes or generated summaries.

On Windows, the Diary sink is disabled at runtime unless Inboxclaw is running with administrator privileges, because the sink maintains convenience symlinks (`today.md`, `yesterday.md`, `last_week.md`, `last_month.md`).

## Reconciliation

On startup and periodically while running, the sink reconciles the diary tree from the filesystem:

- creates required directories and the current user note
- generates missing daily summaries through the last closed day
- generates completed ISO weekly summaries and completed monthly summaries
- updates convenience symlinks atomically
- preserves existing summary files

By default, daily summaries concatenate the previous daily artifact, user notes, and raw events. Weekly summaries concatenate the previous weekly artifact and the seven daily artifacts for that ISO week. Monthly summaries concatenate the previous monthly artifact and all daily artifacts in the month. Missing inputs and empty files are rendered explicitly so later summarizers can distinguish missing data from empty data.

Daily backfill is bounded by `max_backfill_days`. Existing summaries are never regenerated, but reconciliation rechecks the bounded window for gaps so a crash after creating a later artifact does not strand an earlier missing one. Weekly/monthly reconciliation covers periods that overlap that same window. If all daily inputs for a weekly or monthly summary are missing or skipped, the sink writes a skipped marker for that period.

## LLM Summarization

Set `summary_mode: llm` to replace the concatenation artifact with an LLM-generated Markdown memory summary. The sink uses the OpenAI Python SDK against an OpenAI-compatible chat completions endpoint. API calls run through the async reconciliation path used by sink startup and the background loop.

```yaml
sink:
  diary:
    type: diary
    path: "./data/diary"
    timezone: "UTC"
    summary_mode: llm
```

LLM connection settings can be provided directly in config, but are intended to come from environment variables:

```bash
DIARY_LLM_ENDPOINT_URL=https://api.openai.com/v1
DIARY_LLM_API_KEY=...
DIARY_LLM_MODEL=gpt-4.1
DIARY_LLM_EFFORT=medium
DIARY_LLM_TIMEOUT=2m
DIARY_LLM_MAX_RETRIES=2
```

`OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL` are also accepted as fallbacks for the matching `DIARY_LLM_*` variables.

### Provider Examples

The Diary sink uses the OpenAI Python SDK with the Chat Completions API, so any OpenAI-compatible provider can be used by changing the base URL, API key, and model name. Prefer the `DIARY_LLM_*` variables when configuring the diary sink so other OpenAI-based tools on the same machine do not accidentally inherit the diary model.

OpenAI:

```bash
DIARY_LLM_ENDPOINT_URL=https://api.openai.com/v1
DIARY_LLM_API_KEY=${OPENAI_API_KEY}
DIARY_LLM_MODEL=gpt-5
DIARY_LLM_EFFORT=medium
```

OpenAI's own SDK also reads `OPENAI_API_KEY` from the environment, and current OpenAI documentation lists GPT-5-family models for `/v1/chat/completions`. You can omit `DIARY_LLM_ENDPOINT_URL` for the default OpenAI endpoint. See the OpenAI quickstart and model endpoint documentation:

- https://platform.openai.com/docs/quickstart/make-your-first-api-request
- https://platform.openai.com/docs/models/default-usage-policies-by-endpoint

DeepSeek:

```bash
DIARY_LLM_ENDPOINT_URL=https://api.deepseek.com
DIARY_LLM_API_KEY=${DEEPSEEK_API_KEY}
DIARY_LLM_MODEL=deepseek-v4-flash
```

DeepSeek documents its OpenAI-format base URL as `https://api.deepseek.com`. Current DeepSeek V4 model IDs include `deepseek-v4-flash` and `deepseek-v4-pro`; older `deepseek-chat` and `deepseek-reasoner` names were scheduled for deprecation on July 24, 2026. Check DeepSeek's model page before choosing a model for long-term use:

- https://api-docs.deepseek.com/quick_start/pricing/

OpenRouter:

```bash
DIARY_LLM_ENDPOINT_URL=https://openrouter.ai/api/v1
DIARY_LLM_API_KEY=${OPENROUTER_API_KEY}
DIARY_LLM_MODEL=anthropic/claude-sonnet-4
```

OpenRouter documents the OpenAI SDK base URL as `https://openrouter.ai/api/v1`. Model IDs use provider/model slugs such as `openai/gpt-4o`, `anthropic/claude-sonnet-4`, or latest aliases such as `~openai/gpt-latest`. Browse the catalog or use OpenRouter's models endpoint for exact current slugs:

- https://openrouter.ai/docs/quickstart
- https://openrouter.ai/docs/guides/overview/models

`llm_effort` is passed as `reasoning_effort`. Leave it unset for providers or models that do not support OpenAI-style reasoning effort.

The built-in daily, weekly, and monthly prompts are used when no prompt path is configured. To override them, point the sink at Markdown prompt files:

```yaml
sink:
  diary:
    type: diary
    path: "./data/diary"
    summary_mode: llm
    daily_prompt_path: "./prompts/diary-daily.md"
    weekly_prompt_path: "./prompts/diary-weekly.md"
    monthly_prompt_path: "./prompts/diary-monthly.md"
```

Prompt changes do not invalidate or regenerate previous summaries. The sink still preserves any existing `daily/`, `weekly/`, or `monthly/` artifact and only generates missing periods.

### Prompt Placeholders

Prompt files may include placeholders using double braces. Whitespace inside the braces is allowed. Unknown placeholders are left unchanged.

| Placeholder | Daily | Weekly | Monthly | Description |
|:--|:--:|:--:|:--:|:--|
| `{{DATE}}` | yes | no | no | Diary date being summarized, formatted as `YYYY-MM-DD`. |
| `{{WEEK_ID}}` | no | yes | no | ISO week id, formatted as `YYYY-Www`. |
| `{{MONTH_ID}}` | no | no | yes | Month id, formatted as `YYYY-MM`. |
| `{{START_DATE}}` | no | yes | yes | First date covered by the weekly or monthly summary. |
| `{{END_DATE}}` | no | yes | yes | Last date covered by the weekly or monthly summary. |

The source material is sent to the model as a separate user message. Prompt files should describe how to summarize the input; they do not need an input placeholder.

## Configuration Reference

| Parameter | Type | Default   | Description |
|:--|:--|:----------|:--|
| `type` | `string` | —         | Must be `diary`. |
| `path` | `string` | Required  | Diary root directory. |
| `match` | `string\|list` | `"*"`     | Event type filter. |
| `cutoff_time` | `string` | `"04:00"` | Diary day rotation time in `HH:MM` or `HH:MM:SS` format. |
| `timezone` | `string\|null` | UTC       | Timezone for cutoff resolution; use `"UTC"` or an IANA timezone name. `"local"` is not supported. |
| `lock_timeout` | interval | `"30s"`   | Maximum time to wait for the diary lock. |
| `max_backfill_days` | integer | `3`       | Maximum daily reconciliation window. |
| `summary_mode` | `string` | `"concat"` | `concat` writes deterministic input artifacts; `llm` generates summaries with an OpenAI-compatible LLM. |
| `llm_endpoint_url` | `string\|null` | env       | OpenAI-compatible base URL; also read from `DIARY_LLM_ENDPOINT_URL` or `OPENAI_BASE_URL`. |
| `llm_api_key` | `string\|null` | env       | API key; also read from `DIARY_LLM_API_KEY` or `OPENAI_API_KEY`. |
| `llm_model` | `string\|null` | env       | Model name; also read from `DIARY_LLM_MODEL` or `OPENAI_MODEL`. |
| `llm_effort` | `string\|null` | env       | Optional reasoning effort; read from `DIARY_LLM_EFFORT` when omitted. |
| `llm_timeout` | interval | `"2m"`     | LLM request timeout; also read from `DIARY_LLM_TIMEOUT`. |
| `llm_max_retries` | integer | `2`       | LLM SDK retry count; also read from `DIARY_LLM_MAX_RETRIES`. |
| `daily_prompt_path` | `string\|null` | `null`    | Markdown file overriding the built-in daily prompt. |
| `weekly_prompt_path` | `string\|null` | `null`   | Markdown file overriding the built-in weekly prompt. |
| `monthly_prompt_path` | `string\|null` | `null`  | Markdown file overriding the built-in monthly prompt. |
