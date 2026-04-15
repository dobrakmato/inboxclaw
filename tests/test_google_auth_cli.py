import pytest
from click import UsageError

from src.cli.commands.google_auth import resolve_scopes, normalize_authorization_response


class TestResolveScopes:
    def test_single_alias(self):
        scopes = resolve_scopes("gmail")
        assert scopes == ["https://www.googleapis.com/auth/gmail.readonly"]

    def test_multiple_aliases(self):
        scopes = resolve_scopes("gmail,calendar")
        assert set(scopes) == {
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
        }

    def test_health_composite_alias(self):
        scopes = resolve_scopes("health")
        assert set(scopes) == {
            "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
            "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
            "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
        }

    def test_health_individual_aliases(self):
        scopes = resolve_scopes("health_activity")
        assert scopes == ["https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly"]

        scopes = resolve_scopes("health_sleep")
        assert scopes == ["https://www.googleapis.com/auth/googlehealth.sleep.readonly"]

        scopes = resolve_scopes("health_metrics")
        assert scopes == ["https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"]

    def test_all_includes_health(self):
        scopes = resolve_scopes("all")
        health_scopes = {
            "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
            "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
            "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
        }
        assert health_scopes.issubset(set(scopes))

    def test_all_includes_core_scopes(self):
        scopes = resolve_scopes("all")
        scope_set = set(scopes)
        assert "https://www.googleapis.com/auth/gmail.readonly" in scope_set
        assert "https://www.googleapis.com/auth/drive.readonly" in scope_set
        assert "https://www.googleapis.com/auth/calendar.readonly" in scope_set
        assert "https://www.googleapis.com/auth/contacts.readonly" in scope_set

    def test_deduplication(self):
        scopes = resolve_scopes("health,health_sleep")
        sleep_scope = "https://www.googleapis.com/auth/googlehealth.sleep.readonly"
        assert scopes.count(sleep_scope) == 1

    def test_raw_url_passthrough(self):
        url = "https://www.googleapis.com/auth/custom.scope"
        scopes = resolve_scopes(url)
        assert scopes == [url]

    def test_unknown_alias_raises(self):
        with pytest.raises(UsageError, match="Unknown scope alias"):
            resolve_scopes("nonexistent")

    def test_empty_raises(self):
        with pytest.raises(UsageError, match="At least one scope"):
            resolve_scopes("")

    def test_case_insensitive(self):
        scopes = resolve_scopes("Gmail")
        assert scopes == ["https://www.googleapis.com/auth/gmail.readonly"]


class TestNormalizeAuthorizationResponse:
    REDIRECT = "http://127.0.0.1:8765/"

    def test_valid_url(self):
        url = f"{self.REDIRECT}?code=abc123&scope=email"
        result = normalize_authorization_response(url, self.REDIRECT)
        assert "code=abc123" in result

    def test_strips_whitespace_and_quotes(self):
        url = f'  "{self.REDIRECT}?code=abc"  '
        result = normalize_authorization_response(url, self.REDIRECT)
        assert "code=abc" in result

    def test_rejects_wrong_host(self):
        from click import ClickException
        with pytest.raises(ClickException, match="does not match"):
            normalize_authorization_response("http://evil.com/?code=abc", self.REDIRECT)

    def test_rejects_missing_code(self):
        from click import ClickException
        with pytest.raises(ClickException, match="does not contain an authorization code"):
            normalize_authorization_response(f"{self.REDIRECT}?scope=email", self.REDIRECT)

    def test_oauth_error(self):
        from click import ClickException
        with pytest.raises(ClickException, match="OAuth error"):
            normalize_authorization_response(
                f"{self.REDIRECT}?error=access_denied&error_description=User+denied",
                self.REDIRECT,
            )
