import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from src.sources.google_health import (
    GoogleHealthSource,
    _build_filter_string,
    _data_point_id,
    _extract_entity_id,
    _extract_timestamp,
    _parse_rfc3339,
)
from src.config import GoogleHealthSourceConfig
from src.schemas import NewEvent


@pytest.fixture
def mock_services():
    services = MagicMock()
    services.db_session_maker = MagicMock()
    services.cursor = MagicMock()
    services.writer = MagicMock()
    return services


@pytest.fixture
def config():
    return GoogleHealthSourceConfig(
        type="google_health",
        token_file="data/google_token.json",
        data_types=["steps", "weight", "sleep"],
        lookback_days=7,
        poll_interval=600,
    )


@pytest.fixture
def steps_response():
    return {
        "dataPoints": [
            {
                "name": "users/abc/dataTypes/steps/dataPoints/dp1",
                "steps": {
                    "count": "5432",
                    "interval": {
                        "startTime": "2026-04-14T00:00:00Z",
                        "endTime": "2026-04-14T23:59:59Z",
                    },
                },
            },
            {
                "name": "users/abc/dataTypes/steps/dataPoints/dp2",
                "steps": {
                    "count": "8765",
                    "interval": {
                        "startTime": "2026-04-13T00:00:00Z",
                        "endTime": "2026-04-13T23:59:59Z",
                    },
                },
            },
        ]
    }


@pytest.fixture
def weight_response():
    return {
        "dataPoints": [
            {
                "name": "users/abc/dataTypes/weight/dataPoints/w1",
                "weight": {
                    "value": 75.5,
                    "unit": "kg",
                    "sampleTime": {
                        "physicalTime": "2026-04-14T08:30:00Z",
                    },
                },
            }
        ]
    }


@pytest.fixture
def sleep_response():
    return {
        "dataPoints": [
            {
                "name": "users/abc/dataTypes/sleep/dataPoints/s1",
                "sleep": {
                    "interval": {
                        "startTime": "2026-04-13T22:30:00Z",
                        "endTime": "2026-04-14T06:45:00Z",
                    },
                    "summary": {"totalMinutes": "495"},
                    "createTime": "2026-04-14T07:00:00Z",
                },
            }
        ]
    }


# --- Unit tests for helper functions ---


class TestParseRfc3339:
    def test_z_suffix(self):
        result = _parse_rfc3339("2026-04-14T10:00:00Z")
        assert result == datetime(2026, 4, 14, 10, 0, 0, tzinfo=timezone.utc)

    def test_offset(self):
        result = _parse_rfc3339("2026-04-14T12:00:00+02:00")
        assert result is not None
        assert result.utcoffset() == timedelta(hours=2)

    def test_invalid(self):
        assert _parse_rfc3339("not-a-date") is None


class TestBuildFilterString:
    def test_steps_interval(self):
        dt = datetime(2026, 4, 14, 0, 0, 0, tzinfo=timezone.utc)
        result = _build_filter_string("steps", dt)
        assert result == 'steps.interval.start_time >= "2026-04-14T00:00:00Z"'

    def test_weight_sample(self):
        dt = datetime(2026, 4, 14, 0, 0, 0, tzinfo=timezone.utc)
        result = _build_filter_string("weight", dt)
        assert result == 'weight.sample_time.physical_time >= "2026-04-14T00:00:00Z"'

    def test_sleep_session(self):
        dt = datetime(2026, 4, 14, 0, 0, 0, tzinfo=timezone.utc)
        result = _build_filter_string("sleep", dt)
        assert result == 'sleep.interval.end_time >= "2026-04-14T00:00:00Z"'

    def test_exercise_session(self):
        dt = datetime(2026, 4, 14, 0, 0, 0, tzinfo=timezone.utc)
        result = _build_filter_string("exercise", dt)
        assert result == 'exercise.interval.civil_start_time >= "2026-04-14"'

    def test_daily_resting_heart_rate(self):
        dt = datetime(2026, 4, 14, 0, 0, 0, tzinfo=timezone.utc)
        result = _build_filter_string("daily-resting-heart-rate", dt)
        assert result == 'dailyRestingHeartRate.date >= "2026-04-14"'

    def test_heart_rate_sample(self):
        dt = datetime(2026, 4, 14, 0, 0, 0, tzinfo=timezone.utc)
        result = _build_filter_string("heart-rate", dt)
        assert result == 'heartRate.sample_time.physical_time >= "2026-04-14T00:00:00Z"'

    def test_unknown_type_returns_none(self):
        dt = datetime(2026, 4, 14, 0, 0, 0, tzinfo=timezone.utc)
        assert _build_filter_string("unknown-type", dt) is None


class TestDataPointId:
    def test_with_name(self):
        dp = {"name": "users/abc/dataTypes/steps/dataPoints/dp1"}
        assert _data_point_id("steps", dp) == "ghealth_steps_dp1"

    def test_without_name(self):
        dp = {"steps": {"count": "100"}}
        result = _data_point_id("steps", dp)
        assert result.startswith("ghealth_steps_")
        assert len(result) > len("ghealth_steps_")


class TestExtractEntityId:
    def test_with_name(self):
        dp = {"name": "users/abc/dataTypes/steps/dataPoints/dp1"}
        assert _extract_entity_id(dp) == "dp1"

    def test_without_name(self):
        assert _extract_entity_id({}) is None
        assert _extract_entity_id({"name": ""}) is None


class TestExtractTimestamp:
    def test_interval_start_time(self):
        dp = {"steps": {"interval": {"startTime": "2026-04-14T00:00:00Z"}}}
        result = _extract_timestamp("steps", dp)
        assert result == datetime(2026, 4, 14, 0, 0, 0, tzinfo=timezone.utc)

    def test_sample_time(self):
        dp = {"weight": {"sampleTime": {"physicalTime": "2026-04-14T08:30:00Z"}}}
        result = _extract_timestamp("weight", dp)
        assert result == datetime(2026, 4, 14, 8, 30, 0, tzinfo=timezone.utc)

    def test_create_time(self):
        dp = {"sleep": {"createTime": "2026-04-14T07:00:00Z"}}
        result = _extract_timestamp("sleep", dp)
        assert result == datetime(2026, 4, 14, 7, 0, 0, tzinfo=timezone.utc)

    def test_no_timestamp(self):
        dp = {"steps": {"count": "100"}}
        assert _extract_timestamp("steps", dp) is None


# --- Integration tests for GoogleHealthSource ---


@pytest.mark.asyncio
async def test_poll_success(mock_services, config, steps_response, weight_response, sleep_response):
    """Test a successful poll that fetches multiple data types."""
    source = GoogleHealthSource("test_health", config, mock_services, 1)

    mock_services.cursor.get_last_cursor = MagicMock(return_value=None)
    mock_services.cursor.set_cursor = MagicMock()
    mock_services.writer.write_events = MagicMock()

    def make_response(data):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = data
        resp.raise_for_status = MagicMock()
        return resp

    responses = {
        "steps": make_response(steps_response),
        "weight": make_response(weight_response),
        "sleep": make_response(sleep_response),
    }

    async def mock_get(url, **kwargs):
        for dt, resp in responses.items():
            if f"/dataTypes/{dt}/" in url:
                return resp
        raise ValueError(f"Unexpected URL: {url}")

    with patch("src.sources.google_health.get_google_credentials") as mock_creds:
        mock_creds.return_value = MagicMock(token="fake-token")
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get_fn:
            mock_get_fn.side_effect = mock_get
            await source.poll()

    # Should have written 4 events total (2 steps + 1 weight + 1 sleep)
    assert mock_services.writer.write_events.call_count == 1
    call_args = mock_services.writer.write_events.call_args
    source_id, events = call_args.args
    assert source_id == 1
    assert len(events) == 4

    # Verify event types
    event_types = [e.event_type for e in events]
    assert event_types.count("google.health.steps") == 2
    assert event_types.count("google.health.weight") == 1
    assert event_types.count("google.health.sleep") == 1

    # Verify event IDs
    assert events[0].event_id == "ghealth_steps_dp1"
    assert events[0].entity_id == "dp1"

    # Verify cursor was updated
    assert mock_services.cursor.set_cursor.call_count == 1


@pytest.mark.asyncio
async def test_poll_empty_response(mock_services, config):
    """Test poll with no data points returned."""
    source = GoogleHealthSource("test_health", config, mock_services, 1)

    mock_services.cursor.get_last_cursor = MagicMock(return_value=None)
    mock_services.cursor.set_cursor = MagicMock()
    mock_services.writer.write_events = MagicMock()

    empty_resp = MagicMock()
    empty_resp.status_code = 200
    empty_resp.json.return_value = {"dataPoints": []}
    empty_resp.raise_for_status = MagicMock()

    with patch("src.sources.google_health.get_google_credentials") as mock_creds:
        mock_creds.return_value = MagicMock(token="fake-token")
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = empty_resp
            await source.poll()

    # No events written
    assert mock_services.writer.write_events.call_count == 0
    # Cursor still updated
    assert mock_services.cursor.set_cursor.call_count == 1


@pytest.mark.asyncio
async def test_poll_uses_cursor(mock_services, config):
    """Test that poll uses the stored cursor as start time."""
    source = GoogleHealthSource("test_health", config, mock_services, 1)

    cursor_time = "2026-04-13T12:00:00+00:00"
    mock_services.cursor.get_last_cursor = MagicMock(return_value=cursor_time)
    mock_services.cursor.set_cursor = MagicMock()
    mock_services.writer.write_events = MagicMock()

    empty_resp = MagicMock()
    empty_resp.status_code = 200
    empty_resp.json.return_value = {"dataPoints": []}
    empty_resp.raise_for_status = MagicMock()

    with patch("src.sources.google_health.get_google_credentials") as mock_creds:
        mock_creds.return_value = MagicMock(token="fake-token")
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = empty_resp
            await source.poll()

    # Verify filter contains the cursor time
    calls = mock_get.call_args_list
    for call in calls:
        url = call.args[0] if call.args else call.kwargs.get("url", "")
        params = call.kwargs.get("params", {})
        if "steps" in url and "filter" in params:
            assert "2026-04-13" in params["filter"]


@pytest.mark.asyncio
async def test_poll_http_error_continues(mock_services, config):
    """Test that an HTTP error on one data type doesn't stop others."""
    source = GoogleHealthSource("test_health", config, mock_services, 1)

    mock_services.cursor.get_last_cursor = MagicMock(return_value=None)
    mock_services.cursor.set_cursor = MagicMock()
    mock_services.writer.write_events = MagicMock()

    import httpx

    error_response = MagicMock()
    error_response.status_code = 403
    error_response.text = "Forbidden"

    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {
        "dataPoints": [
            {"name": "users/abc/dataTypes/weight/dataPoints/w1", "weight": {"value": 75.5}}
        ]
    }
    ok_resp.raise_for_status = MagicMock()

    async def mock_get(url, **kwargs):
        if "/dataTypes/steps/" in url:
            raise httpx.HTTPStatusError("Forbidden", request=MagicMock(), response=error_response)
        return ok_resp

    with patch("src.sources.google_health.get_google_credentials") as mock_creds:
        mock_creds.return_value = MagicMock(token="fake-token")
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get_fn:
            mock_get_fn.side_effect = mock_get
            await source.poll()

    # Should still write events from weight and sleep (2 events, one per type)
    assert mock_services.writer.write_events.call_count == 1
    call_args = mock_services.writer.write_events.call_args
    _, events = call_args.args
    assert len(events) == 2  # weight + sleep both return ok_resp with 1 data point each


@pytest.mark.asyncio
async def test_poll_pagination(mock_services):
    """Test that pagination is handled correctly."""
    config = GoogleHealthSourceConfig(
        type="google_health",
        token_file="data/google_token.json",
        data_types=["steps"],
        lookback_days=7,
    )
    source = GoogleHealthSource("test_health", config, mock_services, 1)

    mock_services.cursor.get_last_cursor = MagicMock(return_value=None)
    mock_services.cursor.set_cursor = MagicMock()
    mock_services.writer.write_events = MagicMock()

    page1 = MagicMock()
    page1.status_code = 200
    page1.json.return_value = {
        "dataPoints": [{"name": "users/abc/dataTypes/steps/dataPoints/dp1", "steps": {"count": "100"}}],
        "nextPageToken": "token123",
    }
    page1.raise_for_status = MagicMock()

    page2 = MagicMock()
    page2.status_code = 200
    page2.json.return_value = {
        "dataPoints": [{"name": "users/abc/dataTypes/steps/dataPoints/dp2", "steps": {"count": "200"}}],
    }
    page2.raise_for_status = MagicMock()

    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return page1
        return page2

    with patch("src.sources.google_health.get_google_credentials") as mock_creds:
        mock_creds.return_value = MagicMock(token="fake-token")
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get_fn:
            mock_get_fn.side_effect = mock_get
            await source.poll()

    assert mock_services.writer.write_events.call_count == 1
    _, events = mock_services.writer.write_events.call_args.args
    assert len(events) == 2
    assert events[0].event_id == "ghealth_steps_dp1"
    assert events[1].event_id == "ghealth_steps_dp2"


class TestMapDataPoint:
    def test_steps_data_point(self, mock_services, config):
        source = GoogleHealthSource("test", config, mock_services, 1)
        dp = {
            "name": "users/abc/dataTypes/steps/dataPoints/dp1",
            "steps": {
                "count": "5432",
                "interval": {
                    "startTime": "2026-04-14T00:00:00Z",
                    "endTime": "2026-04-14T23:59:59Z",
                },
            },
        }
        event = source._map_data_point("steps", dp)
        assert isinstance(event, NewEvent)
        assert event.event_type == "google.health.steps"
        assert event.event_id == "ghealth_steps_dp1"
        assert event.entity_id == "dp1"
        assert event.occurred_at == datetime(2026, 4, 14, 0, 0, 0, tzinfo=timezone.utc)
        assert "name" not in event.data
        assert "steps" in event.data

    def test_weight_data_point(self, mock_services, config):
        source = GoogleHealthSource("test", config, mock_services, 1)
        dp = {
            "name": "users/abc/dataTypes/weight/dataPoints/w1",
            "weight": {
                "value": 75.5,
                "sampleTime": {"physicalTime": "2026-04-14T08:30:00Z"},
            },
        }
        event = source._map_data_point("weight", dp)
        assert event.event_type == "google.health.weight"
        assert event.occurred_at == datetime(2026, 4, 14, 8, 30, 0, tzinfo=timezone.utc)

    def test_heart_rate_kebab_case(self, mock_services, config):
        source = GoogleHealthSource("test", config, mock_services, 1)
        dp = {"name": "users/abc/dataTypes/heart-rate/dataPoints/hr1", "heartRate": {"bpm": 72}}
        event = source._map_data_point("heart-rate", dp)
        assert event.event_type == "google.health.heart_rate"
        assert event.entity_id == "hr1"
