import pytest
import os
import yaml
from src.config import load_config, Config

def test_load_config_defaults(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_data = {
        "database": {"days": 30, "db_path": ":memory:"},
        "sources": {"test_source": {"type": "gmail"}},
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
        "database": {"days": 60, "db_path": "other.db"},
        "sources": {},
        "sink": {}
    }
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)
    
    config = load_config(str(config_file))
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 9000
    assert config.database.days == 60
    assert config.database.db_path == "other.db"
