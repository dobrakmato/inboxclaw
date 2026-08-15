import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import StravaSourceConfig
from src.sources.strava import StravaSource, TOKEN_STATE_KEY, _token_seed_hash


def make_response(body):
    response = MagicMock()
    response.json.return_value = body
    response.raise_for_status = MagicMock()
    return response


@pytest.fixture
def services():
    value_store = {}
    result = MagicMock()
    result.writer = MagicMock()
    result.kv.get.side_effect = lambda source_id, key: value_store.get((source_id, key))
    result.kv.set.side_effect = lambda source_id, key, value: value_store.__setitem__((source_id, key), value)
    result.value_store = value_store
    return result


@pytest.fixture
def config():
    return StravaSourceConfig(
        client_id="123",
        client_secret="client-secret",
        refresh_token="refresh-token",
        poll_interval="15m",
        lookback_days=7,
        per_page=2,
    )


def activity(activity_id=42, **overrides):
    value = {
        "id": activity_id,
        "name": "Morning Run",
        "sport_type": "Run",
        "distance": 5000.0,
        "moving_time": 1500,
        "start_date": "2026-08-14T06:30:00Z",
        "kudos_count": 3,
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_refreshes_token_and_persists_rotated_refresh_token(services, config):
    source = StravaSource("strava", config, services, 1)
    source.client.post = AsyncMock(return_value=make_response({
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_at": 2_000_000_000,
    }))

    token = await source._get_access_token()

    assert token == "new-access"
    source.client.post.assert_awaited_once()
    token_state = services.value_store[(1, TOKEN_STATE_KEY)]
    assert token_state["refresh_token"] == "new-refresh"
    assert token_state["expires_at"] == 2_000_000_000
    await source.client.aclose()


@pytest.mark.asyncio
async def test_reuses_unexpired_access_token(services, config):
    source = StravaSource("strava", config, services, 1)
    services.value_store[(1, TOKEN_STATE_KEY)] = {
        "seed_hash": _token_seed_hash(config.refresh_token),
        "access_token": "cached-access",
        "refresh_token": "cached-refresh",
        "expires_at": 2_000_000_000,
    }
    source.client.post = AsyncMock()

    token = await source._get_access_token()

    assert token == "cached-access"
    source.client.post.assert_not_awaited()
    await source.client.aclose()


@pytest.mark.asyncio
async def test_run_reports_success_to_source_health(services, config):
    source = StravaSource("strava", config, services, 1)
    source.poll = AsyncMock()

    with patch(
        "src.sources.strava.asyncio.sleep",
        new=AsyncMock(side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await source.run()

    services.health.reporter.assert_called_once_with("strava")
    source.health.checking.assert_called_once_with()
    source.health.healthy.assert_called_once_with()


@pytest.mark.asyncio
async def test_fetches_all_activity_pages(services, config):
    source = StravaSource("strava", config, services, 1)
    source.client.get = AsyncMock(side_effect=[
        make_response([activity(3), activity(2)]),
        make_response([activity(1)]),
    ])

    result = await source._fetch_activities(
        "access",
        datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert [item["id"] for item in result] == [3, 2, 1]
    assert source.client.get.await_count == 2
    assert source.client.get.await_args_list[0].kwargs["params"]["after"] == int(
        datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
    )
    await source.client.aclose()


@pytest.mark.asyncio
async def test_emits_created_then_updated_and_ignores_social_counts(services, config):
    source = StravaSource("strava", config, services, 1)

    await source._process_activity(activity())
    await source._process_activity(activity(kudos_count=99))
    await source._process_activity(activity(name="Evening Run", kudos_count=99))

    assert services.writer.write_events.call_count == 2
    created = services.writer.write_events.call_args_list[0].args[1][0]
    updated = services.writer.write_events.call_args_list[1].args[1][0]
    assert created.event_type == "strava.activity_created"
    assert created.event_id == "strava-42-created"
    assert created.entity_id == "42"
    assert created.occurred_at == datetime(2026, 8, 14, 6, 30, tzinfo=timezone.utc)
    assert updated.event_type == "strava.activity_updated"
    assert updated.data["changed_fields"] == {
        "name": {"before": "Morning Run", "after": "Evening Run"}
    }
    await source.client.aclose()


@pytest.mark.asyncio
async def test_poll_publishes_backfill_oldest_first(services, config):
    source = StravaSource("strava", config, services, 1)
    source._get_access_token = AsyncMock(return_value="access")
    source._fetch_activities = AsyncMock(return_value=[activity(2), activity(1)])

    await source.poll()

    events = [call.args[1][0] for call in services.writer.write_events.call_args_list]
    assert [event.entity_id for event in events] == ["1", "2"]
    await source.client.aclose()


@pytest.mark.asyncio
async def test_missing_credentials_fail_before_api_call(services):
    config = StravaSourceConfig(client_id="", client_secret="", refresh_token="")
    source = StravaSource("strava", config, services, 1)
    source.client.post = AsyncMock()

    with pytest.raises(ValueError, match="client_id, client_secret, refresh_token"):
        await source.poll()

    source.client.post.assert_not_awaited()
    await source.client.aclose()


def test_strava_config_defaults():
    config = StravaSourceConfig(
        client_id="123",
        client_secret="secret",
        refresh_token="refresh",
    )

    assert config.poll_interval == 900.0
    assert config.lookback_days == 7
    assert config.per_page == 100
    assert "kudos_count" in config.ignored_fields
