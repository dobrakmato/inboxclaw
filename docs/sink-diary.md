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

Daily summaries concatenate the previous daily artifact, user notes, and raw events. Weekly summaries concatenate the previous weekly artifact and the seven daily artifacts for that ISO week. Monthly summaries concatenate the previous monthly artifact and all daily artifacts in the month. Missing inputs and empty files are rendered explicitly so later summarizers can distinguish missing data from empty data.

Daily backfill is bounded by `max_backfill_days`. Existing summaries are never regenerated, but reconciliation rechecks the bounded window for gaps so a crash after creating a later artifact does not strand an earlier missing one. Weekly/monthly reconciliation covers periods that overlap that same window. If all daily inputs for a weekly or monthly summary are missing or skipped, the sink writes a skipped marker for that period.

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
