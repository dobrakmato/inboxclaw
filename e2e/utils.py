import os
import subprocess
import sys
import time
import yaml
import requests
import shutil
from typing import Dict, Any, Optional

class E2EApp:
    def __init__(self, config: Dict[str, Any], config_name: str, app_port: int, suite_name: Optional[str] = None):
        self.config = config
        self.config_name = config_name
        self.app_port = app_port
        
        # All runs now go into e2e/runs/<suite_name>_<config_name>
        prefix = f"{suite_name}_" if suite_name else ""
        self.e2e_dir = os.path.join("e2e", "runs", f"{prefix}{config_name}")
            
        os.makedirs(self.e2e_dir, exist_ok=True)
        
        self.config_path = os.path.join(self.e2e_dir, "config.yaml")
        self.db_path = os.path.join(self.e2e_dir, "data.db")
        self.process: Optional[subprocess.Popen] = None
        
        # Adjust config for E2E
        self.config.setdefault("database", {})["db_path"] = self.db_path
        self.config.setdefault("server", {})["port"] = self.app_port
        self.config["server"]["host"] = "127.0.0.1"

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def start(self):
        # Clean up old DB if exists
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except PermissionError:
                time.sleep(1)
                os.remove(self.db_path)
            
        # Write config file in the run directory
        with open(self.config_path, "w") as f:
            yaml.dump(self.config, f)
            
        env = os.environ.copy()
        env["PYTHONPATH"] = os.getcwd()
        env["CONFIG_PATH"] = self.config_path
        
        self.process = subprocess.Popen(
            [sys.executable, "-m", "src.main"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Wait for app to be ready
        url = f"http://127.0.0.1:{self.app_port}/"
        max_retries = 30
        for i in range(max_retries):
            try:
                response = requests.get(url, timeout=1)
                if response.status_code == 200:
                    return
            except requests.exceptions.ConnectionError:
                pass
            time.sleep(1)
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                raise RuntimeError(f"App failed to start (code {self.process.returncode})\nSTDOUT: {stdout}\nSTDERR: {stderr}")
        
        raise RuntimeError("Timed out waiting for app to start")

    def stop(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # On Windows, terminate might not work for some process trees
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.process.pid)], 
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def get_url(self, path: str = "") -> str:
        return f"http://127.0.0.1:{self.app_port}/{path.lstrip('/')}"
