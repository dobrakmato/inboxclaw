"""
GoCardless Bank Account Data (Nordigen) source.

Polls a single connected bank account for new transactions and emits events.
Each source instance handles exactly one GoCardless account_id. Configure
multiple sources if you have multiple bank accounts.

Poll scheduling and rate-limit state are stored in the per-source KV cache so
they survive restarts. The minimum poll interval is 6 hours to stay within
GoCardless's documented worst-case rate limit of 4 calls/day per account.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from src.config import NordigenSourceConfig
from src.schemas import NewEvent
from src.services import AppServices
from src.utils.nordigen_client import (
    Transaction,
    TransactionList,
    canonical_tx_id,
    fetch_transactions,
    parse_tx_date,
    refresh_access_token,
)

logger = logging.getLogger(__name__)

# How many days of overlap to use when fetching incremental transactions.
# Banks can retroactively change recent transactions, so we re-fetch a small window.
OVERLAP_DAYS = 3

# KV keys (all scoped to source_id automatically by SourceKVService)
_KV_LAST_POLL_AT = "last_poll_at"
_KV_NEXT_POLL_AT = "next_poll_at"
_KV_BACKOFF_UNTIL = "backoff_until"
_KV_ACCESS_TOKEN = "access_token"
_KV_ACCESS_EXPIRES_AT = "access_expires_at"
_KV_LAST_BOOKED_DATE = "last_booked_date"


class NordigenSource:
    """
    Source for GoCardless Bank Account Data (formerly Nordigen) transactions.

    Polls one connected bank account for new transactions and balance snapshots,
    emitting events for each booked or pending transaction. One source instance
    = one bank account.

    Poll scheduling is persisted in the KV cache so the 6-hour rate-limit
    budget is respected across restarts.
    """

    def __init__(
        self,
        name: str,
        config: NordigenSourceConfig,
        services: AppServices,
        source_id: int,
    ):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.writer = services.writer
        self.kv = services.kv

    # ------------------------------------------------------------------
    # Token management (backed by KV cache)
    # ------------------------------------------------------------------

    async def _get_access_token(self) -> str:
        """Return a valid access token, refreshing via KV-cached value if possible."""
        now = datetime.now(timezone.utc)

        cached_token = self.kv.get(self.source_id, _KV_ACCESS_TOKEN)
        cached_expires = self.kv.get(self.source_id, _KV_ACCESS_EXPIRES_AT)

        if cached_token and cached_expires:
            try:
                expires_at = datetime.fromisoformat(cached_expires)
                if now < expires_at:
                    return cached_token
            except ValueError:
                pass

        logger.debug("Refreshing Nordigen access token for source '%s'", self.name)
        token_resp = await refresh_access_token(self.config.refresh_token)

        expires_at = now + timedelta(seconds=token_resp.access_expires - 60)
        self.kv.set(self.source_id, _KV_ACCESS_TOKEN, token_resp.access)
        self.kv.set(self.source_id, _KV_ACCESS_EXPIRES_AT, expires_at.isoformat())
        return token_resp.access

    # ------------------------------------------------------------------
    # Poll scheduling (backed by KV cache)
    # ------------------------------------------------------------------

    def _is_in_backoff(self) -> bool:
        backoff_until_str = self.kv.get(self.source_id, _KV_BACKOFF_UNTIL)
        if not backoff_until_str:
            return False
        try:
            backoff_until = datetime.fromisoformat(backoff_until_str)
            return datetime.now(timezone.utc) < backoff_until
        except ValueError:
            return False

    def _set_backoff(self, seconds: float) -> None:
        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        self.kv.set(self.source_id, _KV_BACKOFF_UNTIL, until.isoformat())
        logger.info(
            "Nordigen source '%s': backing off for %.0fs (until %s)",
            self.name, seconds, until.isoformat()
        )

    def _record_poll(self) -> None:
        now = datetime.now(timezone.utc)
        next_poll = now + timedelta(seconds=self.config.effective_poll_interval)
        self.kv.set(self.source_id, _KV_LAST_POLL_AT, now.isoformat())
        self.kv.set(self.source_id, _KV_NEXT_POLL_AT, next_poll.isoformat())

    def _seconds_until_next_poll(self) -> float:
        next_poll_str = self.kv.get(self.source_id, _KV_NEXT_POLL_AT)
        if not next_poll_str:
            return 0.0
        try:
            next_poll = datetime.fromisoformat(next_poll_str)
            remaining = (next_poll - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, remaining)
        except ValueError:
            return 0.0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main polling loop."""
        logger.info(
            "Starting Nordigen source '%s' (account: %s), effective poll interval %.0fs",
            self.name,
            self.config.account_id or "(not configured)",
            self.config.effective_poll_interval,
        )

        # On startup, wait out any remaining time from the previous poll cycle
        wait = self._seconds_until_next_poll()
        if wait > 0:
            logger.info(
                "Nordigen source '%s': waiting %.0fs before first poll (resuming schedule)",
                self.name, wait,
            )
            await asyncio.sleep(wait)

        while True:
            if self._is_in_backoff():
                wait = self._seconds_until_next_poll()
                await asyncio.sleep(max(wait, 60))
                continue

            try:
                await self._poll()
            except Exception:
                logger.exception("Unexpected error in Nordigen source '%s' poll loop", self.name)

            self._record_poll()
            await asyncio.sleep(self.config.effective_poll_interval)

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------

    async def _poll(self) -> None:
        if not self.config.account_id:
            logger.warning(
                "Nordigen source '%s' has no account_id configured — skipping poll", self.name
            )
            return

        try:
            access_token = await self._get_access_token()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Nordigen source '%s': failed to refresh access token (%d)",
                self.name, exc.response.status_code,
            )
            self._set_backoff(3600)
            return

        account_id = self.config.account_id
        now = datetime.now(timezone.utc)
        today = now.date()

        last_booked_str = self.kv.get(self.source_id, _KV_LAST_BOOKED_DATE)

        if last_booked_str:
            try:
                last_booked = date.fromisoformat(last_booked_str)
            except ValueError:
                logger.warning(
                    "Nordigen source '%s': invalid last_booked_date '%s', resetting",
                    self.name, last_booked_str,
                )
                last_booked = today - timedelta(days=self.config.initial_history_days)
            date_from = last_booked - timedelta(days=OVERLAP_DAYS)
        else:
            date_from = today - timedelta(days=self.config.initial_history_days)

        date_to = today

        logger.info(
            "Nordigen source '%s': polling account '%s' from %s to %s",
            self.name, account_id, date_from.isoformat(), date_to.isoformat(),
        )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                tx_list: TransactionList = await fetch_transactions(
                    client, access_token, account_id, date_from, date_to
                )

        except httpx.HTTPStatusError as exc:
            self._handle_http_error(exc)
            return
        except Exception:
            logger.exception(
                "Nordigen source '%s': unexpected error polling account '%s'",
                self.name, self.config.account_id,
            )
            return

        events = []
        max_booked_date: Optional[date] = None

        for tx in tx_list.booked:
            events.append(self._map_transaction(tx, "booked"))
            tx_date = parse_tx_date(tx)
            if tx_date and (max_booked_date is None or tx_date > max_booked_date):
                max_booked_date = tx_date

        for tx in tx_list.pending:
            events.append(self._map_transaction(tx, "pending"))

        if events:
            self.writer.write_events(self.source_id, events)
            logger.info(
                "Nordigen source '%s': wrote %d booked + %d pending transactions",
                self.name, len(tx_list.booked), len(tx_list.pending),
            )

        # Advance checkpoint to the latest booked date seen (or today if none)
        new_last_booked = (max_booked_date or today).isoformat()
        self.kv.set(self.source_id, _KV_LAST_BOOKED_DATE, new_last_booked)

    def _map_transaction(self, tx: Transaction, status: str) -> NewEvent:
        account_id = self.config.account_id
        tx_id = canonical_tx_id(tx, account_id, status)

        amount_str = tx.transactionAmount.amount if tx.transactionAmount else "0"
        try:
            amount = float(amount_str)
        except (ValueError, TypeError):
            amount = 0.0
        currency = tx.transactionAmount.currency if tx.transactionAmount else ""

        if amount > 0:
            event_type = "nordigen.transaction.credit"
        elif amount < 0:
            event_type = "nordigen.transaction.debit"
        else:
            event_type = "nordigen.transaction"

        if status == "pending":
            event_type = f"{event_type}.pending"

        tx_date = parse_tx_date(tx)
        occurred_at: Optional[datetime] = None
        if tx_date:
            occurred_at = datetime.combine(tx_date, datetime.min.time(), tzinfo=timezone.utc)

        data = tx.model_dump(exclude_none=True)
        data["account_id"] = account_id
        data["status"] = status
        data["amount"] = amount
        data["currency"] = currency
        if self.config.label:
            data["account_label"] = self.config.label

        return NewEvent(
            event_id=tx_id,
            event_type=event_type,
            entity_id=None,
            occurred_at=occurred_at,
            data=data,
        )

    def _handle_http_error(self, exc: httpx.HTTPStatusError) -> None:
        status = exc.response.status_code
        account_id = self.config.account_id

        try:
            body = exc.response.json()
            summary = body.get("summary", "")
            detail = body.get("detail", "")
        except Exception:
            summary = ""
            detail = ""

        if status == 429:
            logger.warning(
                "Nordigen source '%s': rate limit hit for account '%s' — backing off 6h",
                self.name, account_id,
            )
            self._set_backoff(6 * 3600)

        elif status == 401:
            logger.error(
                "Nordigen source '%s': access expired or revoked for account '%s' (%s: %s). "
                "Reconnect the account.",
                self.name, account_id, summary, detail,
            )
            self.writer.write_events(self.source_id, [
                NewEvent(
                    event_id=f"nordigen_error_401_{account_id}_{datetime.now(timezone.utc).isoformat()}",
                    event_type="nordigen.error.access_expired",
                    entity_id=None,
                    data={
                        "account_id": account_id,
                        "source": self.name,
                        "summary": summary,
                        "detail": detail,
                        "action": "Reconnect the bank account using: python main.py nordigen connect",
                    },
                )
            ])
            self._set_backoff(24 * 3600)

        elif status == 403:
            logger.error(
                "Nordigen source '%s': access forbidden for account '%s' (%s: %s). "
                "The user may not have the necessary permissions.",
                self.name, account_id, summary, detail,
            )
            self.writer.write_events(self.source_id, [
                NewEvent(
                    event_id=f"nordigen_error_403_{account_id}_{datetime.now(timezone.utc).isoformat()}",
                    event_type="nordigen.error.access_forbidden",
                    entity_id=None,
                    data={
                        "account_id": account_id,
                        "source": self.name,
                        "summary": summary,
                        "detail": detail,
                        "action": "Check account permissions or reconnect the bank account.",
                    },
                )
            ])
            self._set_backoff(24 * 3600)

        elif status in (500, 503):
            logger.warning(
                "Nordigen source '%s': institution/service error (%d) for account '%s' — backing off 1h",
                self.name, status, account_id,
            )
            self._set_backoff(3600)

        else:
            logger.error(
                "Nordigen source '%s': HTTP %d for account '%s' (%s: %s)",
                self.name, status, account_id, summary, detail,
            )
