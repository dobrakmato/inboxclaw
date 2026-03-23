from unittest.mock import patch

from click.testing import CliRunner

from src.cli import cli


class TestConfigCommand:
    """Tests for the `inboxclaw config` CLI command."""

    def setup_method(self):
        self.runner = CliRunner()

    @patch("src.cli.commands.config._resolve_config_path")
    @patch("os.path.isfile", return_value=True)
    @patch("click.edit")
    def test_opens_config_in_default_editor(self, mock_edit, mock_isfile, mock_resolve):
        mock_resolve.return_value = "/fake/config.yaml"
        mock_edit.return_value = None

        result = self.runner.invoke(cli, ["config"])

        assert result.exit_code == 0
        mock_edit.assert_called_once()
        _, kwargs = mock_edit.call_args
        assert kwargs.get("filename") == "/fake/config.yaml"
        assert "editor" not in kwargs

    @patch("src.cli.commands.config._resolve_config_path")
    @patch("os.path.isfile", return_value=True)
    @patch("click.edit")
    def test_opens_config_in_vim(self, mock_edit, mock_isfile, mock_resolve):
        mock_resolve.return_value = "/fake/config.yaml"
        mock_edit.return_value = None

        result = self.runner.invoke(cli, ["config", "--vim"])

        assert result.exit_code == 0
        _, kwargs = mock_edit.call_args
        assert kwargs.get("editor") == "vim"
        assert kwargs.get("filename") == "/fake/config.yaml"

    @patch("src.cli.commands.config._resolve_config_path")
    @patch("os.path.isfile", return_value=True)
    @patch("click.edit")
    def test_opens_config_in_nano(self, mock_edit, mock_isfile, mock_resolve):
        mock_resolve.return_value = "/fake/config.yaml"
        mock_edit.return_value = None

        result = self.runner.invoke(cli, ["config", "--nano"])

        assert result.exit_code == 0
        _, kwargs = mock_edit.call_args
        assert kwargs.get("editor") == "nano"
        assert kwargs.get("filename") == "/fake/config.yaml"

    @patch("src.cli.commands.config._resolve_config_path")
    @patch("os.path.isfile", return_value=True)
    @patch("click.edit")
    def test_vim_and_nano_mutually_exclusive(self, mock_edit, mock_isfile, mock_resolve):
        mock_resolve.return_value = "/fake/config.yaml"

        result = self.runner.invoke(cli, ["config", "--vim", "--nano"])

        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()

    @patch("src.cli.commands.config._resolve_config_path")
    def test_config_not_found(self, mock_resolve):
        mock_resolve.return_value = "/nonexistent/config.yaml"

        result = self.runner.invoke(cli, ["config"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()
