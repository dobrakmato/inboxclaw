from unittest.mock import MagicMock, patch

import pytest

from src.utils.google_auth import get_google_credentials


def credential(*, token_json: str = '{"token":"fresh"}') -> MagicMock:
    value = MagicMock()
    value.expired = False
    value.refresh_token = "refresh-token"
    value.to_json.return_value = token_json
    return value


def test_force_refresh_reloads_under_lock_and_replaces_token_file(tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text('{"token":"stale"}', encoding="utf-8")
    first = credential()
    locked = credential()

    with patch(
        "src.utils.google_auth.Credentials.from_authorized_user_file",
        side_effect=[first, locked],
    ) as load, patch("src.utils.google_auth.Request") as request:
        result = get_google_credentials(
            str(token_path),
            "drive",
            force_refresh=True,
        )

    assert result is locked
    assert load.call_count == 2
    locked.refresh.assert_called_once_with(request.return_value)
    assert token_path.read_text(encoding="utf-8") == '{"token":"fresh"}'
    assert not token_path.with_name("token.json.tmp").exists()


def test_force_refresh_failure_preserves_existing_token_file(tmp_path):
    token_path = tmp_path / "token.json"
    original = '{"token":"stale"}'
    token_path.write_text(original, encoding="utf-8")
    first = credential()
    locked = credential()
    locked.refresh.side_effect = RuntimeError("refresh failed")

    with patch(
        "src.utils.google_auth.Credentials.from_authorized_user_file",
        side_effect=[first, locked],
    ), patch("src.utils.google_auth.Request"):
        with pytest.raises(RuntimeError, match="refresh failed"):
            get_google_credentials(str(token_path), "drive", force_refresh=True)

    assert token_path.read_text(encoding="utf-8") == original
    assert not token_path.with_name("token.json.tmp").exists()
