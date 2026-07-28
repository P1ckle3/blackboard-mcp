"""Blackboard REST API client for learn.uq.edu.au."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Optional

import httpx

from .auth import clear_session, get_authenticated_cookies

logger = logging.getLogger(__name__)

BB_HOST = "https://learn.uq.edu.au"
BB_HOSTNAME = "learn.uq.edu.au"
BASE_URL = f"{BB_HOST}/learn/api/public/v1"
PAGE_SIZE = 100          # Blackboard clamps `limit` to 100 on all collections
MAX_PAGES = 500          # backstop: a server ignoring `offset` would loop forever
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
CHUNK = 1 << 20


class BlackboardAPIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


def bb_cookie_jar(cookies: dict[str, str]) -> httpx.Cookies:
    """Domain-scope cookies to learn.uq.edu.au.

    Passing a plain dict to httpx yields cookies with domain='' — which
    http.cookiejar then matches against EVERY host, so any off-host request or
    cross-host redirect leaks the live Blackboard session token.
    """
    jar = httpx.Cookies()
    for k, v in (cookies or {}).items():
        jar.set(k, v, domain=BB_HOSTNAME, path="/")
    return jar


def assert_bb_url(url: str) -> None:
    """Reject any URL that isn't exactly on the Blackboard host."""
    try:
        host = (httpx.URL(url).host or "").lower()
    except Exception as e:
        raise BlackboardAPIError(0, f"malformed URL {url!r}: {e}") from e
    if host != BB_HOSTNAME:
        raise BlackboardAPIError(0, f"refusing to fetch off-host URL: {url!r}")


async def _fetch_xsrf(cookies: dict) -> str:
    """Extract the real XSRF UUID from the Blackboard Ultra page."""
    try:
        async with httpx.AsyncClient(
            cookies=bb_cookie_jar(cookies), follow_redirects=True, timeout=10
        ) as c:
            r = await c.get(f"{BB_HOST}/ultra/institution-page")
            m = re.search(r'"xsrf"\s*:\s*"([a-f0-9\-]{30,})"', r.text, re.I)
            if not m:
                m = re.search(r'xsrf["\s:]+([a-f0-9\-]{30,})', r.text, re.I)
            if not m:
                logger.warning("XSRF token not found in Ultra page; sending empty token.")
                return ""
            return m.group(1)
    except Exception as e:
        logger.warning("XSRF fetch failed (%s); sending empty token.", e)
        return ""


class BlackboardClient:
    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._cookies: Optional[dict[str, str]] = None
        self._lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx.AsyncClient:
        # MCP dispatches tool calls concurrently; without this lock parallel
        # calls each build their own client (and each opened a browser).
        async with self._lock:
            if self._client is None or self._cookies is None:
                self._cookies = await get_authenticated_cookies()
                xsrf = await _fetch_xsrf(self._cookies)
                self._client = httpx.AsyncClient(
                    base_url=BASE_URL,
                    cookies=bb_cookie_jar(self._cookies),
                    follow_redirects=True,
                    timeout=30.0,
                    limits=httpx.Limits(max_connections=10, max_keepalive_connections=10),
                    headers={
                        "Accept": "application/json",
                        "X-Requested-With": "XMLHttpRequest",
                        "X-Blackboard-XSRF": xsrf,
                        "Origin": BB_HOST,
                    },
                )
            return self._client

    async def _reset_client(self) -> None:
        """Drop the current client, closing its pool rather than leaking it."""
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
            self._client = None
            self._cookies = None

    async def _get(self, path: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        reauthed = False

        for attempt in range(4):
            client = await self._ensure_client()
            try:
                resp = await client.get(path, params=params)
            except httpx.RequestError as e:
                if attempt == 3:
                    raise BlackboardAPIError(0, str(e)) from e
                await asyncio.sleep(2 ** attempt)
                continue

            # 403 too: an expired XSRF token fails as 403, and the only way to
            # refresh it is to rebuild the client.
            if resp.status_code in (401, 403) and not reauthed:
                reauthed = True
                clear_session()
                await self._reset_client()
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == 3:
                    raise BlackboardAPIError(resp.status_code, resp.text[:200])
                retry_after = resp.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else 2 ** attempt
                await asyncio.sleep(delay)
                continue

            if not resp.is_success:
                raise BlackboardAPIError(resp.status_code, resp.text[:200])

            return resp.json()

        raise BlackboardAPIError(0, f"gave up after retries: {path}")

    async def _get_all_pages(self, path: str, **params: Any) -> list[dict]:
        """Fetch all pages of a paginated Blackboard endpoint."""
        results: list[dict] = []
        offset = 0
        for _ in range(MAX_PAGES):
            data = await self._get(path, limit=PAGE_SIZE, offset=offset, **params)
            items = data.get("results", []) if isinstance(data, dict) else data
            results.extend(items)
            if len(items) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        else:
            logger.warning("Stopped paginating %s at %d pages.", path, MAX_PAGES)
        return results

    async def get_me(self) -> dict:
        return await self._get("/users/me")

    async def get_courses(self) -> list[dict]:
        """Return course memberships with embedded course details via expand=course."""
        return await self._get_all_pages(
            "/users/me/courses",
            expand="course",
        )

    async def get_terms(self) -> list[dict]:
        """All institution terms (id, name, availability.duration date range)."""
        return await self._get_all_pages("/terms")

    async def get_contents(self, course_id: str, parent_id: Optional[str] = None) -> list[dict]:
        if parent_id:
            return await self._get_all_pages(f"/courses/{course_id}/contents/{parent_id}/children")
        return await self._get_all_pages(f"/courses/{course_id}/contents")

    async def get_content_item(self, course_id: str, content_id: str) -> dict:
        return await self._get(f"/courses/{course_id}/contents/{content_id}")

    async def get_attachments(self, course_id: str, content_id: str) -> list[dict]:
        data = await self._get(f"/courses/{course_id}/contents/{content_id}/attachments")
        return data.get("results", [])

    async def get_announcements(self, course_id: str) -> list[dict]:
        return await self._get_all_pages(f"/courses/{course_id}/announcements")

    async def get_grade_columns(self, course_id: str) -> list[dict]:
        return await self._get_all_pages(f"/courses/{course_id}/gradebook/columns")

    async def get_my_grade(self, course_id: str, column_id: str) -> dict:
        # .../attempts/me does not exist (404s on every column); the per-user
        # grade record lives at .../users/me and is a flat object, not a list.
        return await self._get(
            f"/courses/{course_id}/gradebook/columns/{column_id}/users/me"
        )

    async def download_file(self, url: str, dest_path: Optional[Path] = None) -> bytes | Path:
        """Stream a file. Writes to dest_path if given, else returns bytes.

        Streaming keeps a 300MB lecture recording off the heap, and the byte
        cap bounds both memory and disk regardless of what the server sends.
        """
        assert_bb_url(url)
        client = await self._ensure_client()
        total = 0

        async with client.stream("GET", url) as resp:
            if not resp.is_success:
                await resp.aread()
                raise BlackboardAPIError(resp.status_code, "File download failed")

            if dest_path is not None:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with open(dest_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(CHUNK):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            f.close()
                            dest_path.unlink(missing_ok=True)
                            raise BlackboardAPIError(
                                0, f"file exceeds {MAX_DOWNLOAD_BYTES} byte cap")
                        f.write(chunk)
                return dest_path

            buf = bytearray()
            async for chunk in resp.aiter_bytes(CHUNK):
                buf += chunk
                if len(buf) > MAX_DOWNLOAD_BYTES:
                    raise BlackboardAPIError(
                        0, f"file exceeds {MAX_DOWNLOAD_BYTES} byte cap")
            return bytes(buf)

    async def aclose(self) -> None:
        async with self._lock:
            if self._client:
                await self._client.aclose()
                self._client = None
