import pytest
import os
import yaml
from src.config import load_config, Config, Interval
from pydantic import BaseModel
from scripts.generate_schema import build_schema

class IntervalModel(BaseModel):
    interval: Interval

def test_interval_parsing():
    # Test seconds as string
    m = IntervalModel(interval="10s")
    assert m.interval == 10.0

    # Test minutes as string
    m = IntervalModel(interval="5m")
    assert m.interval == 300.0

    # Test hours as string
    m = IntervalModel(interval="1h")
    assert m.interval == 3600.0

    # Test float directly
    m = IntervalModel(interval=15.5)
    assert m.interval == 15.5

    # Test integer directly
    m = IntervalModel(interval=60)
    assert m.interval == 60.0

    # Test invalid interval
    with pytest.raises(ValueError, match="Invalid interval"):
        IntervalModel(interval="invalid")

def test_load_config_with_intervals(tmp_path):
    config_file = tmp_path / "config_intervals.yaml"
    config_data = {
        "database": {"retention_days": 30, "db_path": ":memory:"},
        "sources": {
            "gcal": {
                "type": "google_calendar",
                "token_file": "token.json",
                "poll_interval": "10m"
            },
            "mocker": {
                "type": "mock",
                "interval": "5s"
            }
        },
        "sink": {
            "webhook_sink": {
                "type": "webhook",
                "match": "*",
                "url": "http://localhost/hook",
                "retry_interval": "1m"
            },
            "sse_sink": {
                "type": "sse",
                "match": "*",
                "heartbeat_timeout": "45s"
            },
            "toast_sink": {
                "type": "win11toast",
                "match": ["google.*"],
                "max_body_length": 180
            }
        }
    }
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)
    
    config = load_config(str(config_file))
    assert config.sources["gcal"].poll_interval == 600.0
    assert config.sources["mocker"].interval == 5.0
    assert config.sink["webhook_sink"].retry_interval == 60.0
    assert config.sink["sse_sink"].heartbeat_timeout == 45.0
    assert config.sink["toast_sink"].max_body_length == 180

def test_load_config_defaults(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_data = {
        "database": {"retention_days": 30, "db_path": ":memory:"},
        "sources": {"test_source": {"type": "gmail", "token_file": "dummy.json"}},
        "sink": {"test_sink": {"type": "sse", "match": "*"}}
    }
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)
    
    config = load_config(str(config_file))
    assert config.server.host == "0.0.0.0"
    assert config.server.port == 8000
    assert config.database.db_path == ":memory:"
    assert "test_source" in config.sources
    assert "test_sink" in config.sink

def test_load_config_overrides(tmp_path):
    config_file = tmp_path / "config_override.yaml"
    config_data = {
        "server": {"host": "127.0.0.1", "port": 9000},
        "database": {"retention_days": 60, "db_path": "other.db"},
        "sources": {},
        "sink": {}
    }
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)
    
    config = load_config(str(config_file))
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 9000
    assert config.database.retention_days == 60
    assert config.database.db_path == "other.db"

def test_http_pull_ttl_config(tmp_path):
    config_file = tmp_path / "config_ttl.yaml"
    config_data = {
        "database": {"retention_days": 30, "db_path": ":memory:"},
        "sources": {},
        "sink": {
            "puller": {
                "type": "http_pull",
                "ttl_enabled": True,
                "default_ttl": "2h",
                "event_ttl": {
                    "urgent": "5m",
                    "gmail.*": "15m"
                }
            }
        }
    }
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)
    
    config = load_config(str(config_file))
    pull_config = config.sink["puller"]
    assert pull_config.ttl_enabled is True
    assert pull_config.default_ttl == 7200.0
    assert pull_config.event_ttl["urgent"] == 300.0
    assert pull_config.event_ttl["gmail.*"] == 900.0

def test_load_config_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DB_PATH", "env_data.db")
    monkeypatch.setenv("TEST_RETENTION", "45")
    monkeypatch.setenv("TEST_TOKEN", "supersecret")
    
    config_file = tmp_path / "config_env.yaml"
    config_content = """
database:
  db_path: ${TEST_DB_PATH}
  days: ${TEST_RETENTION}
sources:
  fio_acc:
    type: fio
    token: ${TEST_TOKEN}
sink: {}
"""
    with open(config_file, "w") as f:
        f.write(config_content)
    
    config = load_config(str(config_file))
    assert config.database.db_path == "env_data.db"
    assert config.database.retention_days == 45
    assert config.sources["fio_acc"].token == "supersecret"


def test_generated_schema_supports_key_named_discriminator_without_type() -> None:
    schema = build_schema()

    sink_props = schema["properties"]["sink"]["properties"]
    assert sink_props["sse"]["$ref"] == "#/$defs/SSESinkConfig"

    source_props = schema["properties"]["sources"]["properties"]
    assert source_props["faktury_online"]["$ref"] == "#/$defs/FakturyOnlineSourceConfig"
