import unittest
import os
import tempfile
import yaml
import json
from click.testing import CliRunner
from datetime import datetime, timezone, timedelta
from src.cli import cli
from src.database import init_db, Source, Event

class TestStatsCommand(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.config_path = os.path.join(self.test_dir, "config.yaml")
        
        # Create a dummy config
        config = {
            "database": {"db_path": self.db_path},
            "sources": {"source1": {"type": "mock"}, "source2": {"type": "mock"}},
            "sink": {}
        }
        with open(self.config_path, "w") as f:
            yaml.dump(config, f)
            
        # Initialize DB and add some data
        self.session_maker = init_db(self.db_path)
        with self.session_maker() as session:
            s1 = Source(name="source1", type="mock")
            s2 = Source(name="source2", type="mock")
            session.add_all([s1, s2])
            session.commit()
            
            now = datetime.now(timezone.utc)
            
            # Events for source1, typeA
            # 1 day ago
            session.add(Event(event_id="e1", source_id=s1.id, event_type="typeA", created_at=now - timedelta(hours=12), data={"msg": "hello"}))
            # 5 days ago
            session.add(Event(event_id="e2", source_id=s1.id, event_type="typeA", created_at=now - timedelta(days=5), data={"msg": "old"}))
            # 15 days ago
            session.add(Event(event_id="e3", source_id=s1.id, event_type="typeA", created_at=now - timedelta(days=15), data={"msg": "older"}))
            
            # Events for source2, typeB
            # 2 days ago (so in 7 and 30 but not 1)
            session.add(Event(event_id="e4", source_id=s2.id, event_type="typeB", created_at=now - timedelta(days=2), data={"msg": "source2-1"}))
            # 40 days ago (not in any)
            session.add(Event(event_id="e5", source_id=s2.id, event_type="typeB", created_at=now - timedelta(days=40), data={"msg": "ancient"}))
            
            session.commit()

    def tearDown(self):
        if 'bind' in self.session_maker.kw:
            self.session_maker.kw['bind'].dispose()
        import shutil
        import time
        time.sleep(0.1)
        try:
            shutil.rmtree(self.test_dir)
        except PermissionError:
            pass

    def test_stats_command(self):
        # This will fail until the command is implemented and registered
        result = self.runner.invoke(cli, ["stats", "--config", self.config_path])
        
        # Once implemented, we expect success
        self.assertEqual(result.exit_code, 0)
        
        # Check source stats
        # source1: 1d=1, 7d=2, 30d=3
        # source2: 1d=0, 7d=1, 30d=1
        self.assertIn("source1", result.output)
        self.assertIn("source2", result.output)
        
        # Check type stats
        # typeA: 1d=1, 7d=2, 30d=3
        # typeB: 1d=0, 7d=1, 30d=1
        self.assertIn("typeA", result.output)
        self.assertIn("typeB", result.output)
        
        # Check average size
        # typeA data sizes: 18, 16, 18 (approximate, JSON stringified)
        # typeB data sizes: 20, 18
        self.assertIn("Average Event Size", result.output)

if __name__ == "__main__":
    unittest.main()
