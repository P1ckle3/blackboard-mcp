# blackboard-mcp

MCP server for **UQ Blackboard Learn** (`learn.uq.edu.au`) with Okta SSO.

Lists your courses, due dates, grades, announcements and content — and bulk-downloads
whole courses to disk. Works in Claude Desktop, Cowork, and Claude Code.

---

## Tools

| Tool | What it does |
|---|---|
| `list_courses` | Enrolled courses, annotated with term + `is_current`, current first |
| `get_me` | Signed-in user profile |
| `list_due_dates` | Upcoming assessment due dates, soonest first, with per-item status |
| `assignment_status` | Every assessment grouped by state, with points earned vs possible |
| `list_content` | One level of folders/files/links in a course or folder |
| `get_content` | Full detail of one item, including body text + attachments |
| `search_content` | Substring search over titles/bodies, one course or all |
| `list_announcements` | Course announcements |
| `get_grades` | Grade columns and your scores |
| `download_file` | One attachment — base64 under 8 MB, or saved to disk |
| `download_content` | Recursively download an item and everything beneath it |
| `download_course` | Download an entire course; incremental re-runs sync only what changed |
| `list_files` | Dry-run tree walk — see what's downloadable without downloading |
| `set_download_root` | Read or change where downloads are saved (persists across restarts) |

---

## Security model

| Concern | Handling |
|---|---|
| Password | OS keyring only (Windows Credential Manager / macOS Keychain / Secret Service). Never written to disk. |
| Username | `~/.blackboard_mcp/credentials.json` |
| Session cookies | `~/.blackboard_mcp/session.json`, domain-scoped to `learn.uq.edu.au` so they can't leak to another host |
| 2FA | Always yours to approve — the Okta push goes to your phone every login |
| File writes | Confined to the download root; traversal, Windows device names and bidi-spoofed filenames are rejected |
| Moving the root | `set_download_root` is agent-callable, so it only accepts folders **inside your home directory**, and refuses ones holding credentials or startup items (`.ssh`, `.claude`, `AppData`, `Library`, …). Only a human editing `BB_DOWNLOAD_ROOT` can point it outside home. |
| Downloads | Streamed, capped at 256 MB per file |

Nothing in this repo contains credentials. All secret state lives outside the repo in
`~/.blackboard_mcp/` and the OS keyring.

---

## Install

Requires **Python 3.10+**.

```bash
git clone https://github.com/P1ckle3/blackboard-mcp.git
cd blackboard-mcp

pip install -e .
playwright install chromium        # login runs in a real browser
```

This puts a `blackboard-mcp` command on your PATH.

### 1. Save your credentials (optional, recommended)

Lets the login browser auto-fill so you only tap the Okta push.

```bash
python scripts/setup_credentials.py
```

### 2. Log in once

```bash
python -m blackboard_mcp --login
```

A browser opens, credentials auto-fill, you approve the push on your phone. The session
is cached. Re-run this whenever tools start reporting `Not authenticated` — tool calls
fail fast with that hint rather than hanging your client while a browser waits.

### 3. Verify

```bash
python scripts/test_connection.py
```

Should print your name and your enrolled courses.

---

## Connect it to a client

### Claude Desktop

Edit `claude_desktop_config.json`:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "blackboard-uq": {
      "command": "blackboard-mcp",
      "args": [],
      "env": {
        "BB_DOWNLOAD_ROOT": "C:\\Users\\YOUR_NAME\\Downloads\\blackboard"
      }
    }
  }
}
```

`BB_DOWNLOAD_ROOT` is optional — see [Download location](#download-location) below.

Restart Claude Desktop. `blackboard-uq` appears under the tools icon.

> If `blackboard-mcp` isn't found (PATH not picked up by the GUI app), point at Python
> directly instead:
> ```json
> "command": "C:\\Path\\To\\python.exe",
> "args": ["-m", "blackboard_mcp"]
> ```

A ready-to-copy version lives at [`config/claude_desktop_example.json`](config/claude_desktop_example.json).

### Cowork

Cowork ships inside the Claude desktop app and reads the **same**
`claude_desktop_config.json`. Follow the Claude Desktop steps above — once the server is
configured there, it's available in Cowork after a restart. No separate config file.

### Claude Code

```bash
claude mcp add blackboard-uq -s user -- blackboard-mcp
```

With a custom download root:

```bash
claude mcp add blackboard-uq -s user \
  -e BB_DOWNLOAD_ROOT="$HOME/Downloads/blackboard" \
  -- blackboard-mcp
```

`-s user` makes it available in every project; drop it to scope the server to the
current project only. Confirm with:

```bash
claude mcp list
```

---

## Download location

Downloads default to `~/Downloads/blackboard`. Three ways to change it, highest
precedence first:

| Method | Scope | Notes |
|---|---|---|
| Ask in chat — *"save my downloads to ~/Documents/uni"* | Persists across restarts | Calls `set_download_root`; folder is created if missing |
| `BB_DOWNLOAD_ROOT` env var in your client config | Per client | The only way to use a folder **outside** your home directory |
| Nothing | — | `~/Downloads/blackboard` |

Ask *"where do my Blackboard downloads go?"* to see the active folder and which of the
three set it. The chosen folder is stored in `~/.blackboard_mcp/config.json` — delete
that file to fall back to the env var or the default.

Every `save_dir` / `save_path` you pass to a download tool is resolved **under** the
root, and anything escaping it is rejected. That confinement is why `set_download_root`
won't accept a folder outside your home directory: an agent that could relocate the root
to `C:\Windows` would make the check meaningless.

---

## Development

Run from source without installing — point `PYTHONPATH` at `src/`:

```json
{
  "mcpServers": {
    "blackboard-uq": {
      "command": "python",
      "args": ["-m", "blackboard_mcp"],
      "env": { "PYTHONPATH": "/path/to/BlackBoard Sync/src" }
    }
  }
}
```

Tests are pure logic — no network, no browser:

```bash
python tests/test_hardening.py
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Not authenticated` | `python -m blackboard_mcp --login` |
| Login browser never opens | `playwright install chromium` |
| Push notification never arrives | Approve in the browser window manually; it stays open 5 minutes |
| `save path must stay under ...` | The target is outside the download root — see [Download location](#download-location) |
| `download root must be a folder inside ...` | `set_download_root` only accepts folders under your home dir; use `BB_DOWNLOAD_ROOT` for anywhere else |
| Server missing in the client | Restart the app; check the command resolves with `which blackboard-mcp` |

---

Unofficial and not affiliated with UQ or Anthology. Uses your own account via the
public Blackboard REST API — respect UQ's acceptable use policy and your course
material's copyright.
