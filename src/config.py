import os
from enum import Enum
from typing import Dict, List, Optional, Union, Literal, Annotated, Any

MIN_NORDIGEN_POLL_INTERVAL: float = 6 * 3600.0  # 6 hours

import yaml
from pydantic import BaseModel, Field, ConfigDict, BeforeValidator
from pytimeparse import parse as parse_time
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


def parse_interval(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)

    parsed = parse_time(value)  # pytimeparse or your wrapper
    if parsed is not None:
        return float(parsed)

    try:
        return float(value)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid interval: {value!r}") from e


Interval = Annotated[float, BeforeValidator(parse_interval)]

class DatabaseConfig(BaseModel):
    retention_days: int = Field(alias="days", default=30)
    db_path: str = "./data/data2.db"
    echo: bool = False
    model_config = ConfigDict(populate_by_name=True)

# --- Source Configurations ---

class CoalesceStrategy(str, Enum):
    DEBOUNCE = "debounce"
    BATCH = "batch"

class CoalesceRule(BaseModel):
    match: Union[str, List[str]]
    strategy: CoalesceStrategy
    window: Interval
    # Future-proofing: aggregation strategy
    aggregation: str = "latest"

class BaseSourceConfig(BaseModel):
    type: str
    coalesce: List[CoalesceRule] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", validate_default=True)

class GoogleSourceConfig(BaseSourceConfig):
    token_file: str
    poll_interval: Interval = "10m"

class GmailFilterItem(BaseModel):
    in_field: Literal["subject", "snippet", "sender"] = Field(alias="in")
    regex: Optional[str] = None
    contains: Optional[str] = None
    model_config = ConfigDict(populate_by_name=True)

class DriveFilterItem(BaseModel):
    in_field: Literal["file_id", "name", "parent_id"] = Field(alias="in")
    regex: Optional[str] = None
    contains: Optional[str] = None
    model_config = ConfigDict(populate_by_name=True)

class CalendarFilterItem(BaseModel):
    in_field: Literal["summary", "description", "location", "organizer", "attendees"] = Field(alias="in")
    regex: Optional[str] = None
    contains: Optional[str] = None
    model_config = ConfigDict(populate_by_name=True)

class GmailSourceConfig(GoogleSourceConfig):
    type: Literal["gmail"] = "gmail"
    exclude_label_ids: List[str] = Field(default_factory=lambda: ["SPAM"])
    emit_label_events: bool = False
    recovery_backfill_window: Interval = "7d"
    filters: List[Dict[str, GmailFilterItem]] = Field(default_factory=list)

class GoogleDriveSourceConfig(GoogleSourceConfig):
    type: Literal["google_drive"] = "google_drive"
    restrict_to_my_drive: bool = False
    include_corpus_removals: bool = False
    eligible_mime_types_for_content_diff: List[str] = Field(
        default_factory=lambda: [
            "application/vnd.google-apps.document",
            "text/plain",
            "text/markdown",
            "text/html",
        ]
    )
    max_diffable_file_bytes: int = 10 * 1024 * 1024  # 10MB
    max_changed_sections: int = 5
    max_section_chars: int = 300
    filters: List[Dict[str, DriveFilterItem]] = Field(default_factory=list)

class GoogleCalendarOverrideConfig(BaseModel):
    max_into_future: Interval
    model_config = ConfigDict(extra="forbid", validate_default=True)


class GoogleCalendarSourceConfig(GoogleSourceConfig):
    type: Literal["google_calendar"] = "google_calendar"
    calendar_ids: List[str] = Field(default_factory=lambda: ["primary"])
    max_event_age_days: Optional[float] = 2.0
    max_into_future: Interval = "365d"
    attendee_detail_limit: int = Field(default=3, ge=0)
    calendar_overrides: Dict[str, GoogleCalendarOverrideConfig] = Field(default_factory=dict)
    filters: List[Dict[str, CalendarFilterItem]] = Field(default_factory=list)

class FakturyOnlineSourceConfig(BaseSourceConfig):
    type: Literal["faktury_online"] = "faktury_online"
    api_key: str = Field(default_factory=lambda: os.environ.get("FAKTURY_ONLINE_KEY", ""))
    email: str = Field(default_factory=lambda: os.environ.get("FAKTURY_ONLINE_EMAIL", ""))
    poll_interval: Interval = "6h"
    max_days_back: int = 30

class FioSourceConfig(BaseSourceConfig):
    type: Literal["fio"] = "fio"
    token: str = Field(default_factory=lambda: os.environ.get("FIO_TOKEN", ""))
    poll_interval: Interval = "30m"
    max_days_back: int = 15
    look_ahead_days: int = 5

class MockSourceConfig(BaseSourceConfig):
    type: Literal["mock"] = "mock"
    interval: Interval = "10s"

class NordigenSourceConfig(BaseSourceConfig):
    type: Literal["nordigen"] = "nordigen"
    secret_id: str = Field(default_factory=lambda: os.environ.get("NORDIGEN_SECRET_ID", ""))
    secret_key: str = Field(default_factory=lambda: os.environ.get("NORDIGEN_SECRET_KEY", ""))
    refresh_token: str = Field(default_factory=lambda: os.environ.get("NORDIGEN_REFRESH_TOKEN", ""))
    account_id: str = ""
    label: Optional[str] = None
    poll_interval: Interval = "6h"
    initial_history_days: int = 90

    @property
    def effective_poll_interval(self) -> float:
        """Poll interval capped at the 6-hour GoCardless minimum."""
        return max(self.poll_interval, MIN_NORDIGEN_POLL_INTERVAL)

class HomeAssistantSourceConfig(BaseSourceConfig):
    type: Literal["home_assistant"] = "home_assistant"
    url: str
    access_token: str = Field(default_factory=lambda: os.environ.get("HOME_ASSISTANT_TOKEN", ""))
    entity_ids: List[str]
    location_threshold_meters: float = 0.0

class JiraSourceConfig(BaseSourceConfig):
    type: Literal["jira"] = "jira"
    url: str
    email: str = Field(default_factory=lambda: os.environ.get("JIRA_EMAIL", ""))
    api_token: str = Field(default_factory=lambda: os.environ.get("JIRA_API_TOKEN", ""))
    jql: str = "assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC"
    poll_interval: Interval = "1m"
    field_discovery_interval: Interval = "24h"
    ignored_fields: List[str] = Field(default_factory=lambda: ["updated", "workratio", "lastViewed", "aggregateprogress", "progress"])

class AsanaSourceConfig(BaseSourceConfig):
    type: Literal["asana"] = "asana"
    access_token: str = Field(default_factory=lambda: os.environ.get("ASANA_ACCESS_TOKEN", ""))
    project_gids: List[str]
    poll_interval: Interval = "1m"
    field_discovery_interval: Interval = "24h"
    track_comments: bool = True
    ignored_fields: List[str] = Field(default_factory=lambda: ["modified_at", "liked", "num_likes", "num_subtasks"])

class GoogleHealthSourceConfig(GoogleSourceConfig):
    type: Literal["google_health"] = "google_health"
    data_types: List[str] = Field(default_factory=lambda: ["steps", "sleep", "exercise", "weight", "heart-rate"])
    lookback_days: int = 7

class StravaSourceConfig(BaseSourceConfig):
    type: Literal["strava"] = "strava"
    client_id: str = Field(default_factory=lambda: os.environ.get("STRAVA_CLIENT_ID", ""))
    client_secret: str = Field(default_factory=lambda: os.environ.get("STRAVA_CLIENT_SECRET", ""))
    refresh_token: str = Field(default_factory=lambda: os.environ.get("STRAVA_REFRESH_TOKEN", ""))
    poll_interval: Interval = "15m"
    lookback_days: int = Field(default=7, ge=1)
    per_page: int = Field(default=100, ge=1, le=200)
    ignored_fields: List[str] = Field(default_factory=lambda: [
        "achievement_count",
        "athlete_count",
        "comment_count",
        "kudos_count",
        "photo_count",
        "total_photo_count",
    ])

class FilesystemSourceConfig(BaseSourceConfig):
    type: Literal["filesystem"] = "filesystem"
    path: str
    watch_mode: Literal["watch", "poll", "hybrid"] = "hybrid"
    poll_interval: Interval = "5m"
    extensions: Optional[List[str]] = None
    ignore_patterns: List[str] = Field(default_factory=lambda: [".git/**", "*.tmp", ".trash/**"])
    recursive: bool = True
    include_content_preview: bool = False
    content_preview_length: int = 200
    max_changed_sections: int = 5
    max_section_chars: int = 300

SourceConfig = Annotated[
    Union[
        GmailSourceConfig,
        GoogleDriveSourceConfig,
        GoogleCalendarSourceConfig,
        FakturyOnlineSourceConfig,
        FioSourceConfig,
        MockSourceConfig,
        HomeAssistantSourceConfig,
        NordigenSourceConfig,
        JiraSourceConfig,
        AsanaSourceConfig,
        GoogleHealthSourceConfig,
        StravaSourceConfig,
        FilesystemSourceConfig,
    ],
    Field(discriminator="type")
]

# --- Sink Configurations ---

class BaseSinkConfig(BaseModel):
    type: str
    match: Union[str, List[str]] = "*"
    model_config = ConfigDict(extra="forbid", validate_default=True)

class TTLConfig(BaseModel):
    ttl_enabled: bool = True
    default_ttl: Interval = "1h"
    event_ttl: Dict[str, Interval] = Field(default_factory=dict)

class WebhookSinkConfig(BaseSinkConfig, TTLConfig):
    type: Literal["webhook"] = "webhook"
    url: str
    max_retries: int = 3
    retry_interval: Interval = 10.0
    headers: Dict[str, str] = Field(default_factory=dict)
    payload: Optional[Dict[str, Any]] = None

class HttpPullSinkConfig(BaseSinkConfig, TTLConfig):
    type: Literal["http_pull"] = "http_pull"
    path: Dict[str, str] = Field(default_factory=lambda: {"extract": "extract", "mark_processed": "mark-processed"})

class SSESinkConfig(BaseSinkConfig):
    type: Literal["sse"] = "sse"
    path: str = ""
    heartbeat_timeout: Interval = 30.0

class Win11ToastSinkConfig(BaseSinkConfig):
    type: Literal["win11toast"] = "win11toast"
    max_body_length: int = 220

class CommandSinkConfig(BaseSinkConfig, TTLConfig):
    type: Literal["command"] = "command"
    command: Union[str, List[str]]
    batch_command: Optional[Union[str, List[str]]] = None
    batch_threshold: int = 10
    max_retries: int = 3
    retry_interval: Interval = 10.0

class FolderSinkConfig(BaseSinkConfig):
    type: Literal["folder"] = "folder"
    path: str

class DiarySinkConfig(BaseSinkConfig):
    type: Literal["diary"] = "diary"
    path: str
    cutoff_time: str = "04:00"
    timezone: Optional[str] = None
    lock_timeout: Interval = "30s"
    max_backfill_days: int = 3
    summary_mode: Literal["concat", "llm"] = "concat"
    llm_endpoint_url: Optional[str] = Field(
        default_factory=lambda: os.environ.get("DIARY_LLM_ENDPOINT_URL") or os.environ.get("OPENAI_BASE_URL")
    )
    llm_api_key: Optional[str] = Field(
        default_factory=lambda: os.environ.get("DIARY_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    )
    llm_model: Optional[str] = Field(
        default_factory=lambda: os.environ.get("DIARY_LLM_MODEL") or os.environ.get("OPENAI_MODEL")
    )
    llm_effort: Optional[str] = Field(default_factory=lambda: os.environ.get("DIARY_LLM_EFFORT"))
    llm_timeout: Interval = Field(default_factory=lambda: os.environ.get("DIARY_LLM_TIMEOUT", "2m"))
    llm_max_retries: int = Field(default_factory=lambda: int(os.environ.get("DIARY_LLM_MAX_RETRIES", "2")))
    daily_prompt_path: Optional[str] = None
    weekly_prompt_path: Optional[str] = None
    monthly_prompt_path: Optional[str] = None

SinkConfig = Annotated[
    Union[
        WebhookSinkConfig,
        HttpPullSinkConfig,
        SSESinkConfig,
        Win11ToastSinkConfig,
        CommandSinkConfig,
        FolderSinkConfig,
        DiarySinkConfig,
    ],
    Field(discriminator="type")
]

class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000

class Config(BaseModel):
    server: ServerConfig = ServerConfig()
    database: DatabaseConfig
    sources: Dict[str, SourceConfig]
    sink: Dict[str, SinkConfig]

def load_config(path: str = None) -> Config:
    if path is None:
        path = os.environ.get("CONFIG_PATH", "config.yaml")
    
    with open(path, "r") as f:
        content = f.read()
    
    # Expand environment variables like ${VAR} or $VAR
    expanded_content = os.path.expandvars(content)
    data = yaml.safe_load(expanded_content)
    
    # Pre-process sources and sinks to ensure 'type' is set
    if "sources" in data:
        for name, cfg in data.get("sources", {}).items():
            if isinstance(cfg, dict) and "type" not in cfg:
                cfg["type"] = name
    if "sink" in data:
        for name, cfg in data.get("sink", {}).items():
            if isinstance(cfg, dict) and "type" not in cfg:
                cfg["type"] = name

    return Config(**data)
