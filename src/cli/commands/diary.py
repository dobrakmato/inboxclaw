import asyncio
import os
from datetime import date, datetime, timedelta
from typing import Optional

import click

from src.cli import cli
from src.config import DiarySinkConfig, load_config
from src.sinks.diary import (
    DiaryConfig,
    _month_ids_for_window,
    _weekly_starts_for_window,
    generate_missing_diary_range_async,
    resolve_diary_date,
)
from src.utils.paths import get_project_root


@cli.group()
def diary():
    """Diary maintenance commands."""
    pass


@diary.command("backfill")
@click.option("--config", "config_path", default=None, help="Path to the configuration file.")
@click.option("--sink", "sink_name", default=None, help="Specific diary sink name to use.")
@click.option("--date-from", "date_from_text", default=None, help="First diary date to backfill (YYYY-MM-DD).")
@click.option("--date-to", "date_to_text", default=None, help="Last diary date to backfill (YYYY-MM-DD).")
@click.option("--last-n-days", type=int, default=None, help="Backfill the last N closed diary days.")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation before generating files.")
def backfill(
    config_path: Optional[str],
    sink_name: Optional[str],
    date_from_text: Optional[str],
    date_to_text: Optional[str],
    last_n_days: Optional[int],
    yes: bool,
):
    """Interactively generate missing historical diary summaries."""
    config_path = _resolve_config_path(config_path)

    try:
        config = load_config(config_path)
    except Exception as exc:
        raise click.ClickException(f"Error loading config from {config_path}: {exc}") from exc

    selected_sink_name, sink_config = _select_diary_sink(config.sink, sink_name)

    try:
        diary_config = DiaryConfig.from_sink_config(sink_config)
    except Exception as exc:
        raise click.ClickException(f"Failed to initialize diary configuration: {exc}") from exc

    last_closed_day = _last_closed_diary_day(diary_config)
    start_day, end_day = _resolve_requested_range(
        last_closed_day,
        date_from_text,
        date_to_text,
        last_n_days,
    )

    if end_day > last_closed_day:
        raise click.ClickException(
            f"Date range ends at {end_day.isoformat()}, but the latest closed diary date is "
            f"{last_closed_day.isoformat()}."
        )

    if diary_config.summary_mode == "llm":
        if not diary_config.llm_api_key:
            raise click.ClickException("LLM summary mode requires DIARY_LLM_API_KEY or OPENAI_API_KEY.")
        if not diary_config.llm_model:
            raise click.ClickException("LLM summary mode requires DIARY_LLM_MODEL or OPENAI_MODEL.")

    daily_count = (end_day - start_day).days + 1
    weekly_count = len(list(_weekly_starts_for_window(start_day, end_day)))
    monthly_count = len(list(_month_ids_for_window(start_day, end_day)))
    period_count = daily_count + weekly_count + monthly_count

    click.echo(f"Diary sink: {selected_sink_name}")
    click.echo(f"Root: {diary_config.root}")
    click.echo(f"Range: {start_day.isoformat()} through {end_day.isoformat()}")
    click.echo(f"Summary mode: {diary_config.summary_mode}")
    click.echo(f"Periods to inspect: {daily_count} daily, {weekly_count} weekly, {monthly_count} monthly")
    click.echo("Existing summary files will not be overwritten.")
    if diary_config.summary_mode == "llm":
        click.echo(f"LLM model: {diary_config.llm_model}")
        click.secho(f"Up to {period_count} missing summaries may call the LLM.", fg="yellow")

    if not yes:
        click.confirm("Generate missing historical diary files?", abort=True)

    try:
        result = asyncio.run(
            generate_missing_diary_range_async(
                diary_config,
                selected_sink_name,
                start_day,
                end_day,
            )
        )
    except Exception as exc:
        raise click.ClickException(f"Diary backfill failed: {exc}") from exc

    click.secho("Diary backfill complete.", fg="green")
    click.echo(
        "Daily: "
        f"{result.daily_generated} generated, {result.daily_skipped} skipped, {result.daily_existing} existing"
    )
    click.echo(
        "Weekly: "
        f"{result.weekly_generated} generated, {result.weekly_skipped} skipped, {result.weekly_existing} existing"
    )
    click.echo(
        "Monthly: "
        f"{result.monthly_generated} generated, {result.monthly_skipped} skipped, {result.monthly_existing} existing"
    )


def _resolve_config_path(config_path: Optional[str]) -> str:
    if config_path is not None:
        return config_path
    if os.path.exists("config.yaml"):
        return "config.yaml"
    return str(get_project_root() / "config.yaml")


def _select_diary_sink(
    sink_configs: dict[str, object],
    sink_name: Optional[str],
) -> tuple[str, DiarySinkConfig]:
    diary_sinks = {name: cfg for name, cfg in sink_configs.items() if isinstance(cfg, DiarySinkConfig)}
    if not diary_sinks:
        raise click.ClickException("No diary sinks configured in this instance.")

    if sink_name is not None:
        if sink_name not in diary_sinks:
            raise click.ClickException(f"Diary sink '{sink_name}' not found in configuration.")
        return sink_name, diary_sinks[sink_name]

    if len(diary_sinks) > 1:
        choices = ", ".join(sorted(diary_sinks))
        selected = click.prompt(f"Diary sink ({choices})", type=click.Choice(sorted(diary_sinks)))
        return selected, diary_sinks[selected]

    return next(iter(diary_sinks.items()))


def _last_closed_diary_day(config: DiaryConfig) -> date:
    current_diary_date = resolve_diary_date(datetime.now(config.timezone), config.cutoff_time, config.timezone)
    return current_diary_date - timedelta(days=1)


def _resolve_requested_range(
    last_closed_day: date,
    date_from_text: Optional[str],
    date_to_text: Optional[str],
    last_n_days: Optional[int],
) -> tuple[date, date]:
    if last_n_days is not None:
        if date_from_text is not None or date_to_text is not None:
            raise click.UsageError("--last-n-days cannot be combined with --date-from or --date-to.")
        if last_n_days < 1:
            raise click.UsageError("--last-n-days must be at least 1.")
        return last_closed_day - timedelta(days=last_n_days - 1), last_closed_day

    if date_from_text is None and date_to_text is None:
        default_start = last_closed_day - timedelta(days=6)
        date_from_text = click.prompt("Date from", default=default_start.isoformat())
        date_to_text = click.prompt("Date to", default=last_closed_day.isoformat())
    elif date_from_text is None or date_to_text is None:
        raise click.UsageError("--date-from and --date-to must be provided together.")

    assert date_from_text is not None
    assert date_to_text is not None
    start_day = _parse_cli_date(date_from_text, "--date-from")
    end_day = _parse_cli_date(date_to_text, "--date-to")
    if start_day > end_day:
        raise click.UsageError("--date-from must be on or before --date-to.")
    return start_day, end_day


def _parse_cli_date(value: str, option_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise click.UsageError(f"{option_name} must use YYYY-MM-DD format.") from exc
