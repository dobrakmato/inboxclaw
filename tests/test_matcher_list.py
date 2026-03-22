import pytest
from src.pipeline.matcher import EventMatcher

def test_matcher_supports_list():
    matcher = EventMatcher(["user.created", "user.updated"])
    assert matcher.matches("user.created")
    assert matcher.matches("user.updated")
    assert not matcher.matches("user.deleted")

def test_matcher_supports_mixed_list():
    matcher = EventMatcher(["auth.*", "system.ping"])
    assert matcher.matches("auth.login")
    assert matcher.matches("auth.logout")
    assert matcher.matches("system.ping")
    assert not matcher.matches("system.pong")

def test_matcher_supports_single_string():
    matcher = EventMatcher("user.*")
    assert matcher.matches("user.created")
    assert not matcher.matches("system.ping")
