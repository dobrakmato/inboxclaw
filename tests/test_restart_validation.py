import unittest
from unittest.mock import patch, MagicMock
from click.testing import CliRunner
from src.cli import cli
import src.cli.commands.restart

class TestRestartValidation(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    @patch('src.cli.commands.restart.os.name', 'posix')
    @patch('src.cli.commands.restart.get_uid', return_value=1000)
    @patch('src.cli.commands.restart.subprocess.run')
    @patch('src.cli.commands.restart.load_config')
    def test_restart_fails_on_invalid_config(self, mock_load_config, mock_run, mock_getuid):
        # Setup mock to raise an error during config loading
        mock_load_config.side_effect = Exception("Invalid configuration: missing 'database' section")
        
        # Invoke restart
        result = self.runner.invoke(cli, ['restart'])
        
        # It should FAIL before calling subprocess.run
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Invalid configuration", result.output)
        mock_run.assert_not_called()

    @patch('src.cli.commands.restart.os.name', 'posix')
    @patch('src.cli.commands.restart.get_uid', return_value=1000)
    @patch('src.cli.commands.restart.subprocess.run')
    @patch('src.cli.commands.restart.load_config')
    def test_restart_succeeds_on_valid_config(self, mock_load_config, mock_run, mock_getuid):
        # Setup mock to return a valid config
        mock_load_config.return_value = MagicMock()
        
        # Invoke restart
        result = self.runner.invoke(cli, ['restart'])
        
        # It should SUCCEED
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Restarting inboxclaw.service...", result.output)
        mock_run.assert_called_once()

if __name__ == '__main__':
    unittest.main()
