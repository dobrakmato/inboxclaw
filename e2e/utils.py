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
        
        self.stdout_path = os.path.join(self.e2e_dir, "stdout.log")
        self.stderr_path = os.path.join(self.e2e_dir, "stderr.log")
        self.stdout_file = None
        self.stderr_file = None
        
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
        
        self.stdout_file = open(self.stdout_path, "w")
        self.stderr_file = open(self.stderr_path, "w")
        
        # Use subprocess.CREATE_NEW_PROCESS_GROUP on Windows to allow taskkill/pgid-like cleanup if needed
        # But for now just Popen is fine as we'll use process.kill()
        
        self.process = subprocess.Popen(
            [sys.executable, "main.py", "listen"],
            env=env,
            stdout=self.stdout_file,
            stderr=self.stderr_file,
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
                # Close files to ensure output is flushed
                self.stdout_file.close()
                self.stderr_file.close()
                with open(self.stdout_path, "r") as f:
                    stdout = f.read()
                with open(self.stderr_path, "r") as f:
                    stderr = f.read()
                raise RuntimeError(f"App failed to start (code {self.process.returncode})\nSTDOUT: {stdout}\nSTDERR: {stderr}")
        
        raise RuntimeError("Timed out waiting for app to start")

    def stop(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.process.pid)], 
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    self.process.kill()
            
            if self.stdout_file and not self.stdout_file.closed:
                self.stdout_file.close()
            if self.stderr_file and not self.stderr_file.closed:
                self.stderr_file.close()

    def get_url(self, path: str = "") -> str:
        return f"http://127.0.0.1:{self.app_port}/{path.lstrip('/')}"
