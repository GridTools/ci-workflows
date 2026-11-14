"""Differential test: the bash collect-result (tests/legacy/) against the Python port.

Temporary (D5): this file and tests/legacy/collect-result.sh are deleted in
the commit after the one that adds the port, once this has run in CI.

Each scenario builds the same tree under two roots and runs one
implementation in each, with the workflow's command line and PATH. Compared:
exit code, stdout, stderr (with the root replaced by '<ROOT>') and a
snapshot of everything under the root afterwards (files, symlinks, FIFOs),
so side effects outside the destination count too. Every difference is
listed in DIFFERENT with the reason it is an improvement, and its expected
outcome on each side is asserted.
"""

import os
import pathlib
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import pytest

AR_DIR = pathlib.Path(__file__).resolve().parents[2]
LEGACY = ["/usr/bin/bash", str(AR_DIR / "tests" / "legacy" / "collect-result.sh")]
PYTHON = ["/usr/bin/python3", "-I", "-S", "-B", str(AR_DIR / "bin" / "collect-result")]
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}
SRC = "<ROOT>/ws/out"
DEST = "<ROOT>/dest"
KIB64 = 65536


@dataclass(frozen=True)
class Outcome:
    rc: int
    stdout: str
    stderr: str
    tree: dict[str, object]


def snapshot(root: pathlib.Path) -> dict[str, object]:
    tree: dict[str, object] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = pathlib.Path(dirpath) / name
            rel = str(path.relative_to(root))
            st = path.lstat()
            if path.is_symlink():
                tree[rel] = ("link", os.readlink(path).replace(str(root), "<ROOT>"))
            elif path.is_fifo() or path.is_socket():
                tree[rel] = ("special",)
            elif path.is_dir():
                tree[rel] = ("dir",)
            elif st.st_mode & 0o400:
                tree[rel] = ("file", path.read_bytes(), st.st_nlink)
            else:
                tree[rel] = ("unreadable", st.st_size)
    return tree


def setup_out(root: pathlib.Path) -> pathlib.Path:
    out = root / "ws" / "out"
    out.mkdir(parents=True)
    (out / "review.md").write_bytes(b"a review\n")
    (out / "meta.json").write_bytes(b'{"status": "ok"}\n')
    (out / "payload.sh").write_bytes(b"evil\n")
    return out


def replace_review(root: pathlib.Path, make: Callable[[pathlib.Path], object]) -> None:
    out = setup_out(root)
    (out / "review.md").unlink()
    make(out / "review.md")


def plain(root: pathlib.Path) -> None:
    setup_out(root)


def sized(n: int) -> Callable[[pathlib.Path], None]:
    def build(root: pathlib.Path) -> None:
        (setup_out(root) / "review.md").write_bytes(b"x" * n)

    return build


def symlinked_out(root: pathlib.Path) -> None:
    (root / "elsewhere").mkdir()
    (root / "elsewhere" / "review.md").write_bytes(b"secret\n")
    (root / "ws").mkdir()
    (root / "ws" / "out").symlink_to(root / "elsewhere")


def symlinked_parent(root: pathlib.Path) -> None:
    setup_out(root)
    (root / "ws").rename(root / "real")
    (root / "ws").symlink_to(root / "real")


def hard_link(root: pathlib.Path) -> None:
    (root / "hostfile").write_bytes(b"host file\n")
    replace_review(root, lambda p: os.link(root / "hostfile", p))


def socket_file(root: pathlib.Path) -> None:
    out = setup_out(root)
    (out / "review.md").unlink()
    cwd = os.getcwd()
    os.chdir(out)
    try:
        s = socket.socket(socket.AF_UNIX)
        s.bind("review.md")
        s.close()
    finally:
        os.chdir(cwd)


def unreadable(root: pathlib.Path) -> None:
    (setup_out(root) / "review.md").chmod(0)


def dest_symlink(root: pathlib.Path) -> None:
    setup_out(root)
    (root / "elsewhere").mkdir()
    (root / "dest").symlink_to(root / "elsewhere")


def tmp_symlink_in_dest(root: pathlib.Path) -> None:
    setup_out(root)
    (root / "target").write_bytes(b"untouched\n")
    (root / "dest").mkdir()
    (root / "dest" / "review.md.tmp").symlink_to(root / "target")


def stale_dest(root: pathlib.Path) -> None:
    setup_out(root)
    (root / "dest").mkdir()
    (root / "dest" / "review.md").write_bytes(b"old\n")
    (root / "dest" / "meta.json.tmp").write_bytes(b"stale\n")


def missing_meta(root: pathlib.Path) -> None:
    (setup_out(root) / "meta.json").unlink()


def non_utf8(root: pathlib.Path) -> None:
    (setup_out(root) / "review.md").write_bytes(b"\xff\xfe\x00 \xc3\x28\r\n::error::x\n##[warning]y\n")


# name -> (build, src, dest); src and dest are relative to the root unless absolute.
SCENARIOS: dict[str, tuple[Callable[[pathlib.Path], object], str, str]] = {
    "copies_expected": (plain, SRC, DEST),
    "symlinked_file": (lambda r: replace_review(r, lambda p: p.symlink_to("/etc/passwd")), SRC, DEST),
    "symlink_to_proc_environ": (lambda r: replace_review(r, lambda p: p.symlink_to("/proc/self/environ")), SRC, DEST),
    "dangling_symlink": (lambda r: replace_review(r, lambda p: p.symlink_to(r / "nothing")), SRC, DEST),
    "symlinked_out_dir": (symlinked_out, SRC, DEST),
    "symlinked_parent": (symlinked_parent, SRC, DEST),
    "oversized": (sized(70000), SRC, DEST),
    "one_over_limit": (sized(KIB64 + 1), SRC, DEST),
    "at_limit": (sized(KIB64), SRC, DEST),
    "empty": (sized(0), SRC, DEST),
    "fifo": (lambda r: replace_review(r, os.mkfifo), SRC, DEST),
    "socket": (socket_file, SRC, DEST),
    "directory": (lambda r: replace_review(r, lambda p: p.mkdir()), SRC, DEST),
    "hard_link": (hard_link, SRC, DEST),
    "missing_out_dir": (lambda r: None, SRC, DEST),
    "missing_meta": (missing_meta, SRC, DEST),
    "out_is_a_file": (plain, SRC + "/review.md", DEST),
    "relative_src": (plain, "ws/out", DEST),
    "trailing_slash": (plain, SRC + "/", DEST),
    "dot_component": (plain, "<ROOT>/ws/./out", DEST),
    "dotdot_component": (plain, "<ROOT>/ws/out/../out", DEST),
    "double_slash": (plain, "<ROOT>/ws//out", DEST),
    "non_utf8_content": (non_utf8, SRC, DEST),
    "stale_dest_files": (stale_dest, SRC, DEST),
    "dest_nested_new": (plain, SRC, "<ROOT>/a/b/dest"),
    "percent_in_src": (lambda r: None, "<ROOT>/a%0Ab/out", DEST),
    "newline_in_src": (lambda r: None, "<ROOT>/a\n::error::b/out", DEST),
    "unreadable_file": (unreadable, SRC, DEST),
    "dest_is_a_symlink": (dest_symlink, SRC, DEST),
    "tmp_symlink_in_dest": (tmp_symlink_in_dest, SRC, DEST),
}

MISSING_SRC = "'{}' is missing, not a directory, or reached through a symlink; nothing collected."
W = "::warning title=Agent review result::"


def _assert_percent(legacy: Outcome, port: Outcome) -> None:
    assert legacy.stdout == f"{W}{MISSING_SRC.format('<ROOT>/a%0Ab/out')}\n"
    assert port.stdout == f"{W}{MISSING_SRC.format('<ROOT>/a%250Ab/out')}\n"


def _assert_newline(legacy: Outcome, port: Outcome) -> None:
    assert legacy.stdout.count("\n") == 2  # the second line starts with '::error::'
    assert "\n::error::b/out" in legacy.stdout
    assert port.stdout == f"{W}{MISSING_SRC.format('<ROOT>/a%0A::error::b/out')}\n"


def _assert_unreadable(legacy: Outcome, port: Outcome) -> None:
    assert legacy.rc == 1
    assert legacy.stdout == ""
    assert "Permission denied" in legacy.stderr
    assert legacy.tree["dest/review.md.tmp"] == ("file", b"", 1)
    assert "dest/meta.json" not in legacy.tree
    assert port.rc == 0
    assert port.stdout == (
        f"{W}'review.md' could not be read (Permission denied); skipped.\nCollected 'meta.json' (17 bytes).\n"
    )
    assert "dest/review.md.tmp" not in port.tree
    assert port.tree["dest/meta.json"] == ("file", b'{"status": "ok"}\n', 1)


def _assert_dest_symlink(legacy: Outcome, port: Outcome) -> None:
    assert legacy.rc == 0
    assert legacy.tree["elsewhere/review.md"] == ("file", b"a review\n", 1)
    assert port.rc == 1
    assert port.stdout == ""
    assert port.stderr.startswith("collect-result: cannot use '<ROOT>/dest' as the destination: ")
    assert "elsewhere/review.md" not in port.tree


def _assert_tmp_symlink(legacy: Outcome, port: Outcome) -> None:
    # bash's '>' followed the planted symlink and wrote through it.
    assert legacy.tree["target"] == ("file", b"a review\n", 1)
    assert legacy.tree["dest/review.md"] == ("link", "<ROOT>/target")
    assert port.rc == 0
    assert port.tree["target"] == ("file", b"untouched\n", 1)
    assert port.tree["dest/review.md"] == ("file", b"a review\n", 1)


# Scenario -> (why the port's behaviour is an improvement, check of both outcomes).
DIFFERENT: dict[str, tuple[str, Callable[[Outcome, Outcome], None]]] = {
    "percent_in_src": ("warning data is escaped ('%' -> '%25'), so it is shown literally", _assert_percent),
    "newline_in_src": ("a newline in the path cannot start a new workflow command", _assert_newline),
    "unreadable_file": (
        "an unreadable file is skipped with a warning, like every other refused file; bash died"
        " (set -e), left an empty review.md.tmp and did not collect meta.json",
        _assert_unreadable,
    ),
    "dest_is_a_symlink": ("a symlinked destination is refused instead of followed", _assert_dest_symlink),
    "tmp_symlink_in_dest": (
        "the temporary file is created with O_EXCL | O_NOFOLLOW; bash's '>' wrote through a planted symlink",
        _assert_tmp_symlink,
    ),
}


def run(argv: list[str], root: pathlib.Path, scenario: str) -> Outcome:
    build, src, dest = SCENARIOS[scenario]
    root.mkdir()
    build(root)
    result = subprocess.run(
        [*argv, src.replace("<ROOT>", str(root)), dest.replace("<ROOT>", str(root))],
        cwd=root,
        env=ENV,
        capture_output=True,
        timeout=20,
        check=False,
    )
    tree = snapshot(root)
    for path in root.rglob("*"):  # let pytest clean up
        if not path.is_symlink() and path.is_file():
            path.chmod(0o644)

    def norm(b: bytes) -> str:
        return b.decode("utf-8", errors="backslashreplace").replace(str(root), "<ROOT>")

    return Outcome(result.returncode, norm(result.stdout), norm(result.stderr), tree)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_differential(scenario: str, tmp_path: pathlib.Path) -> None:
    legacy = run(LEGACY, tmp_path / "bash", scenario)
    port = run(PYTHON, tmp_path / "python", scenario)
    if scenario in DIFFERENT:
        assert legacy != port, f"{scenario}: listed as different, but both behave the same"
        DIFFERENT[scenario][1](legacy, port)
    else:
        assert port == legacy


def test_differences_are_known_scenarios() -> None:
    assert set(DIFFERENT) <= set(SCENARIOS)
