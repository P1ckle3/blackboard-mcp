"""Blackboard MCP server — entry point."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import paths
from .api import BlackboardClient
from .tools.announcements import list_announcements
from .tools.content import get_content, list_content, search_content
from .tools.courses import get_me, list_courses
from .tools.deadlines import assignment_status, list_due_dates
from .tools.files import download_file
from .tools.grades import get_grades
from .tools.download_smart import download_content, download_course, list_files

# stderr, explicitly — stdout carries the JSON-RPC stream.
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

app = Server("blackboard-uq")
_client: BlackboardClient | None = None


def _get_client() -> BlackboardClient:
    global _client
    if _client is None:
        _client = BlackboardClient()
    return _client


def _json(obj: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(obj, indent=2, ensure_ascii=False))]


def _schema(properties: dict, required: list[str]) -> dict:
    """Object schema that rejects unknown keys, so a typo'd argument name is an
    error rather than a silently ignored field."""
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# minLength stops "" passing `required` validation and becoming a malformed URL
_COURSE_ID = {"type": "string", "minLength": 1, "description": "Blackboard course ID"}
_CONTENT_ID = {"type": "string", "minLength": 1, "description": "Content item ID"}
_DEPTH = {"type": "integer", "minimum": 0, "maximum": 12,
          "description": "Max folder recursion depth (default 6)", "default": 6}
_FORCE = {"type": "boolean", "default": False,
          "description": "Re-download files that already exist with a matching size"}


@app.list_tools()
async def handle_list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_courses",
            description=(
                "List enrolled UQ Blackboard courses, annotated with term name and "
                "is_current. Current-term courses are listed first. Pass "
                "current_only=true to hide past enrolments."
            ),
            inputSchema=_schema({
                "current_only": {"type": "boolean", "default": False,
                                 "description": "Only courses whose term includes today"},
            }, []),
        ),
        Tool(
            name="get_me",
            description="Get the signed-in user's Blackboard profile (name, username, student ID).",
            inputSchema=_schema({}, []),
        ),
        Tool(
            name="list_due_dates",
            description=(
                "Upcoming assessment due dates, soonest first, with my current status "
                "for each (upcoming / overdue / submitted / graded). Defaults to "
                "current-term courses. Omit course_id for all courses."
            ),
            inputSchema=_schema({
                "course_id": {"type": "string", "minLength": 1,
                              "description": "Limit to one course (optional)"},
                "days_ahead": {"type": "integer", "minimum": 0, "maximum": 365,
                               "description": "Only items due within N days (optional)"},
                "include_past": {"type": "boolean", "default": False,
                                 "description": "Include already-passed due dates"},
                "current_only": {"type": "boolean", "default": True,
                                 "description": "Restrict to current-term courses"},
            }, []),
        ),
        Tool(
            name="assignment_status",
            description=(
                "Every assessment grouped by state (graded, submitted, overdue, "
                "upcoming, exempt, no_due_date), with counts and total points earned "
                "vs possible. Omit course_id for all current courses."
            ),
            inputSchema=_schema({
                "course_id": {"type": "string", "minLength": 1,
                              "description": "Limit to one course (optional)"},
                "current_only": {"type": "boolean", "default": True,
                                 "description": "Restrict to current-term courses"},
            }, []),
        ),
        Tool(
            name="list_content",
            description=(
                "List content items (folders, files, links) inside a course or folder. "
                "Pass parent_id to drill into a subfolder. Returns one level only."
            ),
            inputSchema=_schema({
                "course_id": _COURSE_ID,
                "parent_id": {"type": "string", "minLength": 1,
                              "description": "Optional parent folder content ID"},
            }, ["course_id"]),
        ),
        Tool(
            name="get_content",
            description=(
                "Get full details of a single content item including body text and attachments list."
            ),
            inputSchema=_schema({
                "course_id": _COURSE_ID,
                "content_id": _CONTENT_ID,
            }, ["course_id", "content_id"]),
        ),
        Tool(
            name="download_file",
            description=(
                "Download a file attachment from a content item. Returns base64 content "
                "for files under 8MB, or saves to disk if save_path is given. Larger "
                "files must use save_path. Saved files are confined to the download "
                "root (BB_DOWNLOAD_ROOT, default ~/Downloads/blackboard)."
            ),
            inputSchema=_schema({
                "course_id": _COURSE_ID,
                "content_id": _CONTENT_ID,
                "attachment_id": {"type": "string", "minLength": 1,
                                  "description": "Specific attachment ID (optional, defaults to first)"},
                "save_path": {"type": "string", "minLength": 1,
                              "description": "Path to save file, under the download root (optional)"},
            }, ["course_id", "content_id"]),
        ),
        Tool(
            name="search_content",
            description=(
                "Search content items whose title or body matches a query string, in one "
                "course or all enrolled courses. Recurses into folders up to 3 levels deep."
            ),
            inputSchema=_schema({
                "query": {"type": "string", "minLength": 1,
                          "description": "Case-insensitive literal substring to match"},
                "course_id": {"type": "string", "minLength": 1,
                              "description": "Limit search to this course (optional)"},
                "max_depth": {"type": "integer", "minimum": 0, "maximum": 8,
                              "description": "Folder recursion depth (default 3)", "default": 3},
            }, ["query"]),
        ),
        Tool(
            name="list_announcements",
            description="List announcements for a course.",
            inputSchema=_schema({"course_id": _COURSE_ID}, ["course_id"]),
        ),
        Tool(
            name="get_grades",
            description="Get grade columns and my scores for a course.",
            inputSchema=_schema({"course_id": _COURSE_ID}, ["course_id"]),
        ),
        Tool(
            name="download_content",
            description=(
                "Recursively download a Blackboard content item and all its children "
                "to a local directory. Handles all content types: files/documents/"
                "assignments (downloads attached files), folders/lessons (recurses), "
                "external links and LTI tools (saves .url shortcuts), and content "
                "body HTML (saves HTML + downloads embedded links). Writes are confined "
                "to the download root (BB_DOWNLOAD_ROOT, default ~/Downloads/blackboard)."
            ),
            inputSchema=_schema({
                "course_id": _COURSE_ID,
                "content_id": _CONTENT_ID,
                "save_dir": {"type": "string", "minLength": 1,
                             "description": "Directory to save into, under the download root"},
                "max_depth": _DEPTH,
                "force": _FORCE,
            }, ["course_id", "content_id", "save_dir"]),
        ),
        Tool(
            name="download_course",
            description=(
                "Download an ENTIRE course — every top-level item and everything "
                "beneath it — into save_dir. Incremental: files already on disk with "
                "a matching size are skipped, so re-running syncs only what changed. "
                "Writes are confined to the download root (BB_DOWNLOAD_ROOT)."
            ),
            inputSchema=_schema({
                "course_id": _COURSE_ID,
                "save_dir": {"type": "string", "minLength": 1,
                             "description": "Directory to save into, under the download root"},
                "max_depth": _DEPTH,
                "force": _FORCE,
            }, ["course_id", "save_dir"]),
        ),
        Tool(
            name="list_files",
            description=(
                "Dry-run tree walk — lists all downloadable files in a course or folder "
                "without downloading anything. Useful for discovering what's available "
                "before committing to a full download. Pass content_id to scope to a "
                "specific folder, or omit to scan the whole course."
            ),
            inputSchema=_schema({
                "course_id": _COURSE_ID,
                "content_id": {"type": "string", "minLength": 1,
                               "description": "Scope to this folder (optional, omit for whole course)"},
                "max_depth": _DEPTH,
            }, ["course_id"]),
        ),
        Tool(
            name="set_download_root",
            description=(
                "Choose where downloads are saved. Call with no arguments to report the "
                "current download folder and where that setting came from. Pass a path "
                "to move it — the folder is created if missing and the choice persists "
                "across restarts. Must be inside your home directory; folders holding "
                "credentials or startup items are refused."
            ),
            inputSchema=_schema({
                "path": {"type": "string", "minLength": 1,
                         "description": "New download folder, e.g. ~/Documents/uni "
                                        "(optional — omit to just read the current one)"},
            }, []),
        ),
    ]


@app.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    client = _get_client()
    try:
        if name == "list_courses":
            return _json(await list_courses(
                client,
                current_only=arguments.get("current_only", False),
            ))

        elif name == "get_me":
            return _json(await get_me(client))

        elif name == "list_due_dates":
            return _json(await list_due_dates(
                client,
                course_id=arguments.get("course_id"),
                days_ahead=arguments.get("days_ahead"),
                include_past=arguments.get("include_past", False),
                current_only=arguments.get("current_only", True),
            ))

        elif name == "assignment_status":
            return _json(await assignment_status(
                client,
                course_id=arguments.get("course_id"),
                current_only=arguments.get("current_only", True),
            ))

        elif name == "list_content":
            return _json(await list_content(
                client,
                course_id=arguments["course_id"],
                parent_id=arguments.get("parent_id"),
            ))

        elif name == "get_content":
            return _json(await get_content(
                client,
                course_id=arguments["course_id"],
                content_id=arguments["content_id"],
            ))

        elif name == "download_file":
            return _json(await download_file(
                client,
                course_id=arguments["course_id"],
                content_id=arguments["content_id"],
                attachment_id=arguments.get("attachment_id"),
                save_path=arguments.get("save_path"),
            ))

        elif name == "search_content":
            return _json(await search_content(
                client,
                query=arguments["query"],
                course_id=arguments.get("course_id"),
                max_depth=arguments.get("max_depth", 3),
            ))

        elif name == "list_announcements":
            return _json(await list_announcements(
                client,
                course_id=arguments["course_id"],
            ))

        elif name == "get_grades":
            return _json(await get_grades(
                client,
                course_id=arguments["course_id"],
            ))

        elif name == "download_content":
            return _json(await download_content(
                client,
                course_id=arguments["course_id"],
                content_id=arguments["content_id"],
                save_dir=arguments["save_dir"],
                max_depth=arguments.get("max_depth", 6),
                force=arguments.get("force", False),
            ))

        elif name == "download_course":
            return _json(await download_course(
                client,
                course_id=arguments["course_id"],
                save_dir=arguments["save_dir"],
                max_depth=arguments.get("max_depth", 6),
                force=arguments.get("force", False),
            ))

        elif name == "list_files":
            return _json(await list_files(
                client,
                course_id=arguments["course_id"],
                content_id=arguments.get("content_id"),
                max_depth=arguments.get("max_depth", 6),
            ))

        elif name == "set_download_root":
            if arguments.get("path"):
                paths.set_download_root(arguments["path"])
            return _json({
                "download_root": str(paths.DOWNLOAD_ROOT),
                "source": paths.root_source(),
            })

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception:
        # Re-raise: the MCP SDK turns a handler exception into
        # CallToolResult(isError=True). Returning the error as a normal result
        # made every failure look like a success to the calling agent.
        logger.exception("Tool %s failed", name)
        raise


async def _run() -> None:
    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())
    finally:
        if _client is not None:
            await _client.aclose()


def main() -> None:
    if "--login" in sys.argv:
        from .auth import login
        raise SystemExit(0 if asyncio.run(login()) else 1)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
