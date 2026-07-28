"""Tool: list_announcements — fetch course announcements."""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from ..api import BlackboardClient


def _strip_html(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)


async def list_announcements(
    client: BlackboardClient,
    course_id: str,
) -> list[dict[str, Any]]:
    raw = await client.get_announcements(course_id)
    results = []
    for a in raw:
        results.append({
            "id": a.get("id"),
            "title": a.get("title"),
            "body": _strip_html(a.get("body", "")),
            "created": a.get("created"),
            "modified": a.get("modified"),
            "show_reorder_buttons": a.get("showReorderButtons"),
        })
    return results
