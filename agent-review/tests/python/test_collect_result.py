"""agent_review.collect_result: what may leave the sandbox workspace.

The black-box cases are in tests/test-collect-result.sh; these add the
races and the cases it does not cover, and run the entry point in a
hostile environment with real inputs.
"""

import os
import pathlib
import socket
import subprocess
from typing import Any

import pytest

from agent_review import collect_result

AR_DIR = pathlib.Path(__file__).resolve().parents[2]
ENTRY = AR_DIR / "bin" / "collect-result"
REVIEW = b"a review\n"
META = b'{"status": "ok"}\n'


@pytest.fixture
def ws(tmp_path: pathlib.Path) -> pathlib.Path:
    """A workspace with ws/out/{review.md,meta.json} and a planted payload."""
    out = tmp_path / "ws" / "out"
    out.mkdir(parents=True)
    (out / "review.md").write_bytes(REVIEW)
    (out / "meta.json").write_bytes(META)
    (out / "payload.sh").write_bytes(b"evil\n")
    return tmp_path


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    rc = collect_result.main(list(args))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def collect(capsys: pytest.CaptureFixture[str], root: pathlib.Path, src: str | None = None) -> tuple[int, str]:
    rc, out, err = run(capsys, src or str(root / "ws" / "out"), str(root / "dest"))
    assert err == ""
    return rc, out


def dest_files(root: pathlib.Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in (root / "dest").iterdir()}


def warning(message: str) -> str:
    return f"::warning title=Agent review result::{message}\n"


def test_copies_only_the_expected_files(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = collect(capsys, ws)
    assert rc == 0
    assert out == f"Collected 'review.md' ({len(REVIEW)} bytes).\nCollected 'meta.json' ({len(META)} bytes).\n"
    assert dest_files(ws) == {"review.md": REVIEW, "meta.json": META}


@pytest.mark.parametrize("args", [[], ["a"], ["a", "b", "c"]])
def test_usage(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert run(capsys, *args) == (1, "", "Usage: collect-result <out-dir> <dest-dir>\n")


def test_dest_is_created_even_without_a_source(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = collect(capsys, tmp_path)
    assert rc == 0
    src = tmp_path / "ws" / "out"
    assert out == warning(f"'{src}' is missing, not a directory, or reached through a symlink; nothing collected.")
    assert dest_files(tmp_path) == {}


def test_symlinked_out_dir(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    (ws / "ws" / "out").rename(ws / "elsewhere")
    (ws / "ws" / "out").symlink_to(ws / "elsewhere")
    rc, out = collect(capsys, ws)
    assert rc == 0
    assert "reached through a symlink; nothing collected." in out
    assert dest_files(ws) == {}


def test_symlinked_parent(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    (ws / "ws").rename(ws / "real")
    (ws / "ws").symlink_to(ws / "real")
    rc, out = collect(capsys, ws)
    assert rc == 0
    assert "reached through a symlink; nothing collected." in out
    assert dest_files(ws) == {}


def test_out_dir_that_is_a_file(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = collect(capsys, ws, str(ws / "ws" / "out" / "review.md"))
    assert rc == 0
    assert "nothing collected." in out
    assert dest_files(ws) == {}


@pytest.mark.parametrize("suffix", ["/", "/.", "/../out", "//", ""])
def test_source_must_be_canonical(
    suffix: str, ws: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    src = str(ws / "ws" / "out") + suffix if suffix else "ws/out"
    monkeypatch.chdir(ws)
    rc, out = collect(capsys, ws, src)
    assert rc == 0
    assert "nothing collected." in out
    assert dest_files(ws) == {}


def test_out_dir_swapped_for_a_symlink_after_it_was_opened(
    ws: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory is used through its descriptor, never by path again."""
    (ws / "elsewhere").mkdir()
    (ws / "elsewhere" / "review.md").write_bytes(b"secret\n")
    real_open_source = collect_result.open_source

    def open_then_swap(src: str) -> int | None:
        fd = real_open_source(src)
        (ws / "ws" / "out").rename(ws / "ws" / "old")
        (ws / "ws" / "out").symlink_to(ws / "elsewhere")
        return fd

    monkeypatch.setattr(collect_result, "open_source", open_then_swap)
    rc, _ = collect(capsys, ws)
    assert rc == 0
    assert dest_files(ws) == {"review.md": REVIEW, "meta.json": META}


def test_parent_swapped_for_a_symlink_during_the_walk(
    ws: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swapping a parent after it was opened changes nothing either."""
    (ws / "elsewhere" / "out").mkdir(parents=True)
    (ws / "elsewhere" / "out" / "review.md").write_bytes(b"secret\n")
    real_open_directory = collect_result.open_path_directory

    def open_then_swap(path: str, *, dir_fd: int | None = None) -> int:
        fd = real_open_directory(path, dir_fd=dir_fd)
        if path == "ws":
            (ws / "ws").rename(ws / "old")
            (ws / "ws").symlink_to(ws / "elsewhere")
        return fd

    monkeypatch.setattr(collect_result, "open_path_directory", open_then_swap)
    rc, _ = collect(capsys, ws)
    assert rc == 0
    assert dest_files(ws) == {"review.md": REVIEW, "meta.json": META}


def test_file_swapped_for_a_symlink_between_check_and_open(
    ws: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lstat check passes, then review.md becomes a symlink: the open refuses it."""
    (ws / "secret").write_bytes(b"secret\n")
    real_stat = os.stat

    def stat_then_swap(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)
        if path == "review.md":
            (ws / "ws" / "out" / "review.md").unlink()
            (ws / "ws" / "out" / "review.md").symlink_to(ws / "secret")
        return result

    monkeypatch.setattr(os, "stat", stat_then_swap)
    rc, out = collect(capsys, ws)
    assert rc == 0
    assert warning("'review.md' is missing or not a regular file; skipped.") in out
    assert dest_files(ws) == {"meta.json": META}


def test_file_swapped_for_a_fifo_between_check_and_open(
    ws: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """fstat on the open descriptor catches what the lstat check missed, without blocking."""
    real_stat = os.stat

    def stat_then_swap(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)
        if path == "review.md":
            (ws / "ws" / "out" / "review.md").unlink()
            os.mkfifo(ws / "ws" / "out" / "review.md")
        return result

    monkeypatch.setattr(os, "stat", stat_then_swap)
    rc, out = collect(capsys, ws)
    assert rc == 0
    assert warning("'review.md' is missing or not a regular file; skipped.") in out
    assert dest_files(ws) == {"meta.json": META}


def test_file_growing_past_the_cap_while_read(
    ws: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    review = ws / "ws" / "out" / "review.md"
    review.write_bytes(b"x" * collect_result.MAX_BYTES)
    real_read = os.read

    def growing_read(fd: int, n: int) -> bytes:
        with open(review, "ab") as f:
            f.write(b"y")
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", growing_read)
    rc, out = collect(capsys, ws)
    assert rc == 0
    assert warning("'review.md' grew over the 65536-byte limit while reading; skipped.") in out
    assert dest_files(ws) == {"meta.json": META}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
@pytest.mark.parametrize("directory", ["ws", "ws/out"])
def test_search_only_directories(directory: str, ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Mode 0311 (search, no read): bash's path lookup worked, so must this."""
    (ws / directory).chmod(0o311)
    try:
        rc, out = collect(capsys, ws)
    finally:
        (ws / directory).chmod(0o755)
    assert rc == 0
    assert out == f"Collected 'review.md' ({len(REVIEW)} bytes).\nCollected 'meta.json' ({len(META)} bytes).\n"
    assert dest_files(ws) == {"review.md": REVIEW, "meta.json": META}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
@pytest.mark.parametrize("mode", [0o444, 0o000])
def test_out_dir_without_search_permission(mode: int, ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Each file is refused on its own (bash said 'missing or not a regular file')."""
    (ws / "ws" / "out").chmod(mode)
    try:
        rc, out = collect(capsys, ws)
    finally:
        (ws / "ws" / "out").chmod(0o755)
    assert rc == 0
    assert out == (
        warning("'review.md' could not be read (Permission denied); skipped.")
        + warning("'meta.json' could not be read (Permission denied); skipped.")
    )
    assert dest_files(ws) == {}


def test_lstat_check_keeps_a_fifo_from_being_opened(
    ws: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (ws / "ws" / "out" / "review.md").unlink()
    os.mkfifo(ws / "ws" / "out" / "review.md")
    opened: list[object] = []
    real_open = os.open

    def recording_open(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    rc, out = collect(capsys, ws)
    assert rc == 0
    assert out.startswith(warning("'review.md' is missing or not a regular file; skipped."))
    assert "review.md" not in opened
    assert "meta.json" in opened  # the recording works


def test_size_limit(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    (ws / "ws" / "out" / "review.md").write_bytes(b"x" * collect_result.MAX_BYTES)
    (ws / "ws" / "out" / "meta.json").write_bytes(b"x" * (collect_result.MAX_BYTES + 1))
    rc, out = collect(capsys, ws)
    assert rc == 0
    assert out == (
        "Collected 'review.md' (65536 bytes).\n"
        + warning("'meta.json' is 65537 bytes, over the 65536-byte limit; skipped.")
    )
    assert dest_files(ws) == {"review.md": b"x" * collect_result.MAX_BYTES}


def test_directory_named_review_md(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    (ws / "ws" / "out" / "review.md").unlink()
    (ws / "ws" / "out" / "review.md").mkdir()
    (ws / "ws" / "out" / "review.md" / "x").write_bytes(b"x")
    rc, out = collect(capsys, ws)
    assert rc == 0
    assert out.startswith(warning("'review.md' is missing or not a regular file; skipped."))
    assert dest_files(ws) == {"meta.json": META}


def test_socket(ws: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    (ws / "ws" / "out" / "review.md").unlink()
    monkeypatch.chdir(ws / "ws" / "out")  # AF_UNIX paths are short
    with socket.socket(socket.AF_UNIX) as s:
        s.bind("review.md")
        rc, out = collect(capsys, ws)
    assert rc == 0
    assert out.startswith(warning("'review.md' is missing or not a regular file; skipped."))
    assert dest_files(ws) == {"meta.json": META}


def test_hard_link(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    (ws / "hostfile").write_bytes(b"host file\n")
    (ws / "ws" / "out" / "review.md").unlink()
    os.link(ws / "hostfile", ws / "ws" / "out" / "review.md")
    rc, out = collect(capsys, ws)
    assert rc == 0
    assert out.startswith(warning("'review.md' has 2 hard links; skipped."))
    assert dest_files(ws) == {"meta.json": META}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
def test_unreadable_file_is_skipped(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    (ws / "ws" / "out" / "review.md").chmod(0)
    rc, out = collect(capsys, ws)
    assert rc == 0
    assert out.startswith(warning("'review.md' could not be read (Permission denied); skipped."))
    assert dest_files(ws) == {"meta.json": META}


def test_non_utf8_content_is_copied_as_bytes(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = b"\xff\xfe\x00 not UTF-8 \xc3\x28\r\n::error::x\n"
    (ws / "ws" / "out" / "review.md").write_bytes(data)
    rc, _ = collect(capsys, ws)
    assert rc == 0
    assert dest_files(ws)["review.md"] == data


def test_empty_file(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    (ws / "ws" / "out" / "review.md").write_bytes(b"")
    rc, out = collect(capsys, ws)
    assert rc == 0
    assert out.startswith("Collected 'review.md' (0 bytes).\n")
    assert dest_files(ws)["review.md"] == b""


def test_existing_dest_files_are_replaced(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    (ws / "dest").mkdir()
    (ws / "dest" / "review.md").write_bytes(b"old")
    collect(capsys, ws)
    assert dest_files(ws)["review.md"] == REVIEW


def test_planted_tmp_symlink_in_dest_is_not_followed(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    (ws / "target").write_bytes(b"untouched")
    (ws / "dest").mkdir()
    (ws / "dest" / "review.md.tmp").symlink_to(ws / "target")
    (ws / "dest" / "meta.json").symlink_to(ws / "target")
    rc, _ = collect(capsys, ws)
    assert rc == 0
    assert (ws / "target").read_bytes() == b"untouched"
    assert dest_files(ws) == {"review.md": REVIEW, "meta.json": META}
    assert not (ws / "dest" / "meta.json").is_symlink()


def test_dest_that_is_a_symlink_fails(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    (ws / "elsewhere").mkdir()
    (ws / "dest").symlink_to(ws / "elsewhere")
    rc, out, err = run(capsys, str(ws / "ws" / "out"), str(ws / "dest"))
    assert rc == 1
    assert out == ""
    assert err.startswith(f"collect-result: cannot use '{ws / 'dest'}' as the destination: ")
    assert list((ws / "elsewhere").iterdir()) == []


def test_dest_write_error_fails(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    (ws / "dest" / "review.md.tmp").mkdir(parents=True)
    rc, out, err = run(capsys, str(ws / "ws" / "out"), str(ws / "dest"))
    assert rc == 1
    assert out == ""
    assert err.startswith(f"collect-result: cannot write 'review.md' to '{ws / 'dest'}': ")


def test_failed_rename_leaves_no_tmp(ws: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    (ws / "dest" / "review.md").mkdir(parents=True)
    rc, _, err = run(capsys, str(ws / "ws" / "out"), str(ws / "dest"))
    assert rc == 1
    assert err.startswith("collect-result: cannot write 'review.md'")
    assert sorted(p.name for p in (ws / "dest").iterdir()) == ["review.md"]
    assert (ws / "dest" / "review.md").is_dir()


def test_warning_data_is_escaped(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = str(tmp_path / "a%0A::error::b\nc")
    rc, out, _ = run(capsys, src, str(tmp_path / "dest"))
    assert rc == 0
    assert out.count("\n") == 1
    assert "a%250A::error::b%0Ac" in out


def test_undecodable_argument(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = os.fsdecode(bytes(tmp_path) + b"/\xff")
    rc, out, _ = run(capsys, src, str(tmp_path / "dest"))
    assert rc == 0
    assert "\\xff' is missing" in out


# --- The entry point, as the workflow runs it, in a hostile environment ------


def plant(directory: pathlib.Path, markers: pathlib.Path, names: list[str]) -> None:
    for name in names:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"open({str(markers / (directory.name + '-' + name.replace('/', '-')))!r}, 'w').close()\n")


def test_entry_point_in_a_hostile_environment(ws: pathlib.Path) -> None:
    markers = ws / "markers"
    markers.mkdir()
    cwd = ws / "ws" / "out"  # the sandbox could write here
    modules = ["json.py", "os.py", "stat.py", "errno.py", "sitecustomize.py", "agent_review/__init__.py"]
    plant(cwd, markers, modules)
    plant(ws / "pythonpath", markers, [*modules, "startup.py"])
    tools = ws / "bin"
    tools.mkdir()
    for tool in ("python3", "head", "stat", "mv", "mkdir", "realpath"):
        (tools / tool).write_text(f"#!/bin/sh\ntouch {markers / ('tool-' + tool)}\n")
        (tools / tool).chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "PYTHONPATH": str(ws / "pythonpath"),
        "PYTHONSTARTUP": str(ws / "pythonpath" / "startup.py"),
        "PYTHONINSPECT": "1",
    }
    result = subprocess.run(
        ["/usr/bin/python3", "-I", "-S", "-B", str(ENTRY), str(ws / "ws" / "out"), str(ws / "dest")],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert sorted(p.name for p in markers.iterdir()) == []
    assert dest_files(ws) == {"review.md": REVIEW, "meta.json": META}


def test_entry_point_does_not_block_on_a_fifo(ws: pathlib.Path) -> None:
    (ws / "ws" / "out" / "review.md").unlink()
    os.mkfifo(ws / "ws" / "out" / "review.md")
    result = subprocess.run(
        ["/usr/bin/python3", "-I", "-S", "-B", str(ENTRY), str(ws / "ws" / "out"), str(ws / "dest")],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0
    assert b"'review.md' is missing or not a regular file; skipped." in result.stdout
    assert dest_files(ws) == {"meta.json": META}
