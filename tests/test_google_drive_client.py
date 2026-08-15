from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.utils.google_drive_client import AsyncGoogleDriveClient, DriveApiError


def credentials_loader(force_refresh: bool):
    credentials = MagicMock()
    credentials.token = "fresh-token" if force_refresh else "token"
    credentials.expired = False
    return credentials


@pytest.mark.asyncio
async def test_client_parses_export_size_limit_reason():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            request=request,
            json={
                "error": {
                    "code": 403,
                    "message": "This file is too large to be exported.",
                    "errors": [{"reason": "exportSizeLimitExceeded"}],
                }
            },
        )

    client = AsyncGoogleDriveClient(
        credentials_loader,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    async with client:
        with pytest.raises(DriveApiError) as raised:
            await client.export_file("f1", mime_type="text/markdown")

    assert raised.value.status == 403
    assert raised.value.reasons == frozenset({"exportSizeLimitExceeded"})


@pytest.mark.asyncio
async def test_client_retries_rate_limit_asynchronously(monkeypatch):
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(
                429,
                request=request,
                json={"error": {"message": "rate limited", "errors": [{"reason": "rateLimitExceeded"}]}},
            )
        return httpx.Response(200, request=request, json={"startPageToken": "start"})

    sleep = AsyncMock()
    monkeypatch.setattr("src.utils.google_drive_client.asyncio.sleep", sleep)
    client = AsyncGoogleDriveClient(
        credentials_loader,
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )
    async with client:
        assert await client.get_start_page_token() == "start"

    assert requests == 2
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_client_retries_transport_errors_asynchronously(monkeypatch):
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, request=request, json={"startPageToken": "start"})

    sleep = AsyncMock()
    monkeypatch.setattr("src.utils.google_drive_client.asyncio.sleep", sleep)
    client = AsyncGoogleDriveClient(
        credentials_loader,
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )
    async with client:
        assert await client.get_start_page_token() == "start"

    assert requests == 2
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_client_refreshes_401_even_with_zero_retry_budget():
    loads: list[bool] = []
    requests = 0

    def load_credentials(force_refresh: bool):
        loads.append(force_refresh)
        credentials = MagicMock()
        credentials.token = "fresh-token" if force_refresh else "stale-token"
        credentials.expired = False
        return credentials

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if request.headers["Authorization"] == "Bearer stale-token":
            return httpx.Response(
                401,
                request=request,
                json={"error": {"message": "invalid credentials"}},
            )
        return httpx.Response(200, request=request, json={"startPageToken": "start"})

    client = AsyncGoogleDriveClient(
        load_credentials,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    async with client:
        assert await client.get_start_page_token() == "start"

    assert requests == 2
    assert loads == [False, True]


@pytest.mark.asyncio
async def test_client_sends_shared_drive_parameters_and_endpoint_shapes():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/changes/startPageToken"):
            return httpx.Response(200, request=request, json={"startPageToken": "start"})
        if request.url.path.endswith("/changes"):
            return httpx.Response(200, request=request, json={"changes": []})
        if request.url.path.endswith("/drives"):
            return httpx.Response(200, request=request, json={"drives": []})
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, request=request, content=b"content")
        return httpx.Response(200, request=request, json={"files": []})

    client = AsyncGoogleDriveClient(
        credentials_loader,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    async with client:
        assert await client.get_start_page_token(drive_id="drive/one") == "start"
        assert await client.list_changes(
            "page",
            include_corpus_removals=True,
            restrict_to_my_drive=False,
            drive_id="drive/one",
        ) == {"changes": []}
        assert await client.list_files_page(fields="id,name", drive_id="drive/one") == {
            "files": []
        }
        assert await client.list_drives_page() == {"drives": []}
        assert await client.download_file("file/one") == b"content"

    start, changes, files, drives, download = seen
    assert start.url.params["supportsAllDrives"] == "true"
    assert start.url.params["driveId"] == "drive/one"
    assert changes.url.params["driveId"] == "drive/one"
    assert "restrictToMyDrive" not in changes.url.params
    assert files.url.params["corpora"] == "drive"
    assert files.url.params["driveId"] == "drive/one"
    assert drives.url.params["fields"] == "nextPageToken,drives(id,name)"
    assert download.url.raw_path.split(b"?", 1)[0].endswith(b"/files/file%2Fone")


@pytest.mark.asyncio
async def test_client_sends_user_log_and_pagination_parameters():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/changes"):
            return httpx.Response(200, request=request, json={"changes": []})
        if request.url.path.endswith("/drives"):
            return httpx.Response(200, request=request, json={"drives": []})
        if request.url.path.endswith("/export"):
            return httpx.Response(200, request=request, content=b"markdown")
        if request.url.params.get("fields") == "id,name":
            return httpx.Response(200, request=request, json={"id": "file/one"})
        return httpx.Response(200, request=request, json={"files": []})

    client = AsyncGoogleDriveClient(
        credentials_loader,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    async with client:
        assert await client.list_changes(
            "page",
            include_corpus_removals=False,
            restrict_to_my_drive=True,
        ) == {"changes": []}
        assert await client.get_file("file/one", fields="id,name") == {"id": "file/one"}
        assert await client.list_files_page(
            fields="id,name",
            page_token="files-next",
        ) == {"files": []}
        assert await client.list_drives_page(page_token="drives-next") == {"drives": []}
        assert await client.export_file("file/one", mime_type="text/markdown") == b"markdown"

    changes, metadata, files, drives, export = seen
    assert changes.url.params["restrictToMyDrive"] == "true"
    assert "driveId" not in changes.url.params
    assert metadata.url.params["supportsAllDrives"] == "true"
    assert files.url.params["corpora"] == "user"
    assert files.url.params["pageToken"] == "files-next"
    assert drives.url.params["pageToken"] == "drives-next"
    assert export.url.params["mimeType"] == "text/markdown"
