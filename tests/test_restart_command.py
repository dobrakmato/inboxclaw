import os
import unittest
from unittest.mock import patch, MagicMock
from click.testing import CliRunner
from src.cli import cli
import src.cli.commands.restart # Ensure it's registered

class TestRestartCommand(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    @patch('src.cli.commands.restart.os.name', 'posix')
    @patch('src.cli.commands.restart.get_uid', return_value=1000)
    @patch('src.cli.commands.restart.subprocess.run')
    def test_restart_user_success(self, mock_run, mock_getuid):
        # Test default restart (user)
        result = self.runner.invoke(cli, ['restart'])
        
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Restarting inboxclaw.service...", result.output)
        self.assertIn("Successfully restarted inboxclaw.service", result.output)
        mock_run.assert_called_once_with(["systemctl", "--user", "restart", "inboxclaw.service"], check=True)

    @patch('src.cli.commands.restart.os.name', 'posix')
    @patch('src.cli.commands.restart.get_uid', return_value=0)
    @patch('src.cli.commands.restart.subprocess.run')
    def test_restart_system_success(self, mock_run, mock_getuid):
        # Test system restart
        result = self.runner.invoke(cli, ['restart', '--system'])
        
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Restarting inboxclaw.service...", result.output)
        mock_run.assert_called_once_with(["systemctl", "restart", "inboxclaw.service"], check=True)

    @patch('src.cli.commands.restart.os.name', 'posix')
    @patch('src.cli.commands.restart.get_uid', return_value=1000)
    def test_restart_system_no_root(self, mock_getuid):
        # Test system restart without root
        result = self.runner.invoke(cli, ['restart', '--system'])
        
        self.assertEqual(result.exit_code, 1)
        self.assertIn("System-wide restart requires root privileges", result.output)

    @patch('src.cli.commands.restart.os.name', 'nt')
    def test_restart_non_posix(self):
        # Test on non-POSIX system
        result = self.runner.invoke(cli, ['restart'])
        
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Systemd is only supported on Linux/POSIX systems.", result.output)

    @patch('src.cli.commands.restart.os.name', 'posix')
    @patch('src.cli.commands.restart.get_uid', return_value=1000)
    @patch('src.cli.commands.restart.subprocess.run')
    def test_restart_custom_name(self, mock_run, mock_getuid):
        # Test with custom service name
        result = self.runner.invoke(cli, ['restart', '--service-name', 'my-claw'])
        
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Restarting my-claw.service...", result.output)
        mock_run.assert_called_once_with(["systemctl", "--user", "restart", "my-claw.service"], check=True)

if __name__ == '__main__':
    unittest.main()
