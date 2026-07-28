"""Tools: list_content, get_content, search_content."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from bs4 import BeautifulSoup

from ..api import BlackboardClient

SEARCH_CONCURRENCY = 6
SEARCH_MAX_DEPTH = 3


def _strip_html(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)


def _format_item(item: dict) -> dict[str, Any]:
    body = item.get("body") or item.get("description") or ""
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        # (x or {}) not .get(x, {}): the default only applies when the key is
        # ABSENT, so an explicit "contentHandler": null crashed every listing.
        "content_handler": (item.get("contentHandler") or {}).get("id"),
        "availability": (item.get("availability") or {}).get("available"),
        "body_text": _strip_html(body),
        "has_children": item.get("hasChildren", False),
    }


def course_ids_from_memberships(memberships: list[dict]) -> list[str]:
    """Pull course ids out of /users/me/courses membership objects.

    The id lives at membership["course"]["id"] (expand=course) or
    membership["courseId"] — never at membership["id"], which is the
    membership's own primary key.
    """
    ids = []
    for m in memberships:
        cid = (m.get("course") or {}).get("id") or m.get("courseId")
        if cid:
            ids.append(cid)
    return ids


async def list_content(
    client: BlackboardClient,
    course_id: str,
    parent_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    raw = await client.get_contents(course_id, parent_id)
    return [_format_item(i) for i in raw]


async def get_content(
    client: BlackboardClient,
    course_id: str,
    content_id: str,
) -> dict[str, Any]:
    item = await client.get_content_item(course_id, content_id)
    result = _format_item(item)
    # Include attachments list
    try:
        attachments = await client.get_attachments(course_id, content_id)
        result["attachments"] = [
            {
                "id": a.get("id"),
                "filename": a.get("fileName"),
                "mime_type": a.get("mimeType"),
                "size": a.get("size"),
                "download_url": a.get("downloadUrl"),
            }
            for a in attachments
        ]
    except Exception:
        result["attachments"] = []
    return result


async def _search_course(
    client: BlackboardClient,
    course_id: str,
    pattern: re.Pattern,
    max_depth: int,
    sem: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    """Recursively scan one course's content tree for matches."""
    matches: list[dict[str, Any]] = []

    async def walk(parent_id: Optional[str], depth: int) -> None:
        if depth > max_depth:
            return
        try:
            async with sem:
                items = await client.get_contents(course_id, parent_id)
        except Exception:
            return

        children: list[str] = []
        for item in items:
            title = item.get("title") or ""
            body = _strip_html(item.get("body") or item.get("description") or "")
            if pattern.search(title) or pattern.search(body):
                formatted = _format_item(item)
                formatted["course_id"] = course_id
                matches.append(formatted)
            if item.get("hasChildren") and item.get("id"):
                children.append(item["id"])

        await asyncio.gather(*(walk(cid, depth + 1) for cid in children))

    await walk(None, 0)
    return matches


async def search_content(
    client: BlackboardClient,
    query: str,
    course_id: Optional[str] = None,
    max_depth: int = SEARCH_MAX_DEPTH,
) -> list[dict[str, Any]]:
    """Search content items matching query across one or all courses.

    Recurses into folders — nearly all real course material lives nested
    inside week folders and lessons, so a top-level-only scan returned nothing
    and reported it as "no matches".
    """
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    sem = asyncio.Semaphore(SEARCH_CONCURRENCY)

    if course_id:
        course_ids = [course_id]
    else:
        course_ids = course_ids_from_memberships(await client.get_courses())

    per_course = await asyncio.gather(*(
        _search_course(client, cid, pattern, max_depth, sem) for cid in course_ids
    ))
    return [m for course_matches in per_course for m in course_matches]
