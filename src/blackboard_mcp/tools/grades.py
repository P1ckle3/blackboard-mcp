"""Tool: get_grades — fetch grade columns and my scores for a course."""

from __future__ import annotations

import asyncio
from typing import Any

from ..api import BlackboardClient

GRADES_CONCURRENCY = 6


def _merge_grade(entry: dict[str, Any], record: dict[str, Any]) -> None:
    """Fold a gradebook user-record into a column entry.

    The record is flat, e.g.
      {"userId": ..., "columnId": ..., "status": "Graded", "score": 8.0,
       "text": "Thursday 15:00", "overridden": "2026-03-06T...", "exempt": false}
    A column with nothing posted yet returns only userId/columnId.
    """
    entry["score"] = record.get("score")
    entry["grade"] = record.get("text")          # display grade, when scaled
    entry["status"] = record.get("status")       # "Graded", "NeedsGrading", ...
    entry["exempt"] = record.get("exempt")
    entry["feedback"] = record.get("feedback")
    if record.get("overridden"):
        entry["overridden"] = record["overridden"]


async def get_grades(
    client: BlackboardClient,
    course_id: str,
) -> list[dict[str, Any]]:
    columns = await client.get_grade_columns(course_id)
    sem = asyncio.Semaphore(GRADES_CONCURRENCY)

    async def _one(col: dict) -> dict[str, Any]:
        col_id = col.get("id")
        entry: dict[str, Any] = {
            "id": col_id,
            "name": col.get("name"),
            "display_name": col.get("displayName"),
            # (x or {}): an explicit "score": null crashed the whole course.
            "points_possible": (col.get("score") or {}).get("possible"),
            "score": None,
            "grade": None,
            "status": None,
            "feedback": None,
        }
        if not col_id:
            return entry
        try:
            async with sem:
                record = await client.get_my_grade(course_id, col_id)
            _merge_grade(entry, record)
        except Exception as e:
            # Surfaced, not swallowed: a 404/401 used to be indistinguishable
            # from "no grade posted yet".
            entry["error"] = str(e)
        return entry

    return list(await asyncio.gather(*(_one(c) for c in columns)))
