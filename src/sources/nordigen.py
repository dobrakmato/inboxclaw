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
    bootstrap_refresh_token,
    canonical_tx_id,
    fetch_transactions,
    parse_tx_date,
    parse_jwt_expiry,
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
_KV_REFRESH_TOKEN = "refresh_token"
_KV_REFRESH_EXPIRES_AT = "refresh_expires_at"
_KV_REFRESH_RETRY_AT = "refresh_retry_at"
_KV_LAST_BOOKED_DATE = "last_booked_date"

# Renew early enough to survive a short outage, but put a durable one-day lease
# in front of /token/new/ so a failure cannot cause every poll to mint again.
REFRESH_RENEWAL_WINDOW = timedelta(days=3)
REFRESH_RETRY_COOLDOWN = timedelta(days=1)
TOKEN_EXPIRY_SAFETY_MARGIN = timedelta(seconds=60)


class NordigenTokenRenewalDeferred(RuntimeError):
    """Automatic token minting is cooling down after a recent attempt."""

    def __init__(self, retry_at: datetime):
        self.retry_at = retry_at
        super().__init__(f"Nordigen token renewal deferred until {retry_at.isoformat()}")


class NordigenTokenMintFailed(NordigenTokenRenewalDeferred):
    """A guarded automatic token-minting attempt failed."""

    def __init__(self, retry_at: datetime, original: Exception):
        self.original = original
        super().__init__(retry_at)
        self.args = (
            f"Nordigen token renewal failed; next attempt after "
            f"{retry_at.isoformat()}: {original}",
        )


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
        self.health = services.health.reporter(name)

        # Sources using the same API credentials share one in-process lock and
        # one KV owner, avoiding simultaneous /token/new/ calls for each bank.
        lock_registry = getattr(services, "_nordigen_token_locks", None)
        if not isinstance(lock_registry, dict):
            lock_registry = {}
            setattr(services, "_nordigen_token_locks", lock_registry)
        credential_key = (
            ("credentials", config.secret_id, config.secret_key)
            if config.secret_id and config.secret_key
            else ("source", source_id)
        )
        self._token_lock = lock_registry.setdefault(credential_key, asyncio.Lock())

    # ------------------------------------------------------------------
    # Token management (backed by KV cache)
    # ------------------------------------------------------------------

    def _token_store_source_id(self) -> int:
        """Choose one KV owner for sources that share GoCardless credentials."""
        if not self._can_bootstrap():
            return self.source_id

        configured_sources = getattr(self.services, "sources", {})
        if not isinstance(configured_sources, dict):
            return self.source_id

        matching_ids = [self.source_id]
        for candidate in configured_sources.values():
            if not isinstance(candidate, NordigenSource):
                continue
            if (
                candidate.config.secret_id == self.config.secret_id
                and candidate.config.secret_key == self.config.secret_key
            ):
                matching_ids.append(candidate.source_id)
        return min(matching_ids)

    @staticmethod
    def _parse_kv_datetime(raw_value: object) -> Optional[datetime]:
        if not isinstance(raw_value, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw_value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _refresh_expiry(
        self,
        refresh_token: str,
        token_store_id: int,
        *,
        token_is_stored: bool,
    ) -> Optional[datetime]:
        jwt_expiry = parse_jwt_expiry(refresh_token)
        if jwt_expiry is not None:
            return jwt_expiry
        if token_is_stored:
            return self._parse_kv_datetime(
                self.kv.get(token_store_id, _KV_REFRESH_EXPIRES_AT)
            )
        return None

    def _refresh_retry_at(self, token_store_id: int) -> Optional[datetime]:
        return self._parse_kv_datetime(
            self.kv.get(token_store_id, _KV_REFRESH_RETRY_AT)
        )

    def _can_bootstrap(self) -> bool:
        return bool(self.config.secret_id and self.config.secret_key)

    def _emit_token_mint_failure(
        self,
        exc: Exception,
        now: datetime,
        retry_at: datetime,
        refresh_expires_at: Optional[datetime],
    ) -> None:
        detail = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                response_detail = exc.response.json().get("detail")
                if response_detail:
                    detail = str(response_detail)
            except Exception:
                pass

        remaining_seconds: Optional[int] = None
        remaining_days: Optional[float] = None
        integration_state = "authentication_unavailable"
        if refresh_expires_at is not None:
            remaining_seconds = max(
                0,
                int((refresh_expires_at - now).total_seconds()),
            )
            remaining_days = round(remaining_seconds / 86400, 1)
            if remaining_seconds > 0:
                integration_state = "degraded"

        if remaining_days is not None and remaining_days > 0:
            action = (
                f"Automatic renewal will retry after {retry_at.isoformat()}. "
                f"If renewal keeps failing, new access tokens cannot be obtained "
                f"after the refresh token expires in approximately {remaining_days} days. "
                "Check NORDIGEN_SECRET_ID and NORDIGEN_SECRET_KEY."
            )
        else:
            action = (
                f"Authentication is unavailable. Automatic renewal will retry after "
                f"{retry_at.isoformat()}. Check NORDIGEN_SECRET_ID and "
                "NORDIGEN_SECRET_KEY."
            )

        event = NewEvent(
            event_id=(
                f"nordigen_error_refresh_renewal_{self.source_id}_"
                f"{retry_at.isoformat()}"
            ),
            event_type="nordigen.error.refresh_renewal_failed",
            entity_id=None,
            occurred_at=now,
            data={
                "account_id": self.config.account_id,
                "source": self.name,
                "summary": "RefreshTokenRenewalFailed",
                "detail": detail,
                "failure_type": type(exc).__name__,
                "integration_state": integration_state,
                "refresh_token_expires_at": (
                    refresh_expires_at.isoformat() if refresh_expires_at else None
                ),
                "refresh_token_seconds_remaining": remaining_seconds,
                "refresh_token_days_remaining": remaining_days,
                "next_retry_at": retry_at.isoformat(),
                "action": action,
            },
        )
        try:
            self.writer.write_events(self.source_id, [event])
        except Exception:
            logger.exception(
                "Nordigen source '%s': failed to emit token-renewal error event",
                self.name,
            )

    async def _bootstrap_and_cache_tokens(
        self,
        token_store_id: int,
        now: datetime,
        current_refresh_expires_at: Optional[datetime] = None,
    ) -> str:
        """Mint and cache a token pair, guarded by a persisted retry lease."""
        retry_at = now + REFRESH_RETRY_COOLDOWN
        # Persist before the network request. A failure or process restart must
        # not turn the regular polling loop into a token-minting retry loop.
        self.kv.set(token_store_id, _KV_REFRESH_RETRY_AT, retry_at.isoformat())

        logger.info(
            "Nordigen credentials shared by source '%s': minting a new refresh token",
            self.name,
        )
        try:
            refresh, refresh_expires, access, access_expires = await bootstrap_refresh_token(
                self.config.secret_id,
                self.config.secret_key,
            )
            if not refresh:
                raise ValueError(
                    "GoCardless token/new response did not include a refresh token"
                )
        except Exception as exc:
            self._emit_token_mint_failure(
                exc,
                now,
                retry_at,
                current_refresh_expires_at,
            )
            raise NordigenTokenMintFailed(retry_at, exc) from exc

        refresh_expires_at = now + timedelta(seconds=refresh_expires)
        access_expires_at = now + timedelta(seconds=access_expires) - TOKEN_EXPIRY_SAFETY_MARGIN
        self.kv.set(token_store_id, _KV_REFRESH_TOKEN, refresh)
        self.kv.set(token_store_id, _KV_REFRESH_EXPIRES_AT, refresh_expires_at.isoformat())
        self.kv.set(token_store_id, _KV_ACCESS_TOKEN, access)
        self.kv.set(token_store_id, _KV_ACCESS_EXPIRES_AT, access_expires_at.isoformat())
        self.kv.delete(token_store_id, _KV_REFRESH_RETRY_AT)
        return access

    async def _get_access_token(self) -> str:
        """Return a valid access token and renew its refresh token when needed."""
        async with self._token_lock:
            now = datetime.now(timezone.utc)
            token_store_id = self._token_store_source_id()

            # Re-check inside the shared lock: another account may just have
            # renewed the credentials for every source using this secret pair.
            cached_token = self.kv.get(token_store_id, _KV_ACCESS_TOKEN)
            cached_expires = self._parse_kv_datetime(
                self.kv.get(token_store_id, _KV_ACCESS_EXPIRES_AT)
            )
            if cached_token and cached_expires and now < cached_expires:
                return cached_token

            stored_refresh = self.kv.get(token_store_id, _KV_REFRESH_TOKEN)
            refresh_token = stored_refresh or self.config.refresh_token
            if not refresh_token:
                if self._can_bootstrap():
                    retry_at = self._refresh_retry_at(token_store_id)
                    if retry_at and now < retry_at:
                        raise NordigenTokenRenewalDeferred(retry_at)
                    return await self._bootstrap_and_cache_tokens(token_store_id, now)
                raise ValueError(f"No refresh token available for Nordigen source '{self.name}'")

            refresh_expires_at = self._refresh_expiry(
                refresh_token,
                token_store_id,
                token_is_stored=bool(stored_refresh),
            )
            renewal_due = bool(
                refresh_expires_at
                and refresh_expires_at - now <= REFRESH_RENEWAL_WINDOW
            )
            retry_at = self._refresh_retry_at(token_store_id)

            if renewal_due and self._can_bootstrap():
                if not retry_at or now >= retry_at:
                    try:
                        return await self._bootstrap_and_cache_tokens(
                            token_store_id,
                            now,
                            refresh_expires_at,
                        )
                    except Exception as exc:
                        if refresh_expires_at and now < refresh_expires_at:
                            logger.warning(
                                "Nordigen source '%s': proactive refresh-token renewal failed; "
                                "using the current token until the next daily retry: %s",
                                self.name,
                                exc,
                            )
                        else:
                            raise
                elif refresh_expires_at and now >= refresh_expires_at:
                    raise NordigenTokenRenewalDeferred(retry_at)

            logger.debug("Refreshing Nordigen access token for source '%s'", self.name)
            try:
                token_resp = await refresh_access_token(refresh_token)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (401, 403) or not self._can_bootstrap():
                    raise

                retry_at = self._refresh_retry_at(token_store_id)
                if retry_at and now < retry_at:
                    raise NordigenTokenRenewalDeferred(retry_at) from exc
                return await self._bootstrap_and_cache_tokens(
                    token_store_id,
                    now,
                    refresh_expires_at,
                )

            # The documented refresh endpoint returns only an access token, but
            # retain compatibility if GoCardless ever supplies a rotated token.
            if token_resp.refresh:
                logger.info("Nordigen source '%s': refresh token rotated, saving to DB", self.name)
                self.kv.set(token_store_id, _KV_REFRESH_TOKEN, token_resp.refresh)
                rotated_expiry = parse_jwt_expiry(token_resp.refresh)
                if rotated_expiry:
                    self.kv.set(
                        token_store_id,
                        _KV_REFRESH_EXPIRES_AT,
                        rotated_expiry.isoformat(),
                    )

            expires_at = now + timedelta(seconds=token_resp.access_expires) - TOKEN_EXPIRY_SAFETY_MARGIN
            self.kv.set(token_store_id, _KV_ACCESS_TOKEN, token_resp.access)
            self.kv.set(token_store_id, _KV_ACCESS_EXPIRES_AT, expires_at.isoformat())
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
                self.health.unhealthy(
                    "backoff",
                    "The source is waiting for a persisted upstream retry window.",
                )
                wait = self._seconds_until_next_poll()
                await asyncio.sleep(max(wait, 60))
                continue

            self.health.checking()
            try:
                succeeded = await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("Unexpected error in Nordigen source '%s' poll loop", self.name)
                self.health.unhealthy_from_exception(error)
            else:
                if succeeded:
                    self.health.healthy()

            self._record_poll()
            await asyncio.sleep(self.config.effective_poll_interval)

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------

    async def _poll(self) -> bool:
        if not self.config.account_id:
            logger.warning(
                "Nordigen source '%s' has no account_id configured — skipping poll", self.name
            )
            self.health.unhealthy(
                "configuration",
                "No GoCardless account_id is configured.",
                action="Run 'inboxclaw nordigen connect' to connect an account.",
            )
            return False

        try:
            access_token = await self._get_access_token()
        except NordigenTokenRenewalDeferred as exc:
            remaining = max(
                60.0,
                (exc.retry_at - datetime.now(timezone.utc)).total_seconds(),
            )
            logger.warning(
                "Nordigen source '%s': automatic token renewal is cooling down until %s",
                self.name,
                exc.retry_at.isoformat(),
            )
            self._set_backoff(remaining)
            self.health.unhealthy(
                "authentication",
                "Automatic GoCardless token renewal is waiting for its retry window.",
                action="Reconnect the account if automatic renewal continues to fail.",
            )
            return False
        except httpx.HTTPStatusError as exc:
            self._handle_http_error(exc)
            return False

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
            return False
        except Exception as error:
            logger.exception(
                "Nordigen source '%s': unexpected error polling account '%s'",
                self.name, self.config.account_id,
            )
            self.health.unhealthy_from_exception(error)
            return False

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
        return True

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
                "Nordigen source '%s': rate limit hit for account '%s' (%s: %s) — backing off 6h",
                self.name, account_id, summary, detail,
            )
            self._set_backoff(6 * 3600)
            self.health.unhealthy(
                "rate_limited",
                "The GoCardless API rate limit was reached.",
            )

        elif status == 401:
            logger.error(
                "Nordigen source '%s': access expired or revoked for account '%s' (%s: %s). "
                "Reconnect the account.",
                self.name, account_id, summary, detail,
            )
            self.health.unhealthy(
                "expired",
                "GoCardless access expired or was revoked.",
                action="Reconnect the bank account using 'inboxclaw nordigen connect'.",
            )
            self.writer.write_events(self.source_id, [
                NewEvent(
                    event_id=(
                        f"nordigen_error_401_{account_id}_"
                        f"{datetime.now(timezone.utc).isoformat()}"
                    ),
                    event_type="nordigen.error.access_expired",
                    entity_id=None,
                    data={
                        "account_id": account_id,
                        "source": self.name,
                        "summary": summary,
                        "detail": detail,
                        "action": (
                            "Reconnect the bank account using: "
                            "inboxclaw nordigen connect"
                        ),
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
            self.health.unhealthy(
                "authorization",
                "GoCardless denied access to the configured account.",
                action="Check account permissions or reconnect the bank account.",
            )
            self.writer.write_events(self.source_id, [
                NewEvent(
                    event_id=(
                        f"nordigen_error_403_{account_id}_"
                        f"{datetime.now(timezone.utc).isoformat()}"
                    ),
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
                "Nordigen source '%s': institution/service error (%d) for account '%s' (%s: %s) — backing off 1h",
                self.name, status, account_id, summary, detail,
            )
            self._set_backoff(3600)
            self.health.unhealthy(
                "upstream",
                f"The GoCardless institution service returned HTTP {status}.",
            )

        else:
            logger.error(
                "Nordigen source '%s': HTTP %d for account '%s' (%s: %s)",
                self.name, status, account_id, summary, detail,
            )
            self.health.unhealthy_from_exception(exc)
