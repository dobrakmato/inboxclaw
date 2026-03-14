import os
from typing import Dict, List, Optional, Union, Literal, Annotated

import yaml
from pydantic import BaseModel, Field, ConfigDict, BeforeValidator
from pytimeparse import parse as parse_time


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
    db_path: str = "./data/data.db"
    model_config = ConfigDict(populate_by_name=True)

# --- Source Configurations ---

class BaseSourceConfig(BaseModel):
    type: str
    model_config = ConfigDict(extra="forbid")

class GoogleSourceConfig(BaseSourceConfig):
    token_file: str
    poll_interval: Interval = "10m"

class GmailSourceConfig(GoogleSourceConfig):
    type: Literal["gmail"] = "gmail"
    exclude_label_ids: List[str] = Field(default_factory=lambda: ["SPAM"])

class GoogleDriveSourceConfig(GoogleSourceConfig):
    type: Literal["google_drive"] = "google_drive"

class GoogleCalendarSourceConfig(GoogleSourceConfig):
    type: Literal["google_calendar"] = "google_calendar"
    calendar_ids: List[str] = Field(default_factory=lambda: ["primary"])
    max_event_age_days: Optional[float] = 1.0
    show_deleted: bool = True
    single_events: bool = True

class GoogleDocsSourceConfig(GoogleSourceConfig):
    type: Literal["google_docs"] = "google_docs"

class MockSourceConfig(BaseSourceConfig):
    type: Literal["mock"] = "mock"
    interval: Interval = "10s"

SourceConfig = Annotated[
    Union[
        GmailSourceConfig,
        GoogleDriveSourceConfig,
        GoogleCalendarSourceConfig,
        GoogleDocsSourceConfig,
        MockSourceConfig
    ],
    Field(discriminator="type")
]

# --- Sink Configurations ---

class BaseSinkConfig(BaseModel):
    type: str
    match: Union[str, List[str]] = "*"
    model_config = ConfigDict(extra="forbid")

class WebhookSinkConfig(BaseSinkConfig):
    type: Literal["webhook"] = "webhook"
    url: str
    max_retries: int = 3
    retry_interval: Interval = 10.0

class HttpPullSinkConfig(BaseSinkConfig):
    type: Literal["http_pull"] = "http_pull"
    path: Dict[str, str] = Field(default_factory=lambda: {"extract": "extract", "mark_processed": "mark-processed"})
    coalesce: Optional[List[str]] = None

class SSESinkConfig(BaseSinkConfig):
    type: Literal["sse"] = "sse"
    path: str = ""
    heartbeat_timeout: Interval = 30.0
    coalesce: Optional[List[str]] = None

SinkConfig = Annotated[
    Union[
        WebhookSinkConfig,
        HttpPullSinkConfig,
        SSESinkConfig
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
        data = yaml.safe_load(f)
    
    # Pre-process sources and sinks to ensure 'type' is set
    if "sources" in data:
        for name, cfg in data["sources"].items():
            if isinstance(cfg, dict) and "type" not in cfg:
                cfg["type"] = name
    if "sink" in data:
        for name, cfg in data["sink"].items():
            if isinstance(cfg, dict) and "type" not in cfg:
                cfg["type"] = name

    return Config(**data)
