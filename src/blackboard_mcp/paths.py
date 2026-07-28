"""Filesystem safety: filename sanitizing and download-root confinement."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

CONFIG_FILE = Path.home() / ".blackboard_mcp" / "config.json"
_HOME = Path.home().resolve()


def _saved_root() -> str | None:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("download_root")
    except Exception:
        return None


# Every write goes under this root. Precedence: the folder last chosen via
# set_download_root, then the BB_DOWNLOAD_ROOT env var, then the default.
DOWNLOAD_ROOT = Path(
    _saved_root()
    or os.environ.get("BB_DOWNLOAD_ROOT")
    or (Path.home() / "Downloads" / "blackboard")
).expanduser().resolve()


def root_source() -> str:
    if _saved_root():
        return f"chosen via set_download_root, saved in {CONFIG_FILE}"
    if os.environ.get("BB_DOWNLOAD_ROOT"):
        return "BB_DOWNLOAD_ROOT environment variable"
    return "default (~/Downloads/blackboard)"


# Folders under $HOME that hold credentials or run code on login. The root is
# settable by whatever is driving the MCP client, so this list is the difference
# between "pick a download folder" and "write to ~/.ssh/authorized_keys".
_FORBIDDEN = {"appdata", "library", ".ssh", ".aws", ".gnupg", ".config",
              ".claude", ".cursor", ".vscode", ".local", "bin", ".git"}


def set_download_root(path: str | Path) -> Path:
    """Move the download root to another folder inside $HOME, and persist it.

    Confined to $HOME: an agent that can relocate the root anywhere would make
    confine() decorative. A human can still point BB_DOWNLOAD_ROOT outside it.
    """
    global DOWNLOAD_ROOT
    p = Path(path).expanduser().resolve()

    if p == _HOME or not p.is_relative_to(_HOME):
        raise ValueError(
            f"download root must be a folder inside {_HOME} (got {p}). "
            f"To use a location outside it, set the BB_DOWNLOAD_ROOT env var."
        )
    top = p.parts[len(_HOME.parts)]
    if top.lower() in _FORBIDDEN:
        raise ValueError(
            f"refusing {p} as a download root: {top!r} holds credentials or "
            f"startup items. Pick another folder under {_HOME}."
        )

    p.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_ROOT = p
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps({"download_root": str(p)}, indent=2), encoding="utf-8"
    )
    return p


# Windows device names: writing to these silently discards the data — a file
# named NUL reports success and size N, but is 0 bytes and absent from the dir.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(10)),
    *(f"LPT{i}" for i in range(10)),
}

# Path separators, control chars, and bidi overrides (u202e spoofs extensions:
# "notes‮gpj.exe" renders to the eye as "notesexe.jpg").
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f‪-‮⁦-⁩]')


def safe_name(name: str) -> str:
    """Sanitize a server-supplied string for use as one path component."""
    s = _BAD_CHARS.sub("_", name or "untitled").strip(". ") or "untitled"
    if s.split(".")[0].upper() in _RESERVED:
        s = "_" + s
    return s[:200] or "untitled"


def confine(path: str | Path) -> Path:
    """Resolve path under DOWNLOAD_ROOT, raising if it escapes.

    .resolve() first so "..", symlinks and drive-letter switches are all
    normalized before the containment check.
    """
    p = Path(path).expanduser()
    dest = (p if p.is_absolute() else DOWNLOAD_ROOT / p).resolve()
    if dest != DOWNLOAD_ROOT and not dest.is_relative_to(DOWNLOAD_ROOT):
        raise ValueError(
            f"save path must stay under {DOWNLOAD_ROOT} (got {dest}). "
            f"Call set_download_root to download somewhere else."
        )
    return dest


def unique_path(p: Path) -> Path:
    """Append (1), (2), ... if the path already exists."""
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        candidate = p.parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1
