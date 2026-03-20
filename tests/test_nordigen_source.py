"""
Tests for the Nordigen (GoCardless Bank Account Data) source.

Covers: KV-backed token caching, poll scheduling, backoff, transaction polling,
canonical ID generation, event mapping, error handling (401/403/429/5xx),
actionable error events, and CLI helpers.
"""

import hashlib
import json
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml

from src.config import NordigenSourceConfig, MIN_NORDIGEN_POLL_INTERVAL
from src.schemas import NewEvent
from src.sources.nordigen import (
    OVERLAP_DAYS,
    NordigenSource,
    _KV_ACCESS_EXPIRES_AT,
    _KV_ACCESS_TOKEN,
    _KV_BACKOFF_UNTIL,
    _KV_LAST_BOOKED_DATE,
    _KV_LAST_POLL_AT,
    _KV_NEXT_POLL_AT,
)
from src.utils.nordigen_client import (
    Institution,
    Transaction,
    TransactionAmount,
    TransactionList,
    bootstrap_refresh_token,
    canonical_tx_id,
    list_institutions,
    parse_tx_date,
)
from src.cli.commands.nordigen_connect import (
    _pick_accounts,
    _pick_institution,
    _update_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_kv():
    store: dict = {}
    kv = MagicMock()
    kv.get.side_effect = lambda source_id, key: store.get((source_id, key))
    kv.set.side_effect = lambda source_id, key, value: store.update({(source_id, key): value})
    return kv


@pytest.fixture
def mock_services(mock_kv):
    services = MagicMock()
    services.kv = mock_kv
    services.writer = MagicMock()
    return services


@pytest.fixture
def source_config():
    return NordigenSourceConfig(
        type="nordigen",
        secret_id="sid",
        secret_key="skey",
        refresh_token="rtoken",
        account_id="acc-123",
        label="Checking",
        poll_interval="6h",
        initial_history_days=90,
    )


@pytest.fixture
def source(source_config, mock_services):
    return NordigenSource("test_nordigen", source_config, mock_services, source_id=1)


def _make_httpx_response(status_code: int, json_data: Any) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _make_tx(
    internal_id: str = None,
    tx_id: str = None,
    entry_ref: str = None,
    amount: str = "100.00",
    currency: str = "EUR",
    booking_date: str = "2024-03-15",
) -> Transaction:
    return Transaction(
        internalTransactionId=internal_id,
        transactionId=tx_id,
        entryReference=entry_ref,
        transactionAmount=TransactionAmount(amount=amount, currency=currency),
        bookingDate=booking_date,
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestNordigenSourceConfig:
    def test_defaults(self):
        cfg = NordigenSourceConfig(secret_id="s", secret_key="k", refresh_token="r")
        assert cfg.poll_interval == pytest.approx(6 * 3600)
        assert cfg.initial_history_days == 90
        assert cfg.account_id == ""
        assert cfg.label is None

    def test_effective_poll_interval_enforces_minimum(self):
        # Even if poll_interval is set lower, effective is capped at 6h
        cfg = NordigenSourceConfig(
            secret_id="s", secret_key="k", refresh_token="r",
            poll_interval=60,  # 1 minute — below minimum
        )
        assert cfg.effective_poll_interval == MIN_NORDIGEN_POLL_INTERVAL

    def test_effective_poll_interval_respects_higher_value(self):
        cfg = NordigenSourceConfig(
            secret_id="s", secret_key="k", refresh_token="r",
            poll_interval="12h",
        )
        assert cfg.effective_poll_interval == pytest.approx(12 * 3600)

    def test_poll_interval_human_readable(self):
        cfg = NordigenSourceConfig(
            secret_id="s", secret_key="k", refresh_token="r",
            poll_interval="6h",
        )
        assert cfg.poll_interval == pytest.approx(6 * 3600)

    def test_label_and_account_id(self):
        cfg = NordigenSourceConfig(
            secret_id="s", secret_key="k", refresh_token="r",
            account_id="acc-1", label="Main",
        )
        assert cfg.account_id == "acc-1"
        assert cfg.label == "Main"


# ---------------------------------------------------------------------------
# KV-backed token management
# ---------------------------------------------------------------------------

class TestGetAccessToken:
    @pytest.mark.asyncio
    async def test_uses_cached_token_when_valid(self, source, mock_kv):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        mock_kv.set(1, _KV_ACCESS_TOKEN, "cached_tok")
        mock_kv.set(1, _KV_ACCESS_EXPIRES_AT, future)

        token = await source._get_access_token()

        assert token == "cached_tok"

    @pytest.mark.asyncio
    async def test_refreshes_when_no_cache(self, source, mock_kv):
        with patch("src.sources.nordigen.refresh_access_token", new_callable=AsyncMock) as mock_refresh:
            mock_refresh.return_value = MagicMock(access="new_tok", access_expires=86400)
            token = await source._get_access_token()

        assert token == "new_tok"

    @pytest.mark.asyncio
    async def test_refreshes_when_token_expired(self, source, mock_kv):
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        mock_kv.set(1, _KV_ACCESS_TOKEN, "old_tok")
        mock_kv.set(1, _KV_ACCESS_EXPIRES_AT, past)

        with patch("src.sources.nordigen.refresh_access_token", new_callable=AsyncMock) as mock_refresh:
            mock_refresh.return_value = MagicMock(access="fresh_tok", access_expires=86400)
            token = await source._get_access_token()

        assert token == "fresh_tok"

    @pytest.mark.asyncio
    async def test_stores_new_token_in_kv(self, source, mock_kv):
        with patch("src.sources.nordigen.refresh_access_token", new_callable=AsyncMock) as mock_refresh:
            mock_refresh.return_value = MagicMock(access="stored_tok", access_expires=86400)
            await source._get_access_token()

        assert mock_kv.get(1, _KV_ACCESS_TOKEN) == "stored_tok"
        assert mock_kv.get(1, _KV_ACCESS_EXPIRES_AT) is not None


# ---------------------------------------------------------------------------
# Poll scheduling / backoff
# ---------------------------------------------------------------------------

class TestPollScheduling:
    def test_not_in_backoff_when_no_kv(self, source):
        assert source._is_in_backoff() is False

    def test_in_backoff_when_future_timestamp(self, source, mock_kv):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        mock_kv.set(1, _KV_BACKOFF_UNTIL, future)
        assert source._is_in_backoff() is True

    def test_not_in_backoff_when_past_timestamp(self, source, mock_kv):
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        mock_kv.set(1, _KV_BACKOFF_UNTIL, past)
        assert source._is_in_backoff() is False

    def test_set_backoff_stores_future_timestamp(self, source, mock_kv):
        source._set_backoff(3600)
        stored = mock_kv.get(1, _KV_BACKOFF_UNTIL)
        assert stored is not None
        until = datetime.fromisoformat(stored)
        assert until > datetime.now(timezone.utc)

    def test_record_poll_stores_last_and_next(self, source, mock_kv):
        source._record_poll()
        assert mock_kv.get(1, _KV_LAST_POLL_AT) is not None
        assert mock_kv.get(1, _KV_NEXT_POLL_AT) is not None

    def test_seconds_until_next_poll_zero_when_no_kv(self, source):
        assert source._seconds_until_next_poll() == 0.0

    def test_seconds_until_next_poll_positive_when_future(self, source, mock_kv):
        future = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        mock_kv.set(1, _KV_NEXT_POLL_AT, future)
        remaining = source._seconds_until_next_poll()
        assert remaining > 0

    def test_seconds_until_next_poll_zero_when_past(self, source, mock_kv):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        mock_kv.set(1, _KV_NEXT_POLL_AT, past)
        assert source._seconds_until_next_poll() == 0.0


# ---------------------------------------------------------------------------
# parse_tx_date
# ---------------------------------------------------------------------------

class TestParseTxDate:
    def test_booking_date(self):
        tx = Transaction(bookingDate="2024-03-15")
        assert parse_tx_date(tx) == date(2024, 3, 15)

    def test_booking_datetime(self):
        tx = Transaction(bookingDateTime="2024-03-15T10:30:00Z")
        assert parse_tx_date(tx) == date(2024, 3, 15)

    def test_value_date_fallback(self):
        tx = Transaction(valueDate="2024-01-01")
        assert parse_tx_date(tx) == date(2024, 1, 1)

    def test_no_date_returns_none(self):
        tx = Transaction()
        assert parse_tx_date(tx) is None

    def test_prefers_booking_over_value(self):
        tx = Transaction(bookingDate="2024-03-15", valueDate="2024-03-10")
        assert parse_tx_date(tx) == date(2024, 3, 15)


# ---------------------------------------------------------------------------
# canonical_tx_id
# ---------------------------------------------------------------------------

class TestCanonicalTxId:
    def test_uses_internal_transaction_id(self):
        tx = Transaction(internalTransactionId="INT001")
        assert canonical_tx_id(tx, "acc-1", "booked") == "nordigen_acc-1_INT001"

    def test_falls_back_to_transaction_id(self):
        tx = Transaction(transactionId="TXN999")
        assert canonical_tx_id(tx, "acc-1", "booked") == "nordigen_acc-1_TXN999"

    def test_falls_back_to_entry_reference(self):
        tx = Transaction(entryReference="REF42")
        assert canonical_tx_id(tx, "acc-1", "booked") == "nordigen_acc-1_REF42"

    def test_fingerprint_fallback(self):
        tx = Transaction(
            transactionAmount=TransactionAmount(amount="100.00", currency="EUR"),
            bookingDate="2024-03-15",
            creditorName="Shop",
        )
        result = canonical_tx_id(tx, "acc-1", "booked")
        assert result.startswith("nordigen_acc-1_fp_")
        assert len(result) == len("nordigen_acc-1_fp_") + 16

    def test_fingerprint_is_deterministic(self):
        tx = Transaction(transactionAmount=TransactionAmount(amount="50.00", currency="CZK"))
        r1 = canonical_tx_id(tx, "acc-x", "pending")
        r2 = canonical_tx_id(tx, "acc-x", "pending")
        assert r1 == r2

    def test_fingerprint_differs_by_status(self):
        tx = Transaction(transactionAmount=TransactionAmount(amount="50.00", currency="CZK"))
        assert canonical_tx_id(tx, "acc-x", "booked") != canonical_tx_id(tx, "acc-x", "pending")

    def test_internal_id_takes_priority_over_transaction_id(self):
        tx = Transaction(internalTransactionId="INT1", transactionId="TXN1")
        assert canonical_tx_id(tx, "acc-1", "booked") == "nordigen_acc-1_INT1"


# ---------------------------------------------------------------------------
# NordigenSource._map_transaction
# ---------------------------------------------------------------------------

class TestMapTransaction:
    def test_credit_event_type(self, source):
        tx = _make_tx(internal_id="T1", amount="100.00")
        event = source._map_transaction(tx, "booked")
        assert event.event_type == "nordigen.transaction.credit"
        assert event.data["amount"] == 100.0
        assert event.data["currency"] == "EUR"
        assert event.data["account_label"] == "Checking"
        assert event.data["status"] == "booked"

    def test_debit_event_type(self, source):
        tx = _make_tx(internal_id="T2", amount="-50.00")
        event = source._map_transaction(tx, "booked")
        assert event.event_type == "nordigen.transaction.debit"

    def test_zero_amount_event_type(self, source):
        tx = _make_tx(internal_id="T3", amount="0.00")
        event = source._map_transaction(tx, "booked")
        assert event.event_type == "nordigen.transaction"

    def test_pending_suffix(self, source):
        tx = _make_tx(internal_id="T4", amount="10.00")
        event = source._map_transaction(tx, "pending")
        assert event.event_type == "nordigen.transaction.credit.pending"

    def test_occurred_at_set_from_booking_date(self, source):
        tx = _make_tx(internal_id="T5", booking_date="2024-03-15")
        event = source._map_transaction(tx, "booked")
        assert event.occurred_at == datetime(2024, 3, 15, tzinfo=timezone.utc)

    def test_occurred_at_none_when_no_date(self, source):
        tx = Transaction(
            internalTransactionId="T6",
            transactionAmount=TransactionAmount(amount="10.00", currency="EUR"),
        )
        event = source._map_transaction(tx, "booked")
        assert event.occurred_at is None

    def test_entity_id_is_none(self, source):
        tx = _make_tx(internal_id="T7")
        event = source._map_transaction(tx, "booked")
        assert event.entity_id is None

    def test_no_label_when_not_configured(self, source_config, mock_services):
        source_config = NordigenSourceConfig(
            type="nordigen", secret_id="s", secret_key="k",
            refresh_token="r", account_id="acc-1", label=None,
        )
        src = NordigenSource("no_label", source_config, mock_services, source_id=2)
        tx = _make_tx(internal_id="T8")
        event = src._map_transaction(tx, "booked")
        assert "account_label" not in event.data

    def test_account_id_in_data(self, source):
        tx = _make_tx(internal_id="T9")
        event = source._map_transaction(tx, "booked")
        assert event.data["account_id"] == "acc-123"


# ---------------------------------------------------------------------------
# NordigenSource._poll_account
# ---------------------------------------------------------------------------

SAMPLE_TX_LIST = {
    "transactions": {
        "booked": [
            {"internalTransactionId": "B1", "transactionAmount": {"amount": "200.00", "currency": "EUR"}, "bookingDate": "2024-03-15"},
            {"internalTransactionId": "B2", "transactionAmount": {"amount": "-30.00", "currency": "EUR"}, "bookingDate": "2024-03-14"},
        ],
        "pending": [
            {"internalTransactionId": "P1", "transactionAmount": {"amount": "50.00", "currency": "EUR"}},
        ],
    }
}


@pytest.mark.asyncio
async def test_poll_account_first_sync(source, mock_kv):
    """On first sync (no KV checkpoint), fetches initial_history_days of history."""
    tx_list = TransactionList(
        booked=[
            Transaction(internalTransactionId="B1", transactionAmount=TransactionAmount(amount="100.00", currency="EUR"), bookingDate="2024-03-15"),
            Transaction(internalTransactionId="B2", transactionAmount=TransactionAmount(amount="-30.00", currency="EUR"), bookingDate="2024-03-14"),
        ],
        pending=[
            Transaction(internalTransactionId="P1", transactionAmount=TransactionAmount(amount="50.00", currency="EUR")),
        ],
    )

    with patch("src.sources.nordigen.datetime") as mock_dt, \
         patch("src.sources.nordigen.fetch_transactions", new_callable=AsyncMock) as mock_fetch, \
         patch("src.sources.nordigen.refresh_access_token", new_callable=AsyncMock) as mock_refresh:
        mock_dt.now.return_value = datetime(2024, 3, 16, tzinfo=timezone.utc)
        mock_dt.combine = datetime.combine
        mock_dt.min = datetime.min
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_refresh.return_value = MagicMock(access="tok", access_expires=86400)
        mock_fetch.return_value = tx_list

        await source._poll()

    source.writer.write_events.assert_called_once()
    _, events = source.writer.write_events.call_args.args
    assert len(events) == 3  # 2 booked + 1 pending

    # Checkpoint advanced to max booked date
    assert mock_kv.get(1, _KV_LAST_BOOKED_DATE) == "2024-03-15"


@pytest.mark.asyncio
async def test_poll_account_incremental_uses_overlap(source, mock_kv):
    """On subsequent syncs, date_from = last_booked - OVERLAP_DAYS."""
    mock_kv.set(1, _KV_LAST_BOOKED_DATE, "2024-03-10")
    empty_list = TransactionList(booked=[], pending=[])

    with patch("src.sources.nordigen.datetime") as mock_dt, \
         patch("src.sources.nordigen.fetch_transactions", new_callable=AsyncMock) as mock_fetch, \
         patch("src.sources.nordigen.refresh_access_token", new_callable=AsyncMock) as mock_refresh:
        mock_dt.now.return_value = datetime(2024, 3, 16, tzinfo=timezone.utc)
        mock_dt.combine = datetime.combine
        mock_dt.min = datetime.min
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_refresh.return_value = MagicMock(access="tok", access_expires=86400)
        mock_fetch.return_value = empty_list

        await source._poll()

    expected_from = (date(2024, 3, 10) - timedelta(days=OVERLAP_DAYS))
    mock_fetch.assert_called_once()
    _, _, _, date_from, date_to = mock_fetch.call_args.args
    assert date_from == expected_from
    assert date_to == date(2024, 3, 16)


@pytest.mark.asyncio
async def test_poll_account_no_events_still_advances_checkpoint(source, mock_kv):
    """Even with no transactions, checkpoint is advanced to today."""
    mock_kv.set(1, _KV_LAST_BOOKED_DATE, "2024-03-10")
    empty_list = TransactionList(booked=[], pending=[])

    with patch("src.sources.nordigen.datetime") as mock_dt, \
         patch("src.sources.nordigen.fetch_transactions", new_callable=AsyncMock) as mock_fetch, \
         patch("src.sources.nordigen.refresh_access_token", new_callable=AsyncMock) as mock_refresh:
        mock_dt.now.return_value = datetime(2024, 3, 16, tzinfo=timezone.utc)
        mock_dt.combine = datetime.combine
        mock_dt.min = datetime.min
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_refresh.return_value = MagicMock(access="tok", access_expires=86400)
        mock_fetch.return_value = empty_list

        await source._poll()

    source.writer.write_events.assert_not_called()
    assert mock_kv.get(1, _KV_LAST_BOOKED_DATE) == "2024-03-16"


@pytest.mark.asyncio
async def test_poll_account_invalid_checkpoint_resets(source, mock_kv):
    """An invalid checkpoint value is treated as a fresh start (no crash)."""
    mock_kv.set(1, _KV_LAST_BOOKED_DATE, "not-a-date")
    empty_list = TransactionList(booked=[], pending=[])

    with patch("src.sources.nordigen.datetime") as mock_dt, \
         patch("src.sources.nordigen.fetch_transactions", new_callable=AsyncMock) as mock_fetch, \
         patch("src.sources.nordigen.refresh_access_token", new_callable=AsyncMock) as mock_refresh:
        mock_dt.now.return_value = datetime(2024, 3, 16, tzinfo=timezone.utc)
        mock_dt.combine = datetime.combine
        mock_dt.min = datetime.min
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_refresh.return_value = MagicMock(access="tok", access_expires=86400)
        mock_fetch.return_value = empty_list

        await source._poll()  # should not raise


@pytest.mark.asyncio
async def test_poll_skips_when_no_account_id(mock_services):
    """Source with no account_id configured skips the poll gracefully."""
    cfg = NordigenSourceConfig(
        type="nordigen", secret_id="s", secret_key="k", refresh_token="r",
        account_id="",
    )
    src = NordigenSource("empty", cfg, mock_services, source_id=99)
    await src._poll()
    mock_services.writer.write_events.assert_not_called()


# ---------------------------------------------------------------------------
# Error handling — _handle_http_error
# ---------------------------------------------------------------------------

class TestHandleHttpError:
    def _make_exc(self, status: int, body: dict) -> httpx.HTTPStatusError:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status
        resp.json.return_value = body
        return httpx.HTTPStatusError(f"HTTP {status}", request=MagicMock(), response=resp)

    def test_429_sets_backoff(self, source, mock_kv):
        exc = self._make_exc(429, {"summary": "RateLimitError"})
        source._handle_http_error(exc)
        assert mock_kv.get(1, _KV_BACKOFF_UNTIL) is not None
        source.writer.write_events.assert_not_called()

    def test_401_emits_error_event_and_sets_backoff(self, source, mock_kv):
        exc = self._make_exc(401, {"summary": "AccessExpiredError", "detail": "Access has expired"})
        source._handle_http_error(exc)

        source.writer.write_events.assert_called_once()
        events = source.writer.write_events.call_args.args[1]
        assert len(events) == 1
        assert events[0].event_type == "nordigen.error.access_expired"
        assert events[0].entity_id is None
        assert "action" in events[0].data
        assert mock_kv.get(1, _KV_BACKOFF_UNTIL) is not None

    def test_403_emits_error_event_and_sets_backoff(self, source, mock_kv):
        exc = self._make_exc(403, {"summary": "AccountAccessForbidden", "detail": "Forbidden"})
        source._handle_http_error(exc)

        source.writer.write_events.assert_called_once()
        events = source.writer.write_events.call_args.args[1]
        assert events[0].event_type == "nordigen.error.access_forbidden"
        assert events[0].entity_id is None
        assert mock_kv.get(1, _KV_BACKOFF_UNTIL) is not None

    def test_500_sets_backoff_no_event(self, source, mock_kv):
        exc = self._make_exc(500, {"summary": "ServiceError"})
        source._handle_http_error(exc)
        assert mock_kv.get(1, _KV_BACKOFF_UNTIL) is not None
        source.writer.write_events.assert_not_called()

    def test_503_sets_backoff_no_event(self, source, mock_kv):
        exc = self._make_exc(503, {"summary": "ConnectionError"})
        source._handle_http_error(exc)
        assert mock_kv.get(1, _KV_BACKOFF_UNTIL) is not None
        source.writer.write_events.assert_not_called()

    def test_other_error_logs_no_backoff(self, source, mock_kv):
        exc = self._make_exc(400, {"summary": "Bad Request"})
        source._handle_http_error(exc)
        # No backoff set for unknown errors
        assert mock_kv.get(1, _KV_BACKOFF_UNTIL) is None
        source.writer.write_events.assert_not_called()


# ---------------------------------------------------------------------------
# bootstrap_refresh_token (via nordigen_client)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bootstrap_refresh_token_success():
    mock_resp = _make_httpx_response(200, {
        "refresh": "rtoken123",
        "refresh_expires": 2592000,
        "access": "atoken456",
        "access_expires": 86400,
    })
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        refresh, r_exp, access, a_exp = await bootstrap_refresh_token("sid", "skey")

    assert refresh == "rtoken123"
    assert r_exp == 2592000
    assert access == "atoken456"
    assert a_exp == 86400


@pytest.mark.asyncio
async def test_bootstrap_refresh_token_http_error():
    mock_resp = _make_httpx_response(401, {"detail": "invalid credentials"})
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError):
            await bootstrap_refresh_token("bad", "creds")


# ---------------------------------------------------------------------------
# list_institutions (via nordigen_client)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_institutions():
    raw = [{"id": "MONZO_GB", "name": "Monzo", "countries": ["GB"]}]
    mock_resp = _make_httpx_response(200, raw)
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        result = await list_institutions("tok", "gb")

    assert len(result) == 1
    assert result[0].id == "MONZO_GB"
    assert result[0].name == "Monzo"


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _inst(id_: str, name: str) -> Institution:
    return Institution(id=id_, name=name)


class TestPickAccounts:
    def test_single_account_returns_immediately(self):
        result = _pick_accounts(["acc-1"])
        assert result == ["acc-1"]

    def test_empty_input_returns_all(self):
        with patch("click.prompt", return_value=""):
            result = _pick_accounts(["acc-1", "acc-2"])
        assert result == ["acc-1", "acc-2"]

    def test_valid_selection(self):
        with patch("click.prompt", return_value="1"):
            result = _pick_accounts(["acc-1", "acc-2"])
        assert result == ["acc-1"]

    def test_multiple_selection(self):
        with patch("click.prompt", return_value="1, 2"):
            result = _pick_accounts(["acc-1", "acc-2", "acc-3"])
        assert result == ["acc-1", "acc-2"]

    def test_invalid_number_falls_back_to_all(self):
        with patch("click.prompt", return_value="99"):
            result = _pick_accounts(["acc-1", "acc-2"])
        assert result == ["acc-1", "acc-2"]


class TestPickInstitution:
    INSTITUTIONS = [
        _inst("MONZO_GB", "Monzo"),
        _inst("BARCLAYS_GB", "Barclays"),
        _inst("HSBC_GB", "HSBC"),
    ]

    def test_single_match_returns_directly(self):
        with patch("click.prompt", return_value="monzo"):
            result = _pick_institution(self.INSTITUTIONS)
        assert result.id == "MONZO_GB"

    def test_multiple_matches_then_select(self):
        # "bar" matches Barclays only by name, but let's use "arc" which only hits Barclays
        # "hs" matches HSBC by name and HSBC_GB by id — two matches; select 1 = HSBC
        with patch("click.prompt", side_effect=["hs", "1"]):
            result = _pick_institution(self.INSTITUTIONS)
        assert result.id == "HSBC_GB"

    def test_no_match_retries(self):
        with patch("click.prompt", side_effect=["zzz", "hsbc"]):
            result = _pick_institution(self.INSTITUTIONS)
        assert result.id == "HSBC_GB"


class TestUpdateConfig:
    def test_creates_new_source_entry(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("sources: {}\n")

        _update_config(config_file, "nordigen_abc", "acc-1", "Checking")

        data = yaml.safe_load(config_file.read_text())
        src = data["sources"]["nordigen_abc"]
        assert src["account_id"] == "acc-1"
        assert src["label"] == "Checking"
        assert src["type"] == "nordigen"

    def test_skips_duplicate_account_id(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "sources": {"nordigen_abc": {"type": "nordigen", "account_id": "acc-1"}}
        }))

        _update_config(config_file, "nordigen_abc", "acc-1", None)

        data = yaml.safe_load(config_file.read_text())
        # Still only one source
        assert len(data["sources"]) == 1

    def test_preserves_other_sources(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "sources": {"fio": {"type": "fio", "token": "tok"}}
        }))

        _update_config(config_file, "nordigen_abc", "acc-1", None)

        data = yaml.safe_load(config_file.read_text())
        assert "fio" in data["sources"]
        assert data["sources"]["fio"]["token"] == "tok"

    def test_no_label_field_when_none(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("sources: {}\n")

        _update_config(config_file, "nordigen_abc", "acc-1", None)

        data = yaml.safe_load(config_file.read_text())
        assert "label" not in data["sources"]["nordigen_abc"]

    def test_creates_file_if_missing(self, tmp_path):
        config_file = tmp_path / "new_config.yaml"
        assert not config_file.exists()

        _update_config(config_file, "nordigen_abc", "acc-1", None)

        assert config_file.exists()
        data = yaml.safe_load(config_file.read_text())
        assert data["sources"]["nordigen_abc"]["account_id"] == "acc-1"

    def test_different_source_names_for_different_accounts(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("sources: {}\n")

        _update_config(config_file, "nordigen_acc1", "acc-1", "Checking")
        _update_config(config_file, "nordigen_acc2", "acc-2", "Savings")

        data = yaml.safe_load(config_file.read_text())
        assert "nordigen_acc1" in data["sources"]
        assert "nordigen_acc2" in data["sources"]
        assert data["sources"]["nordigen_acc1"]["account_id"] == "acc-1"
        assert data["sources"]["nordigen_acc2"]["account_id"] == "acc-2"
