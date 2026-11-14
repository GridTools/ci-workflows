"""agent_review.run_review: CLI, parameters, review collection, and the entry point on crafted PRs.

The entry-point tests run bin/run-review as the workflow does, on crafted
pull requests served from a local repository (run_review_fixtures), with
no network.
"""

import json
import os
import pathlib
import re

import pytest
from run_review_fixtures import Upstream, make_root, make_ws, set_adapter
from run_review_fixtures import run_review as run_entry

from agent_review import run_review
from agent_review.review_errors import Fatal

SHA_A, SHA_B = "a" * 40, "b" * 40

# --- CLI and parameters ------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["/ws"], ("all", "/ws")),
        (["--phase", "fetch", "/ws"], ("fetch", "/ws")),
        (["--phase", "review", "/ws"], ("review", "/ws")),
    ],
)
def test_parse_args(args: list[str], expected: tuple[str, str]) -> None:
    assert run_review.parse_args(args) == expected


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], run_review.USAGE),
        (["--phase"], run_review.USAGE),
        (["--phase", "x", "/ws"], "Invalid phase 'x'."),
        (["--phase", "", "/ws"], "Invalid phase ''."),
        (["--phase", "fetch"], run_review.USAGE),
        (["/a", "/b"], run_review.USAGE),
        (["ws"], "The workspace must be an absolute path, got 'ws'."),
    ],
)
def test_parse_args_errors(args: list[str], message: str) -> None:
    with pytest.raises(Fatal) as e:
        run_review.parse_args(args)
    assert str(e.value) == message


def write_params(tmp_path: pathlib.Path, **changes: object) -> str:
    params: dict[str, object] = {
        "repo": "GridTools/gt4py",
        "pr": 2983,
        "head_sha": SHA_A,
        "base_sha": SHA_B,
        "harness": "stub",
        "model": "none",
        "harness_timeout_minutes": 5,
        "project": "GT4Py",
    }
    params.update(changes)
    params = {k: v for k, v in params.items() if v is not ...}
    path = tmp_path / "params.json"
    path.write_text(json.dumps(params))
    return str(path)


def test_load_params(tmp_path: pathlib.Path) -> None:
    params = run_review.load_params(write_params(tmp_path))
    assert params == run_review.Params("GridTools/gt4py", "2983", SHA_A, SHA_B, "stub", "none", "5", "0", "GT4Py")
    # The workflow passes strings.
    assert run_review.load_params(write_params(tmp_path, pr="12", harness_timeout_minutes="30")).pr_number == "12"
    # Defaults: the project is the repository name, the padding 0.
    defaulted = run_review.load_params(write_params(tmp_path, project=..., stub_padding_kib=None))
    assert (defaulted.project, defaulted.stub_padding_kib) == ("gt4py", "0")
    assert run_review.load_params(write_params(tmp_path, project="")).project == "gt4py"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"repo": "a/b/c"}, "Invalid repo 'a/b/c'."),
        ({"repo": ...}, "Invalid repo 'null'."),
        ({"pr": 0}, "Invalid PR number '0'."),
        ({"pr": "1\n"}, "Invalid PR number '1\n'."),
        ({"pr": "\uff11"}, "Invalid PR number '\uff11'."),
        ({"pr": True}, "Invalid PR number 'true'."),
        ({"pr": 1.0}, "Invalid PR number '1.0'."),
        ({"head_sha": "A" * 40}, f"Invalid head sha '{'A' * 40}'."),
        ({"base_sha": "b" * 39}, f"Invalid base sha '{'b' * 39}'."),
        ({"harness": "Stub"}, "Invalid harness 'Stub'."),
        ({"model": "-x"}, "Invalid model '-x'."),
        ({"harness_timeout_minutes": 1000}, "Invalid timeout '1000'."),
        ({"harness_timeout_minutes": ...}, "Invalid timeout 'null'."),
        ({"stub_padding_kib": "123456"}, "Invalid stub padding '123456'."),
        ({"project": "-x"}, "Invalid project '-x'."),
    ],
)
def test_load_params_rejects(tmp_path: pathlib.Path, changes: dict[str, object], message: str) -> None:
    with pytest.raises(Fatal) as e:
        run_review.load_params(write_params(tmp_path, **changes))
    assert str(e.value) == message


@pytest.mark.parametrize("data", [b"[]", b"{", b"\xff"])
def test_load_params_bad_file(tmp_path: pathlib.Path, data: bytes) -> None:
    (tmp_path / "params.json").write_bytes(data)
    with pytest.raises(Fatal):
        run_review.load_params(str(tmp_path / "params.json"))


def test_load_params_missing_file(tmp_path: pathlib.Path) -> None:
    with pytest.raises(Fatal, match="Cannot read the parameters"):
        run_review.load_params(str(tmp_path / "params.json"))


# --- Collecting the review ------------------------------------------------------------


def collect(tmp_path: pathlib.Path, data: bytes) -> tuple[bytes, bool] | None:
    path = tmp_path / "review.md"
    path.write_bytes(data)
    return run_review.read_review(str(path))


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"### Summary\nok\n", b"### Summary\nok\n"),
        (b"Writing it now.\n\n### Summary\nok", b"### Summary\nok\n"),  # awk adds the final newline
        (b"no heading", b"no heading"),  # cat does not
        (b"#### deeper\n### Summary\n", b"### Summary\n"),
        (b"x ### not a heading\n", b"x ### not a heading\n"),
        (b"### a\xff\xc3\n", b"### a\n"),  # invalid bytes dropped
        (b"\n### a\n", b"### a\n"),
    ],
)
def test_read_review(tmp_path: pathlib.Path, data: bytes, expected: bytes) -> None:
    assert collect(tmp_path, data) == (expected, False)


def test_read_review_heading_across_scan_chunks(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_review, "_SCAN_CHUNK", 7)
    for prefix in range(20):
        data = b"x" * prefix + b"\n### H\nbody"
        assert collect(tmp_path, data) == (b"### H\nbody\n", False), prefix


def test_read_review_truncates(tmp_path: pathlib.Path) -> None:
    exact = b"### S\n" + b"x" * (run_review.REVIEW_MAX_BYTES - 7) + b"\n"
    assert collect(tmp_path, exact) == (exact, False)
    # Without the final newline, awk's added one counts too.
    assert collect(tmp_path, exact[:-1]) == (exact, False)
    over = b"### S\nx" + "\u00e9".encode() * 40000  # 59900 falls inside a character
    review, truncated = collect(tmp_path, over) or (b"", False)
    assert truncated
    assert review.endswith(b"\n\n_[Review truncated: it exceeded 60000 bytes.]_\n")
    kept = review[: -len(run_review.TRUNCATION_NOTE)]
    assert len(kept) == run_review.REVIEW_MAX_BYTES - 100 - 1  # the cut character is dropped
    kept.decode("utf-8", errors="strict")
    # Off by one: one byte over the limit, through awk's final newline.
    one_over = b"### S\n" + b"x" * (run_review.REVIEW_MAX_BYTES - 6)
    assert collect(tmp_path, one_over + b"\n") == collect(tmp_path, one_over)
    assert (collect(tmp_path, one_over) or (b"", False))[1]


def test_read_review_refuses_other_files(tmp_path: pathlib.Path) -> None:
    assert collect(tmp_path, b"") is None
    (tmp_path / "real.md").write_text("### S\n")
    (tmp_path / "link.md").symlink_to(tmp_path / "real.md")
    assert run_review.read_review(str(tmp_path / "link.md")) is None
    os.mkfifo(tmp_path / "fifo.md")
    assert run_review.read_review(str(tmp_path / "fifo.md")) is None
    (tmp_path / "dir.md").mkdir()
    assert run_review.read_review(str(tmp_path / "dir.md")) is None
    assert run_review.read_review(str(tmp_path / "missing.md")) is None


# --- The entry point on crafted pull requests --------------------------------------------

FORK = {
    "AGENTS.md": "base rules\n",
    "docs/RULES.md": "base doc rules\n",
    "docs/OLD.md": "old rules\n",
    "src/code.py": "x = 1\n",
}


@pytest.fixture
def upstream(tmp_path: pathlib.Path) -> Upstream:
    return Upstream(tmp_path / "upstream")


# A git first on PATH that skips the reset's two writing commands (the
# only ones with '--literal-pathspecs'): the reset subverted, so only the
# post-condition stands between the PR's files and the agent.
GIT_SHIM = """#!/bin/sh
for arg in "$@"; do
  [ "$arg" = --literal-pathspecs ] && { echo "git shim: skipped $*" >&2; exit 0; }
done
exec /usr/bin/git "$@"
"""


def shim_env(tmp_path: pathlib.Path) -> dict[str, str]:
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "git").write_text(GIT_SHIM)
    (shim / "git").chmod(0o755)
    return {"PATH": f"{shim}:{os.environ['PATH']}"}


@pytest.mark.parametrize(
    ("head", "rules", "message"),
    [
        # A rule file left at the PR's version.
        ({"docs/RULES.md": "PR rules\n"}, "docs/RULES.md", "'docs/RULES.md' is not its base version in the checkout"),
        # A rule file the PR adds, absent at base.
        ({"NEW_RULES.md": "PR\n"}, "NEW_RULES.md", "'NEW_RULES.md' is not in the base commit but is in the checkout"),
        # A rule file the PR deletes.
        ({"docs/OLD.md": None}, "docs/OLD.md", "'docs/OLD.md' is not its base version in the checkout"),
        # A symlinked parent directory.
        (
            {"docs": "-> evil", "evil/RULES.md": "PR rules\n"},
            "docs/RULES.md",
            "Followed file 'docs/RULES.md': 'docs' is a symlink in the checkout.",
        ),
    ],
)
def test_post_condition_stops_the_fetch_phase(
    tmp_path: pathlib.Path, upstream: Upstream, head: dict[str, str | None], rules: str, message: str
) -> None:
    _, base_sha, head_sha = upstream.make(FORK, {}, head)
    root = make_root(tmp_path / "root", rules=rules + "\n")
    ws = make_ws(tmp_path / "ws", head=head_sha, base=base_sha)
    # Positive control: with the real reset the same PR passes.
    ok = run_entry(root, ["--phase", "fetch", str(ws)], env=upstream.env())
    assert ok.returncode == 0, ok.stdout.decode()
    assert b"1 rule file(s) and 0 file(s) named by the addendum are their base versions." in ok.stdout
    (ws / "prompt.md").unlink()

    result = run_entry(root, ["--phase", "fetch", str(ws)], env={**upstream.env(), **shim_env(tmp_path)})
    log = result.stdout.decode()
    assert result.returncode == 1, log
    assert "git shim: skipped" in log  # the reset really was skipped
    assert f"run-review: Followed file {message}" in log or f"run-review: {message}" in log
    assert not (ws / "prompt.md").exists()


def test_harness_contract(tmp_path: pathlib.Path, upstream: Upstream) -> None:
    _, base_sha, head_sha = upstream.make(FORK, {}, {"src/code.py": "x = 2\n"})
    root = make_root(tmp_path / "root")
    set_adapter(
        root,
        'env -0 > "${OUT_DIR}/env"\npwd > "${OUT_DIR}/pwd"\n'
        "printf '### Summary\\nfrom the adapter\\n' > \"${OUT_DIR}/review.md\"\necho adapter-stdout\n",
    )
    ws = make_ws(tmp_path / "ws", head=head_sha, base=base_sha, stub_padding_kib=3)
    result = run_entry(root, [str(ws)], env={**upstream.env(), "INHERITED_MARKER": "1"})
    log = result.stdout.decode()
    assert result.returncode == 0, log
    # The adapter's output reaches the log, after run-review's own line.
    assert re.search(r"running harness 'stub'.*\nadapter-stdout\n.*review written", log, re.DOTALL), log
    env = dict(item.split("=", 1) for item in (ws / "harness-out" / "env").read_text().split("\0") if "=" in item)
    assert {k: env[k] for k in ("PROMPT_FILE", "REPO_DIR", "OUT_DIR", "CONTEXT_DIR", "HARNESS_PREFIX")} == {
        "PROMPT_FILE": f"{ws}/prompt.md",
        "REPO_DIR": f"{ws}/repo",
        "OUT_DIR": f"{ws}/harness-out",
        "CONTEXT_DIR": f"{ws}/repo/.agent-review-context",
        "HARNESS_PREFIX": f"{root}/harness/stub",
    }
    assert (env["MODEL"], env["PR_NUMBER"], env["STUB_PADDING_KIB"]) == ("none", "7", "3")
    assert (env["HOME"], env["TMPDIR"]) == (f"{ws}/scratch-home", f"{ws}/scratch-tmp")
    assert {k: env[k] for k in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT")} == {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    assert (env["GIT_LFS_SKIP_SMUDGE"], env["GIT_NO_REPLACE_OBJECTS"]) == ("1", "1")
    # Inherited, including GIT_ALLOW_PROTOCOL.
    assert (env["INHERITED_MARKER"], env["GIT_ALLOW_PROTOCOL"]) == ("1", "file")
    assert (ws / "harness-out" / "pwd").read_text() == f"{ws}/repo\n"
    meta = json.loads((ws / "out" / "meta.json").read_text())
    assert meta == {
        "harness": "stub",
        "model": "none",
        "status": "ok",
        "harness_exit": 0,
        "truncated": False,
        "review_bytes": 29,
    }
    assert (ws / "out" / "meta.json").read_text().startswith('{\n  "harness": "stub",\n')
    assert (ws / "out" / "review.md").read_bytes() == b"### Summary\nfrom the adapter\n"


@pytest.mark.parametrize(
    ("script", "code", "status", "harness_exit"),
    [
        ("exit 2", 2, "no-review", 2),
        ("exit 3", 1, "failed", 3),
        (': > "${OUT_DIR}/review.md"', 2, "no-review", 0),
        ('ln -s /etc/passwd "${OUT_DIR}/review.md"', 2, "no-review", 0),
        ('mkfifo "${OUT_DIR}/review.md"', 2, "no-review", 0),
        ('echo "### S" > "${OUT_DIR}/review.md"; exit 124', 1, "timeout", 124),
        # Killed by a signal: 'timeout' reports 128 + 9, which bash also counted as a timeout.
        ('echo "### S" > "${OUT_DIR}/review.md"; kill -KILL $$', 1, "timeout", 137),
    ],
)
def test_review_status(
    tmp_path: pathlib.Path, upstream: Upstream, *, script: str, code: int, status: str, harness_exit: int
) -> None:
    _, base_sha, head_sha = upstream.make(FORK, {}, {})
    root = make_root(tmp_path / "root")
    ws = make_ws(tmp_path / "ws", head=head_sha, base=base_sha)
    assert run_entry(root, ["--phase", "fetch", str(ws)], env=upstream.env()).returncode == 0
    set_adapter(root, script + "\n")
    # The review phase needs no network: no file transport any more.
    result = run_entry(root, ["--phase", "review", str(ws)], env={"GIT_ALLOW_PROTOCOL": "none"})
    log = result.stdout.decode()
    assert result.returncode == code, log
    meta = json.loads((ws / "out" / "meta.json").read_text())
    assert (meta["status"], meta["harness_exit"], meta["review_bytes"]) == (status, harness_exit, 0)
    assert f"no review produced (status '{status}', harness exit {harness_exit})." in log
    assert not (ws / "out" / "review.md").exists()


def test_no_adapter_or_harness(tmp_path: pathlib.Path) -> None:
    root = make_root(tmp_path / "root")
    ws = make_ws(tmp_path / "ws", head=SHA_A, base=SHA_B, harness="nope")
    result = run_entry(root, ["--phase", "review", str(ws)])
    assert (result.returncode, result.stdout) == (1, b"run-review: No adapter for harness 'nope'.\n")
    (root / "harnesses" / "nope").mkdir()
    (root / "harnesses" / "nope" / "run.sh").write_text("exit 0\n")
    result = run_entry(root, ["--phase", "review", str(ws)])
    assert result.stdout == f"run-review: Harness 'nope' is not installed at '{root}/harness/nope'.\n".encode()


def test_coverage_scratch_is_removed(tmp_path: pathlib.Path) -> None:
    root = make_root(tmp_path / "root", rules="AGENTS.md\n", addendum=b"Follow `src/AGENTS.md`.\n")
    ws = make_ws(tmp_path / "ws", head=SHA_A, base=SHA_B)
    result = run_entry(root, ["--phase", "review", str(ws)])
    assert b"run the fetch phase first" in result.stdout
    assert sorted(p.name for p in ws.iterdir()) == ["in"]


def test_messages_are_one_line() -> None:
    assert run_review.one_line("a\nb\r\nc") == "a\\nb\\r\\nc"
