import os
import unittest
from unittest.mock import patch, MagicMock
from click.testing import CliRunner
from src.cli import cli
import src.cli.commands.config

class TestConfigCommand(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    @patch('src.cli.commands.config.click.edit')
    @patch('src.cli.commands.config.load_config')
    @patch('src.cli.commands.config.os.path.getmtime')
    @patch('src.cli.commands.config.os.path.isfile')
    @patch('src.cli.commands.config.os.path.exists')
    def test_config_changed_and_valid(self, mock_exists, mock_isfile, mock_getmtime, mock_load_config, mock_edit):
        mock_exists.return_value = True
        mock_isfile.return_value = True
        # Initial mtime then changed mtime
        mock_getmtime.side_effect = [1000, 2000]
        
        result = self.runner.invoke(cli, ['config'])
        
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Config file changed. Validating...", result.output)
        self.assertIn("Configuration is valid.", result.output)
        self.assertIn("You likely want to restart Inboxclaw to apply changes.", result.output)
        self.assertIn("Run: inboxclaw restart", result.output)
        mock_load_config.assert_called_once()

    @patch('src.cli.commands.config.click.edit')
    @patch('src.cli.commands.config.load_config')
    @patch('src.cli.commands.config.os.path.getmtime')
    @patch('src.cli.commands.config.os.path.isfile')
    @patch('src.cli.commands.config.os.path.exists')
    def test_config_not_changed(self, mock_exists, mock_isfile, mock_getmtime, mock_load_config, mock_edit):
        mock_exists.return_value = True
        mock_isfile.return_value = True
        # Same mtime
        mock_getmtime.side_effect = [1000, 1000]
        
        result = self.runner.invoke(cli, ['config'])
        
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn("Config file changed", result.output)
        mock_load_config.assert_not_called()

    @patch('src.cli.commands.config.click.edit')
    @patch('src.cli.commands.config.load_config')
    @patch('src.cli.commands.config.os.path.getmtime')
    @patch('src.cli.commands.config.os.path.isfile')
    @patch('src.cli.commands.config.os.path.exists')
    def test_config_changed_and_invalid(self, mock_exists, mock_isfile, mock_getmtime, mock_load_config, mock_edit):
        mock_exists.return_value = True
        mock_isfile.return_value = True
        mock_getmtime.side_effect = [1000, 2000]
        mock_load_config.side_effect = Exception("Invalid YAML")
        
        result = self.runner.invoke(cli, ['config'])
        
        self.assertEqual(result.exit_code, 0) # We don't want to crash if validation fails after edit
        self.assertIn("Config file changed. Validating...", result.output)
        self.assertIn("Configuration validation failed: Invalid YAML", result.output)
        self.assertNotIn("You likely want to restart Inboxclaw", result.output)

if __name__ == '__main__':
    unittest.main()
