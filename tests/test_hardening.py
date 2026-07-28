"""Regression checks for the code-review hardening pass.

Pure-logic only — no network, no browser. Run:  python tests/test_hardening.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from blackboard_mcp import paths
from blackboard_mcp.api import assert_bb_url, bb_cookie_jar, BlackboardAPIError
from blackboard_mcp.auth import _on_bb
from blackboard_mcp.tools.content import _format_item, course_ids_from_memberships
from blackboard_mcp.tools.courses import list_courses
from blackboard_mcp.tools.deadlines import _status
from blackboard_mcp.tools.download_smart import (
    _assign_names, _extract_body_links, _fix_extension, _up_to_date, _url_shortcut,
)
from blackboard_mcp.tools.grades import _merge_grade


def test_cookies_never_leave_blackboard():
    """A plain dict yields domain='' cookies, which go to EVERY host."""
    jar = bb_cookie_jar({"BbRouter": "SECRET", "JSESSIONID": "SECRET2"})
    c = httpx.Client(cookies=jar)

    foreign = c.build_request("GET", "https://evil.example/steal?r=https://learn.uq.edu.au")
    c.cookies.set_cookie_header(foreign)
    assert foreign.headers.get("cookie") is None, foreign.headers.get("cookie")

    real = c.build_request("GET", "https://learn.uq.edu.au/x")
    c.cookies.set_cookie_header(real)
    assert "BbRouter=SECRET" in real.headers["cookie"]


def test_off_host_urls_rejected():
    for bad in [
        "https://evil.example/steal?x=https://learn.uq.edu.au",   # substring bypass
        "https://learn.uq.edu.au.evil.example/steal",             # suffix bypass
        "https://evil.example/p.png#https://learn.uq.edu.au",     # fragment bypass
        "http://127.0.0.1:8080/admin",
    ]:
        try:
            assert_bb_url(bad)
        except BlackboardAPIError:
            continue
        raise AssertionError(f"accepted off-host URL: {bad}")
    assert_bb_url("https://learn.uq.edu.au/webapps/x")  # must not raise


def test_body_link_extraction():
    html = """
      <a href="https://evil.example/x?r=https://learn.uq.edu.au">bypass1</a>
      <a href="https://learn.uq.edu.au.evil.example/x">bypass2</a>
      <a href="//cdn.example.com/x.png">protocol-relative</a>
      <a href="mailto:a@b.com">mail</a>
      <a href="#anchor">anchor</a>
      <a href="javascript:void(0)">js</a>
      <a href="/bbcswebdav/real.pdf">good-abs</a>
      <img src="images/rel.png">
    """
    hosts = {httpx.URL(u).host for u, _ in _extract_body_links(html)}
    assert hosts == {"learn.uq.edu.au"}, hosts
    names = {n for _, n in _extract_body_links(html)}
    assert "real.pdf" in names and "rel.png" in names, names
    # the old string concat produced host "learn.uq.edu.auimages"
    assert not any("learn.uq.edu.aumailto" in u for u, _ in _extract_body_links(html))


def test_url_shortcut_rejects_ini_injection(tmp: Path):
    evil = "https://x/\nIconFile=\\\\attacker.example\\share\\i.ico\nIconIndex=1"
    try:
        _url_shortcut(evil, "note", tmp)
    except ValueError:
        pass
    else:
        raise AssertionError("wrote a shortcut with an injected IconFile key")
    for bad in ["file:///C:/x", "javascript:alert(1)"]:
        try:
            _url_shortcut(bad, "n", tmp)
        except ValueError:
            continue
        raise AssertionError(f"accepted unsafe scheme: {bad}")
    r = _url_shortcut("https://example.com/ok", "good", tmp)
    assert Path(r["saved"]).read_text(encoding="utf-8").count("\n") == 2


def test_path_confinement(tmp: Path):
    paths.DOWNLOAD_ROOT = tmp.resolve()
    assert paths.confine("sub/file.pdf") == tmp.resolve() / "sub" / "file.pdf"
    for escape in [
        "../../../../Windows/System32/evil.dll",
        str(Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/x.bat"),
        str(Path.home() / ".claude" / "settings.json"),
        "C:/Windows/evil.dll" if sys.platform == "win32" else "/etc/passwd",
    ]:
        try:
            paths.confine(escape)
        except ValueError:
            continue
        raise AssertionError(f"escaped the download root: {escape}")


def test_set_download_root_cannot_escape_home(tmp: Path):
    """The root is agent-settable, so confine() is only as strong as this check."""
    original_cfg = paths.CONFIG_FILE
    paths.CONFIG_FILE = tmp / "config.json"
    try:
        for bad in [
            str(Path.home()),                                  # home itself -> ~/.bashrc
            str(Path.home() / ".ssh"),
            str(Path.home() / ".claude" / "x"),
            str(Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu"),
            "C:/Windows/Temp" if sys.platform == "win32" else "/tmp",
            str(Path.home() / ".." / "Public"),                # traversal out of home
        ]:
            try:
                paths.set_download_root(bad)
            except ValueError:
                continue
            raise AssertionError(f"accepted unsafe download root: {bad}")

        good = Path.home() / "Downloads" / "bbtest_root"
        assert paths.set_download_root(good) == good.resolve()
        assert paths.DOWNLOAD_ROOT == good.resolve()          # confine() follows it
        assert paths.confine("x.pdf") == good.resolve() / "x.pdf"
        assert paths._saved_root() == str(good.resolve())     # persisted
        good.rmdir()
    finally:
        paths.CONFIG_FILE = original_cfg


def test_safe_name_blocks_devices_and_traversal():
    assert paths.safe_name("NUL") == "_NUL"          # else: 0 bytes, silently
    assert paths.safe_name("con.txt") == "_con.txt"
    assert paths.safe_name("COM1") == "_COM1"
    assert paths.safe_name("../../evil.bat") == "_.._evil.bat"
    assert "\u202e" not in paths.safe_name("notes\u202egpj.exe")
    assert paths.safe_name("") == "untitled"
    assert paths.safe_name("....") == "untitled"
    assert len(paths.safe_name("a" * 500)) <= 200


def test_fix_extension_does_not_double_up():
    # Blackboard reports OOXML under legacy MIME ids; appending broke the file
    assert _fix_extension("slides.pptx", "application/vnd.ms-powerpoint") == "slides.pptx"
    assert _fix_extension("essay.docx", "application/msword") == "essay.docx"
    assert _fix_extension("Lecture 1", "application/pdf") == "Lecture 1.pdf"
    assert _fix_extension("raw", "application/octet-stream") == "raw"


def test_on_bb_ignores_query_string():
    # An encoded SSO return-url in the query used to pin this False forever
    assert _on_bb("https://learn.uq.edu.au/ultra/course?returnUrl=https://sso.uq.edu.au/done")
    assert _on_bb("https://learn.uq.edu.au/webapps/bb-SSO-BBLEARN/index.jsp")
    assert not _on_bb("https://sso.uq.edu.au/idp/login")
    assert not _on_bb("https://learn.uq.edu.au/auth-saml/saml/login?x=1")
    assert not _on_bb("https://learn.uq.edu.au.evil.example/ultra")


def test_course_ids_from_memberships():
    # membership["id"] is the membership PK, not the course id
    assert course_ids_from_memberships([
        {"id": "_membership_1", "course": {"id": "_course_9"}},
        {"id": "_membership_2", "courseId": "_course_8"},
        {"id": "_membership_3"},
    ]) == ["_course_9", "_course_8"]


def test_null_values_do_not_crash():
    # .get(k, {}) returns None when the key EXISTS with a null value
    assert _format_item({"contentHandler": None, "availability": None})["content_handler"] is None


def test_grade_record_merge():
    # Real payload shapes observed from .../gradebook/columns/{id}/users/me
    e = {}
    _merge_grade(e, {"userId": "u", "columnId": "c"})          # nothing posted
    assert e["score"] is None and e["status"] is None

    e = {}
    _merge_grade(e, {"status": "Graded", "score": 8.0, "exempt": False})
    assert e["score"] == 8.0 and e["status"] == "Graded"

    e = {}
    _merge_grade(e, {"status": "Graded", "text": "Thursday 15:00",
                     "overridden": "2026-04-23T05:19:33.954Z"})
    assert e["grade"] == "Thursday 15:00"
    assert e["overridden"].startswith("2026-04-23")


def test_incremental_sync_skips_matching_files(tmp: Path):
    f = tmp / "lecture.pdf"
    f.write_bytes(b"x" * 100)
    assert _up_to_date(f, 100, force=False)          # same size -> skip
    assert not _up_to_date(f, 250, force=False)      # changed upstream -> refetch
    assert not _up_to_date(f, 100, force=True)       # force overrides
    assert not _up_to_date(tmp / "missing.pdf", 100, force=False)
    # no size advertised (body-embedded): presence is the signal
    assert _up_to_date(f, None, force=False)
    (tmp / "empty.pdf").write_bytes(b"")
    assert not _up_to_date(tmp / "empty.pdf", None, force=False)


def test_assign_names_is_deterministic():
    # Must not depend on disk state, or a sync re-emits "x (1)", "x (2)" forever
    names = ["notes.pdf", "notes.pdf", "a.txt", "notes.pdf"]
    first = _assign_names(names)
    assert first == ["notes.pdf", "notes (2).pdf", "a.txt", "notes (3).pdf"], first
    assert _assign_names(names) == first


def test_undefined_embedded_filename():
    html = '<a href="/bbcswebdav/x/ultraDocumentBody/undefined">x</a>'
    assert _extract_body_links(html)[0][1] == "embedded"


def test_assessment_status_derivation():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    past, future = now - timedelta(days=2), now + timedelta(days=2)
    cases = [
        ({"status": "Graded", "score": 8.0}, past, "graded"),
        ({"status": "NeedsGrading"}, past, "submitted"),
        ({"exempt": True}, past, "exempt"),
        ({}, past, "overdue"),
        ({}, future, "upcoming"),
        ({}, None, "no_due_date"),
        ({"status": "Graded", "score": None}, future, "upcoming"),
    ]
    for record, due, expected in cases:
        got = _status(record, due, now)
        assert got == expected, f"{record} due={due} -> {got}, expected {expected}"


def test_current_course_filter():
    """Term date range decides is_current, not the name string."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    def iso(d):
        return d.isoformat().replace("+00:00", "Z")

    class Fake:
        async def get_courses(self):
            return [
                {"course": {"id": "_1", "courseId": "OLD", "name": "Old", "termId": "_t_old"}},
                {"course": {"id": "_2", "courseId": "NOW", "name": "Now", "termId": "_t_now"}},
                {"course": {"id": "_3", "courseId": "NONE", "name": "NoTerm"}},
            ]
        async def get_terms(self):
            return [
                {"id": "_t_old", "name": "Semester 1, 2020", "availability": {"duration": {
                    "start": iso(now - timedelta(days=400)), "end": iso(now - timedelta(days=300))}}},
                {"id": "_t_now", "name": "Semester 2, 2026", "availability": {"duration": {
                    "start": iso(now - timedelta(days=10)), "end": iso(now + timedelta(days=80))}}},
            ]

    everything = asyncio.run(list_courses(Fake()))
    assert len(everything) == 3
    assert everything[0]["display_id"] == "NOW", "current course must sort first"
    assert everything[0]["term"] == "Semester 2, 2026"

    current = asyncio.run(list_courses(Fake(), current_only=True))
    assert [c["display_id"] for c in current] == ["NOW"], current


def main() -> int:
    import tempfile
    original_root = paths.DOWNLOAD_ROOT
    failed = 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        with tempfile.TemporaryDirectory() as td:
            try:
                if fn.__code__.co_argcount:
                    fn(Path(td))
                else:
                    fn()
            except Exception as e:
                failed += 1
                print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
            else:
                print(f"ok    {fn.__name__}")
            finally:
                paths.DOWNLOAD_ROOT = original_root
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
