"""
GoCardless Bank Account Data (Nordigen) REST client.

Provides strongly-typed Pydantic models for API responses and async helper
functions for all GoCardless API calls. Used by both the polling source and
the CLI onboarding commands.
"""

import hashlib
import json
import logging
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

BASE_URL = "https://bankaccountdata.gocardless.com/api/v2"


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class TokenResponse(BaseModel):
    access: str
    access_expires: int = 86400
    refresh: Optional[str] = None
    refresh_expires: Optional[int] = None


class Institution(BaseModel):
    id: str
    name: str
    bic: Optional[str] = None
    transaction_total_days: Optional[int] = None
    countries: List[str] = Field(default_factory=list)
    logo: Optional[str] = None


class Agreement(BaseModel):
    id: str
    institution_id: str
    max_historical_days: int
    access_valid_for_days: int
    access_scope: List[str]
    accepted: Optional[str] = None
    created: Optional[str] = None


class Requisition(BaseModel):
    id: str
    status: str
    link: Optional[str] = None
    accounts: List[str] = Field(default_factory=list)
    institution_id: Optional[str] = None
    reference: Optional[str] = None
    redirect: Optional[str] = None


class AccountDetails(BaseModel):
    id: str
    status: Optional[str] = None
    institution_id: Optional[str] = None
    iban: Optional[str] = None
    owner_name: Optional[str] = None
    currency: Optional[str] = None


class TransactionAmount(BaseModel):
    amount: str
    currency: str


class Transaction(BaseModel):
    """A single bank transaction as returned by GoCardless."""
    internalTransactionId: Optional[str] = None
    transactionId: Optional[str] = None
    entryReference: Optional[str] = None
    bookingDate: Optional[str] = None
    bookingDateTime: Optional[str] = None
    valueDate: Optional[str] = None
    valueDateTime: Optional[str] = None
    transactionAmount: Optional[TransactionAmount] = None
    creditorName: Optional[str] = None
    debtorName: Optional[str] = None
    remittanceInformationUnstructured: Optional[str] = None
    remittanceInformationStructured: Optional[str] = None
    bankTransactionCode: Optional[str] = None
    additionalInformation: Optional[str] = None

    model_config = {"extra": "allow"}


class TransactionList(BaseModel):
    booked: List[Transaction] = Field(default_factory=list)
    pending: List[Transaction] = Field(default_factory=list)


class TransactionsResponse(BaseModel):
    transactions: TransactionList = Field(default_factory=TransactionList)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_tx_date(tx: Transaction) -> Optional[date]:
    """Extract the most relevant date from a transaction."""
    for raw in (tx.bookingDate, tx.bookingDateTime, tx.valueDate, tx.valueDateTime):
        if raw:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
            except ValueError:
                try:
                    return date.fromisoformat(raw[:10])
                except ValueError:
                    pass
    return None


def canonical_tx_id(tx: Transaction, account_id: str, status: str) -> str:
    """
    Compute a stable canonical ID for a transaction.

    Priority: internalTransactionId → transactionId → entryReference → fingerprint hash.
    """
    for value in (tx.internalTransactionId, tx.transactionId, tx.entryReference):
        if value:
            return f"nordigen_{account_id}_{value}"

    # Fallback: hash a normalized tuple of stable fields
    tx_date = parse_tx_date(tx)
    amount = tx.transactionAmount.amount if tx.transactionAmount else ""
    currency = tx.transactionAmount.currency if tx.transactionAmount else ""
    creditor = tx.creditorName or tx.debtorName or ""
    remittance = tx.remittanceInformationUnstructured or tx.remittanceInformationStructured or ""
    bank_code = tx.bankTransactionCode or ""

    fingerprint = json.dumps(
        [account_id, status, str(tx_date), amount, currency, creditor, remittance, bank_code],
        sort_keys=True,
    )
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
    return f"nordigen_{account_id}_fp_{digest}"


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def bootstrap_refresh_token(secret_id: str, secret_key: str) -> Tuple[str, int, str, int]:
    """
    Exchange secret_id + secret_key for a refresh token and initial access token.

    Returns (refresh_token, refresh_expires, access_token, access_expires).
    Called once during the ``nordigen connect`` onboarding flow.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{BASE_URL}/token/new/",
            json={"secret_id": secret_id, "secret_key": secret_key},
        )
        response.raise_for_status()
        data = TokenResponse.model_validate(response.json())
    return (
        data.refresh or "",
        data.refresh_expires or 2592000,
        data.access,
        data.access_expires,
    )


async def refresh_access_token(refresh_token: str) -> TokenResponse:
    """Mint a new short-lived access token from the stored refresh token."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{BASE_URL}/token/refresh/",
            json={"refresh": refresh_token},
        )
        response.raise_for_status()
        return TokenResponse.model_validate(response.json())


async def list_institutions(access_token: str, country: str) -> List[Institution]:
    """Fetch available institutions for a given ISO 3166-1 alpha-2 country code."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{BASE_URL}/institutions/",
            headers=_auth_headers(access_token),
            params={"country": country.upper()},
        )
        response.raise_for_status()
        return [Institution.model_validate(i) for i in response.json()]


async def create_requisition(
    access_token: str,
    institution_id: str,
    redirect_url: str,
    reference: str,
    max_historical_days: int,
    language: str = "EN",
) -> Requisition:
    """
    Create an end-user agreement and a requisition, then return the requisition.

    The agreement requests the maximum allowed history for the institution.
    """
    headers = _auth_headers(access_token)
    async with httpx.AsyncClient(timeout=30) as client:
        agreement_resp = await client.post(
            f"{BASE_URL}/agreements/enduser/",
            headers=headers,
            json={
                "institution_id": institution_id,
                "max_historical_days": max_historical_days,
                "access_valid_for_days": 90,
                "access_scope": ["details", "balances", "transactions"],
            },
        )
        agreement_resp.raise_for_status()

        req_resp = await client.post(
            f"{BASE_URL}/requisitions/",
            headers=headers,
            json={
                "redirect": redirect_url,
                "institution_id": institution_id,
                "reference": reference,
                "agreement": agreement_resp.json()["id"],
                "user_language": language,
            },
        )
        req_resp.raise_for_status()
        return Requisition.model_validate(req_resp.json())


async def get_requisition(access_token: str, requisition_id: str) -> Requisition:
    """Fetch the current state of a requisition (including linked account IDs)."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{BASE_URL}/requisitions/{requisition_id}/",
            headers=_auth_headers(access_token),
        )
        response.raise_for_status()
        return Requisition.model_validate(response.json())


async def get_account_details(access_token: str, account_id: str) -> AccountDetails:
    """Fetch metadata for a single GoCardless account."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{BASE_URL}/accounts/{account_id}/",
            headers=_auth_headers(access_token),
        )
        response.raise_for_status()
        return AccountDetails.model_validate(response.json())


async def fetch_transactions(
    client: httpx.AsyncClient,
    access_token: str,
    account_id: str,
    date_from: date,
    date_to: date,
) -> TransactionList:
    """Fetch booked and pending transactions for an account within a date window."""
    response = await client.get(
        f"{BASE_URL}/accounts/{account_id}/transactions/",
        headers=_auth_headers(access_token),
        params={
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
        },
    )
    response.raise_for_status()
    return TransactionsResponse.model_validate(response.json()).transactions
