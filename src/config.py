import os
import yaml
from pydantic import BaseModel, Field, ConfigDict
from typing import Dict, List, Any, Optional

class DatabaseConfig(BaseModel):
    days: int = 30
    db_path: str = "./data/data.db"

class SourceConfig(BaseModel):
    type: Optional[str] = None
    model_config = ConfigDict(extra="allow")

class SinkConfig(BaseModel):
    type: str
    match: Any # Can be str or list of str
    model_config = ConfigDict(extra="allow")

class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000

class Config(BaseModel):
    server: ServerConfig = ServerConfig()
    database: DatabaseConfig
    sources: Dict[str, Optional[Dict[str, Any]]]
    sink: Dict[str, Dict[str, Any]]

def load_config(path: str = None) -> Config:
    if path is None:
        path = os.environ.get("CONFIG_PATH", "config.yaml")
    with open(path, "r") as f:
        data = yaml.safe_load(f)
    return Config(**data)
