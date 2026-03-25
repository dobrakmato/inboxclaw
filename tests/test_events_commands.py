import unittest
import os
import tempfile
import yaml
from click.testing import CliRunner
from datetime import datetime, timezone, timedelta
from src.cli import cli
from src.database import init_db, Source, Event, PendingEvent

class TestEventsCommands(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.config_path = os.path.join(self.test_dir, "config.yaml")
        
        # Create a dummy config
        config = {
            "database": {"db_path": self.db_path},
            "sources": {"test_source": {"type": "mock"}},
            "sink": {}
        }
        with open(self.config_path, "w") as f:
            yaml.dump(config, f)
            
        # Initialize DB and add some data
        self.session_maker = init_db(self.db_path)
        with self.session_maker() as session:
            source = Source(name="test_source", type="mock")
            session.add(source)
            session.commit()
            
            # Add published events
            for i in range(15):
                event = Event(
                    event_id=f"evt_{i}",
                    source_id=source.id,
                    event_type="test_event",
                    entity_id=f"entity_{i}",
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=i),
                    data={"i": i}
                )
                session.add(event)
            
            # Add pending events
            for i in range(5):
                pending = PendingEvent(
                    source_id=source.id,
                    event_type="pending_type",
                    entity_id=f"pending_{i}",
                    data={"i": i},
                    count=i+1,
                    first_seen_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                    last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=i),
                    flush_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                    strategy="debounce",
                    window_seconds=60
                )
                session.add(pending)
            
            session.commit()

    def tearDown(self):
        # session_maker.kw['bind'] should be the engine
        if 'bind' in self.session_maker.kw:
            self.session_maker.kw['bind'].dispose()
        import shutil
        import time
        # Small delay to let OS release file
        time.sleep(0.1)
        try:
            shutil.rmtree(self.test_dir)
        except PermissionError:
            pass # On Windows this is sometimes unavoidable in tests

    def test_events_command(self):
        result = self.runner.invoke(cli, ["events", "--config", self.config_path, "-n", "5"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("=== Latest 5 Published Events ===", result.output)
        # Check if it shows the most recent ones (evt_0, evt_1, ...)
        self.assertIn("evt_0", result.output)
        self.assertIn("evt_4", result.output)
        self.assertNotIn("evt_5", result.output)

    def test_pending_events_command(self):
        result = self.runner.invoke(cli, ["pending-events", "--config", self.config_path, "-n", "3"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("=== Latest 3 Pending Events ===", result.output)
        self.assertIn("pending_0", result.output)
        self.assertIn("pending_2", result.output)
        self.assertNotIn("pending_3", result.output)

    def test_events_json(self):
        result = self.runner.invoke(cli, ["events", "--config", self.config_path, "-n", "3", "-j"])
        self.assertEqual(result.exit_code, 0)
        import json
        data = json.loads(result.output)
        self.assertEqual(len(data), 3)
        self.assertEqual(data[0]["event_id"], "evt_0")
        self.assertEqual(data[0]["source"], "test_source")
        self.assertEqual(data[0]["event_type"], "test_event")
        self.assertEqual(data[0]["entity_id"], "entity_0")
        self.assertEqual(data[0]["data"], {"i": 0})
        self.assertIn("created_at", data[0])
        self.assertIn("meta", data[0])
        self.assertIn("id", data[0])

    def test_pending_events_json(self):
        result = self.runner.invoke(cli, ["pending-events", "--config", self.config_path, "-n", "2", "-j"])
        self.assertEqual(result.exit_code, 0)
        import json
        data = json.loads(result.output)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["source"], "test_source")
        self.assertEqual(data[0]["event_type"], "pending_type")
        self.assertIn("count", data[0])
        self.assertIn("first_seen_at", data[0])
        self.assertIn("last_seen_at", data[0])
        self.assertIn("flush_at", data[0])
        self.assertIn("strategy", data[0])
        self.assertIn("window_seconds", data[0])

    def test_events_json_empty(self):
        db_path_empty = os.path.join(self.test_dir, "empty2.db")
        config_empty = os.path.join(self.test_dir, "config_empty2.yaml")
        with open(config_empty, "w") as f:
            yaml.dump({"database": {"db_path": db_path_empty}, "sources": {}, "sink": {}}, f)
        init_db(db_path_empty)
        result = self.runner.invoke(cli, ["events", "--config", config_empty, "-j"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("No published events found.", result.output)

    def test_events_empty(self):
        # Create empty db
        db_path_empty = os.path.join(self.test_dir, "empty.db")
        config_empty = os.path.join(self.test_dir, "config_empty.yaml")
        with open(config_empty, "w") as f:
            yaml.dump({"database": {"db_path": db_path_empty}, "sources": {}, "sink": {}}, f)
        
        init_db(db_path_empty)
        
        result = self.runner.invoke(cli, ["events", "--config", config_empty])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("No published events found.", result.output)

    def test_events_filter_source(self):
        result = self.runner.invoke(cli, ["events", "--config", self.config_path, "--source", "test_source"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("evt_0", result.output)
        
        result = self.runner.invoke(cli, ["events", "--config", self.config_path, "--source", "non_existent"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("No published events found.", result.output)

    def test_events_filter_type(self):
        result = self.runner.invoke(cli, ["events", "--config", self.config_path, "--event-type", "test_event"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("evt_0", result.output)
        
        result = self.runner.invoke(cli, ["events", "--config", self.config_path, "--event-type", "other_type"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("No published events found.", result.output)

    def test_pending_events_filter_source(self):
        result = self.runner.invoke(cli, ["pending-events", "--config", self.config_path, "--source", "test_source"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("pending_0", result.output)

    def test_pending_events_filter_type(self):
        result = self.runner.invoke(cli, ["pending-events", "--config", self.config_path, "--event-type", "pending_type"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("pending_0", result.output)

if __name__ == "__main__":
    unittest.main()
