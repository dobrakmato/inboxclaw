import unittest
from unittest.mock import patch, MagicMock
from click.testing import CliRunner
import httpx
from src.cli import cli
from src.config import Config, ServerConfig, DatabaseConfig, HttpPullSinkConfig
import src.cli.commands.pull  # Ensure it's registered

class TestPullCommand(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.mock_config = Config(
            server=ServerConfig(host="127.0.0.1", port=8000),
            database=DatabaseConfig(db_path="test.db"),
            sources={},
            sink={
                "my_pull": HttpPullSinkConfig(type="http_pull")
            }
        )

    @patch("src.cli.commands.pull.load_config")
    @patch("src.cli.commands.pull.httpx.Client")
    def test_pull_success(self, mock_client_class, mock_load_config):
        mock_load_config.return_value = self.mock_config
        
        # Setup mock client
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        
        # Mock responses
        mock_extract_resp = MagicMock()
        mock_extract_resp.status_code = 200
        mock_extract_resp.json.return_value = {
            "batch_id": 123,
            "events": [{"type": "test", "entity_id": "e1"}],
            "remaining_events": 0
        }
        
        mock_confirm_resp = MagicMock()
        mock_confirm_resp.status_code = 200
        
        mock_client.get.return_value = mock_extract_resp
        mock_client.post.return_value = mock_confirm_resp
        
        result = self.runner.invoke(cli, ["pull"])
        
        self.assertEqual(result.exit_code, 0)
        
        # Verify JSON output
        import json
        output_data = json.loads(result.output.strip())
        self.assertEqual(output_data["batch_id"], 123)
        self.assertEqual(len(output_data["events"]), 1)
        self.assertEqual(output_data["events"][0]["type"], "test")
        
        # Check params
        mock_client.get.assert_called_once_with("http://127.0.0.1:8000/my_pull/extract", params={})
        mock_client.post.assert_called_once_with("http://127.0.0.1:8000/my_pull/mark-processed", params={"batch_id": 123})

    @patch("src.cli.commands.pull.load_config")
    def test_pull_no_sinks(self, mock_load_config):
        config_no_sinks = Config(
            server=ServerConfig(),
            database=DatabaseConfig(db_path="test.db"),
            sources={},
            sink={}
        )
        mock_load_config.return_value = config_no_sinks
        
        result = self.runner.invoke(cli, ["pull"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("No HTTP Pull sinks configured", result.output)

    @patch("src.cli.commands.pull.load_config")
    def test_pull_multiple_sinks_no_name(self, mock_load_config):
        config_multi_sinks = Config(
            server=ServerConfig(),
            database=DatabaseConfig(db_path="test.db"),
            sources={},
            sink={
                "sink1": HttpPullSinkConfig(type="http_pull"),
                "sink2": HttpPullSinkConfig(type="http_pull")
            }
        )
        mock_load_config.return_value = config_multi_sinks
        
        result = self.runner.invoke(cli, ["pull"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Multiple HTTP Pull sinks found", result.output)
        self.assertIn("sink1", result.output)
        self.assertIn("sink2", result.output)

    @patch("src.cli.commands.pull.load_config")
    @patch("src.cli.commands.pull.httpx.Client")
    def test_pull_with_args(self, mock_client_class, mock_load_config):
        mock_load_config.return_value = self.mock_config
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        
        mock_extract_resp = MagicMock()
        mock_extract_resp.status_code = 200
        mock_extract_resp.json.return_value = {
            "batch_id": 456,
            "events": [{"type": "mail", "entity_id": "m1"}],
            "remaining_events": 10
        }
        mock_client.get.return_value = mock_extract_resp
        mock_client.post.return_value = MagicMock(status_code=200)
        
        result = self.runner.invoke(cli, ["pull", "--event-type", "mail.*", "--batch-size", "5"])
        
        self.assertEqual(result.exit_code, 0)
        mock_client.get.assert_called_once_with(
            "http://127.0.0.1:8000/my_pull/extract", 
            params={"event_type": "mail.*", "batch_size": 5}
        )
        
        import json
        output_data = json.loads(result.output.strip())
        self.assertEqual(output_data["remaining_events"], 10)

if __name__ == '__main__':
    unittest.main()
