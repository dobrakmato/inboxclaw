from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx
from google.oauth2.credentials import Credentials


DRIVE_API_BASE_URL = "https://www.googleapis.com/drive/v3"
RETRYABLE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})


@dataclass(frozen=True)
class DriveApiError(Exception):
    status: int
    message: str
    reasons: frozenset[str]
    response_body: str

    def __str__(self) -> str:
        reasons = f" ({', '.join(sorted(self.reasons))})" if self.reasons else ""
        return f"Google Drive API returned {self.status}{reasons}: {self.message}"

    @classmethod
    def from_response(cls, response: httpx.Response) -> "DriveApiError":
        reasons: set[str] = set()
        message = response.reason_phrase or "Request failed"
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                if isinstance(error.get("message"), str):
                    message = error["message"]
                details = error.get("errors")
                if isinstance(details, list):
                    for detail in details:
                        if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
                            reasons.add(detail["reason"])

        return cls(
            status=response.status_code,
            message=message,
            reasons=frozenset(reasons),
            response_body=response.text,
        )

    @property
    def retryable(self) -> bool:
        return (
            self.status == 429
            or 500 <= self.status <= 599
            or bool(self.reasons & RETRYABLE_REASONS)
        )


class AsyncGoogleDriveClient:
    """Small async client for the Drive v3 endpoints used by the source."""

    def __init__(
        self,
        credentials_loader: Callable[[bool], Credentials],
        *,
        max_retries: int = 3,
        timeout_seconds: float = 60.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._credentials_loader = credentials_loader
        self._credentials: Optional[Credentials] = None
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=DRIVE_API_BASE_URL,
            timeout=timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> "AsyncGoogleDriveClient":
        await self._load_credentials(force_refresh=False)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _load_credentials(self, *, force_refresh: bool) -> None:
        self._credentials = await asyncio.to_thread(self._credentials_loader, force_refresh)

    async def _headers(self) -> dict[str, str]:
        if self._credentials is None or self._credentials.expired:
            await self._load_credentials(force_refresh=False)
        if self._credentials is None or not self._credentials.token:
            raise RuntimeError("Google Drive OAuth credentials do not contain an access token")
        return {"Authorization": f"Bearer {self._credentials.token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> httpx.Response:
        refreshed_after_unauthorized = False
        retry_attempt = 0
        while True:
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    headers=await self._headers(),
                )
            except httpx.TransportError:
                if retry_attempt >= self._max_retries:
                    raise
                delay = (2**retry_attempt) + random.uniform(0.0, 0.5)
                retry_attempt += 1
                await asyncio.sleep(delay)
                continue
            if response.is_success:
                return response

            error = DriveApiError.from_response(response)
            if error.status == 401 and not refreshed_after_unauthorized:
                await self._load_credentials(force_refresh=True)
                refreshed_after_unauthorized = True
                continue

            if not error.retryable or retry_attempt >= self._max_retries:
                raise error

            delay = (2**retry_attempt) + random.uniform(0.0, 0.5)
            retry_attempt += 1
            await asyncio.sleep(delay)

    async def get_start_page_token(self, *, drive_id: Optional[str] = None) -> str:
        params: dict[str, Any] = {"supportsAllDrives": True}
        if drive_id:
            params["driveId"] = drive_id
        response = await self._request("GET", "/changes/startPageToken", params=params)
        token = response.json().get("startPageToken")
        if not token:
            raise RuntimeError("Google Drive did not return startPageToken")
        return str(token)

    async def list_changes(
        self,
        page_token: str,
        *,
        include_corpus_removals: bool,
        restrict_to_my_drive: bool,
        drive_id: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pageToken": page_token,
            "spaces": "drive",
            "includeRemoved": True,
            "includeCorpusRemovals": include_corpus_removals,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if drive_id:
            params["driveId"] = drive_id
        else:
            params["restrictToMyDrive"] = restrict_to_my_drive
        response = await self._request(
            "GET",
            "/changes",
            params=params,
        )
        return response.json()

    async def get_file(self, file_id: str, *, fields: str) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"/files/{quote(file_id, safe='')}",
            params={"fields": fields, "supportsAllDrives": True},
        )
        return response.json()

    async def list_files_page(
        self,
        *,
        fields: str,
        page_token: Optional[str] = None,
        drive_id: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "spaces": "drive",
            "pageSize": 1000,
            "fields": f"nextPageToken,incompleteSearch,files({fields})",
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if drive_id:
            params["corpora"] = "drive"
            params["driveId"] = drive_id
        else:
            params["corpora"] = "user"
        if page_token:
            params["pageToken"] = page_token
        response = await self._request("GET", "/files", params=params)
        return response.json()

    async def list_drives_page(self, *, page_token: Optional[str] = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pageSize": 100,
            "fields": "nextPageToken,drives(id,name)",
        }
        if page_token:
            params["pageToken"] = page_token
        response = await self._request("GET", "/drives", params=params)
        return response.json()

    async def export_file(self, file_id: str, *, mime_type: str) -> bytes:
        response = await self._request(
            "GET",
            f"/files/{quote(file_id, safe='')}/export",
            params={"mimeType": mime_type},
        )
        return response.content

    async def download_file(self, file_id: str) -> bytes:
        response = await self._request(
            "GET",
            f"/files/{quote(file_id, safe='')}",
            params={"alt": "media"},
        )
        return response.content
