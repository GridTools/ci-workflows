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
        "repo": "example-org/example",
        "pr": 2983,
        "head_sha": SHA_A,
        "base_sha": SHA_B,
        "harness": "stub",
        "model": "none",
        "harness_timeout_minutes": 5,
        "project": "Example",
    }
    params.update(changes)
    params = {k: v for k, v in params.items() if v is not ...}
    path = tmp_path / "params.json"
    path.write_text(json.dumps(params))
    return str(path)


def test_load_params(tmp_path: pathlib.Path) -> None:
    params = run_review.load_params(write_params(tmp_path))
    assert params == run_review.Params("example-org/example", "2983", SHA_A, SHA_B, "stub", "none", "5", "0", "Example")
    # The workflow passes strings.
    assert run_review.load_params(write_params(tmp_path, pr="12", harness_timeout_minutes="30")).pr_number == "12"
    # Defaults: the project is the repository name, the padding 0.
    defaulted = run_review.load_params(write_params(tmp_path, project=..., stub_padding_kib=None))
    assert (defaulted.project, defaulted.stub_padding_kib) == ("example", "0")
    assert run_review.load_params(write_params(tmp_path, project="")).project == "example"


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


PLANT = (
    'printf \'{"status": "ok", "harness": "forged"}\' > "${OUT_DIR}/../out/meta.json"\n'
    'echo "### Forged" > "${OUT_DIR}/../out/review.md"\n'
    'echo extra > "${OUT_DIR}/../out/extra"\n'
)


@pytest.mark.parametrize(
    ("tail", "code", "names"),
    [
        ('echo "### Summary" > "${OUT_DIR}/review.md"', 0, ["meta.json", "review.md"]),
        ("exit 2", 2, ["meta.json"]),
    ],
)
def test_out_holds_only_run_reviews_files(
    tmp_path: pathlib.Path, upstream: Upstream, tail: str, code: int, names: list[str]
) -> None:
    """The harness can write into out/; it is recreated after the harness exits."""
    _, base_sha, head_sha = upstream.make(FORK, {}, {})
    root = make_root(tmp_path / "root")
    ws = make_ws(tmp_path / "ws", head=head_sha, base=base_sha)
    assert run_entry(root, ["--phase", "fetch", str(ws)], env=upstream.env()).returncode == 0
    set_adapter(root, PLANT + tail + "\n")
    result = run_entry(root, ["--phase", "review", str(ws)])
    assert result.returncode == code, result.stdout.decode()
    assert sorted(p.name for p in (ws / "out").iterdir()) == names
    assert json.loads((ws / "out" / "meta.json").read_text())["harness"] == "stub"
    if code == 0:
        assert (ws / "out" / "review.md").read_text() == "### Summary\n"


def test_deep_history_deepens_the_fetch(tmp_path: pathlib.Path, upstream: Upstream) -> None:
    """The merge base lies beyond the first fetch's depth (50), so the fetch deepens."""
    fork_sha, base_sha, head_sha = upstream.make(FORK, {}, {"AGENTS.md": "PR\n"}, depth=60)
    root = make_root(tmp_path / "root", rules="AGENTS.md\n")
    ws = make_ws(tmp_path / "ws", head=head_sha, base=base_sha)
    result = run_entry(root, ["--phase", "fetch", str(ws)], env=upstream.env())
    log = result.stdout.decode()
    assert result.returncode == 0, log
    assert f"(merge base {fork_sha})." in log
    assert f"`{fork_sha}`" in (ws / "prompt.md").read_text()
    assert (ws / "repo" / "AGENTS.md").read_text() == "base rules\n"


@pytest.mark.parametrize(
    "head",
    [
        {".agent-review-context": "-> /etc"},
        {".agent-review-context/diff.patch": "PR diff\n", ".agent-review-context/AGENTS.md": "PR\n"},
    ],
)
def test_pr_with_its_own_context_directory(tmp_path: pathlib.Path, upstream: Upstream, head: dict[str, str]) -> None:
    _, base_sha, head_sha = upstream.make(FORK, {}, head)
    root = make_root(tmp_path / "root")
    ws = make_ws(tmp_path / "ws", head=head_sha, base=base_sha)
    result = run_entry(root, ["--phase", "fetch", str(ws)], env=upstream.env())
    assert result.returncode == 0, result.stdout.decode()
    context = ws / "repo" / ".agent-review-context"
    assert context.is_dir() and not context.is_symlink()
    assert sorted(p.name for p in context.iterdir()) == ["changed-files.txt", "diff.patch", "pr-instruction-files"]
    # run-review's own files, not the PR's (which appear in them as changes).
    assert (context / "diff.patch").read_bytes().startswith(b"diff --git a/.agent-review-context")
    assert (context / "changed-files.txt").read_text().startswith("A\t.agent-review-context")
    assert "/.agent-review-context/" in (ws / "repo" / ".git" / "info" / "exclude").read_text()


# --- Symlinks and gitlinks where a pattern expects a directory (invariant 6) ---------------

EVIL = {
    "evil/agent/x.md": "PR agent\n",
    "evil/plugin/p.ts": "PR plugin\n",
    "evil/copilot-instructions.md": "PR copilot\n",
}


def real_tree(path: pathlib.Path) -> dict[str, str]:
    """The regular files below ``path``, found with lstat; fails on any symlink."""
    assert not path.is_symlink()
    files = {}
    for directory, dirs, names in os.walk(path, followlinks=False):
        for name in (*dirs, *names):
            entry = pathlib.Path(directory, name)
            assert not entry.is_symlink(), entry
            if name in names:
                files[str(entry.relative_to(path))] = entry.read_text()
    return files


def fetch(
    tmp_path: pathlib.Path, upstream: Upstream, fork: dict[str, str], head: dict[str, str | None], **root_args: object
) -> tuple[pathlib.Path, str]:
    _, base_sha, head_sha = upstream.make(fork, {}, head)
    root = make_root(tmp_path / "root", **root_args)  # type: ignore[arg-type]
    ws = make_ws(tmp_path / "ws", head=head_sha, base=base_sha)
    result = run_entry(root, ["--phase", "fetch", str(ws)], env=upstream.env())
    return ws, result.stdout.decode() + f"\nexit {result.returncode}"


@pytest.mark.parametrize(
    ("link", "target"),
    [
        (".opencode", "evil"),  # no .opencode in base
        (".OpenCode", "evil"),  # case variant
        ("sub/.opencode", "../evil"),  # nested
        (".github", "evil"),  # a directory leading to .github/copilot-instructions.md
        (".claude/agents", "../evil"),  # below a directory pattern
    ],
)
def test_symlinked_instruction_directory_is_removed(
    tmp_path: pathlib.Path, upstream: Upstream, link: str, target: str
) -> None:
    ws, log = fetch(tmp_path, upstream, FORK, {**EVIL, link: f"-> {target}"})
    assert log.endswith("exit 0"), log
    repo = ws / "repo"
    assert not os.path.lexists(repo / link)
    for name in ("agent/x.md", "plugin/p.ts", "copilot-instructions.md"):
        assert not os.path.lexists(repo / link / name)
    # The PR's symlink is kept as data (its target).
    kept = repo / ".agent-review-context" / "pr-instruction-files" / f"{link}.txt"
    assert kept.read_text() == target
    assert (ws / "prompt.md").exists()


def test_gitlink_at_an_instruction_directory_is_removed(tmp_path: pathlib.Path, upstream: Upstream) -> None:
    ws, log = fetch(tmp_path, upstream, FORK, {".opencode": "gitlink " + "1" * 40})
    assert log.endswith("exit 0"), log
    assert not os.path.lexists(ws / "repo" / ".opencode")
    assert b".opencode" not in subprocess_ls_files(ws / "repo")


def subprocess_ls_files(repo: pathlib.Path) -> bytes:
    from run_review_fixtures import git

    return git(repo, "ls-files", "-s")


BASE_DIRS = {
    **FORK,
    ".opencode/agent/a.md": "base agent\n",
    "sub/.agents/skills/s.md": "base skill\n",
}


def test_instruction_directory_in_base_positive_control(tmp_path: pathlib.Path, upstream: Upstream) -> None:
    ws, log = fetch(tmp_path, upstream, BASE_DIRS, {".opencode/agent/a.md": "PR agent\n", ".opencode/new.md": "PR\n"})
    assert log.endswith("exit 0"), log
    assert real_tree(ws / "repo" / ".opencode") == {"agent/a.md": "base agent\n"}


@pytest.mark.parametrize(
    ("directory", "target", "base_files"),
    [
        (".opencode", "evil", {"agent/a.md": "base agent\n"}),
        ("sub/.agents", "../evil", {"skills/s.md": "base skill\n"}),
    ],
)
def test_pr_replacing_a_base_directory_by_a_symlink(
    tmp_path: pathlib.Path, upstream: Upstream, directory: str, target: str, base_files: dict[str, str]
) -> None:
    deleted: dict[str, str | None] = {f"{directory}/{name}": None for name in base_files}
    ws, log = fetch(
        tmp_path,
        upstream,
        BASE_DIRS,
        {**deleted, **EVIL, directory: f"-> {target}"},
        rules="AGENTS.md\n",
    )
    assert log.endswith("exit 0"), log
    # A real directory holding exactly the base files: no PR file is reachable.
    assert real_tree(ws / "repo" / directory) == base_files
    assert "1 rule file(s) and 0 file(s) named by the addendum are their base versions." in log
    assert (ws / "prompt.md").exists()


def test_linked_directory_post_condition_with_the_reset_subverted(tmp_path: pathlib.Path, upstream: Upstream) -> None:
    """The second layer: the reset's writes skipped, the symlink stays, the fetch stops."""
    _, base_sha, head_sha = upstream.make(FORK, {}, {**EVIL, ".opencode": "-> evil"})
    root = make_root(tmp_path / "root")
    ws = make_ws(tmp_path / "ws", head=head_sha, base=base_sha)
    result = run_entry(root, ["--phase", "fetch", str(ws)], env={**upstream.env(), **shim_env(tmp_path)})
    log = result.stdout.decode()
    assert result.returncode == 1, log
    assert "git shim: skipped" in log
    assert "run-review: '.opencode' is a symlink or a gitlink where the reset expects an instruction directory" in log
    assert not (ws / "prompt.md").exists()


@pytest.mark.parametrize(
    "fork",
    [
        {**FORK, ".agents-shared/x.md": "x\n", ".claude": "-> .agents-shared"},
        {**FORK, "skills/s.md": "x\n", ".claude/settings.json": "{}\n", ".claude/skills": "-> ../skills"},
        {**FORK, ".agents/skills/s.md": "x\n", ".claude/skills": "-> ../.agents/x/../skills"},
        {**FORK, ".agents/skills/s.md": "x\n", ".claude/skills": "-> /abs/.agents/skills"},
        {**FORK, ".agents/skills/s.md": "x\n", ".claude/skills": "-> ../../.agents/skills"},
    ],
)
def test_linked_directory_from_the_base_fails_closed(
    tmp_path: pathlib.Path, upstream: Upstream, fork: dict[str, str]
) -> None:
    """A base symlink there could point at files the PR changes: the run stops too."""
    ws, log = fetch(tmp_path, upstream, fork, {"src/code.py": "x = 2\n"})
    assert log.endswith("exit 1"), log
    assert "is a symlink or a gitlink where the reset expects" in log
    assert not (ws / "prompt.md").exists()


@pytest.mark.parametrize("change", [{}, {".agents/skills/s.md": "PR skill\n", ".claude/skills": "-> ../src"}])
def test_base_symlink_into_a_reset_directory_passes(
    tmp_path: pathlib.Path, upstream: Upstream, change: dict[str, str]
) -> None:
    """A base linking '.claude/skills -> ../.agents/skills' passes: the target is reset too."""
    fork = {**FORK, ".agents/skills/s.md": "base skill\n", ".claude/skills": "-> ../.agents/skills"}
    ws, log = fetch(tmp_path, upstream, fork, {"src/code.py": "x = 2\n", **change})
    assert log.endswith("exit 0"), log
    repo = ws / "repo"
    assert os.readlink(repo / ".claude" / "skills") == "../.agents/skills"
    assert (repo / ".claude" / "skills" / "s.md").read_text() == "base skill\n"


def chain(hops: int) -> dict[str, str]:
    """'.claude/skills' through ``hops`` - 1 base symlinks at the root to '.agents/skills'."""
    links = {f"hop{i}": f"-> hop{i + 1}" for i in range(1, hops - 1)}
    links[f"hop{hops - 1}"] = "-> .agents"
    return {**links, ".claude/skills": "-> ../hop1/skills" if hops > 1 else "-> ../.agents/skills"}


@pytest.mark.parametrize(
    ("links", "ok"),
    [
        # Two hops: '.claude/skills' -> 'shared/skills', where 'shared' -> '.agents'.
        ({"shared": "-> .agents", ".claude/skills": "-> ../shared/skills"}, True),
        # The last component a symlink too: '.agents/current' -> 'skills'.
        ({".agents/current": "-> skills", ".claude/skills": "-> ../.agents/current"}, True),
        (chain(8), True),  # eight symlinks in all
        (chain(9), False),  # nine: beyond the bound
        ({"loop1": "-> loop2", "loop2": "-> loop1", ".claude/skills": "-> ../loop1"}, False),  # a cycle
        ({"notes.md": "notes\n", ".claude/CLAUDE.md": "-> ../notes.md"}, False),  # a file no pattern resets
        ({"shared": "-> /etc", ".claude/skills": "-> ../shared/skills"}, False),  # a hop leaving the repository
    ],
)
def test_base_symlink_chains(tmp_path: pathlib.Path, upstream: Upstream, links: dict[str, str], ok: bool) -> None:
    fork = {**FORK, ".agents/skills/s.md": "base skill\n", **links}
    ws, log = fetch(tmp_path, upstream, fork, {"src/code.py": "x = 2\n"})
    if ok:
        assert log.endswith("exit 0"), log
        assert (ws / "repo" / ".claude" / "skills" / "s.md").read_text() == "base skill\n"
    else:
        assert log.endswith("exit 1"), log
        assert "is a symlink or a gitlink where the reset expects" in log
        assert not (ws / "prompt.md").exists()


def test_pr_changing_an_intermediate_hop_fails_closed(tmp_path: pathlib.Path, upstream: Upstream) -> None:
    """'shared' is not reset, but the PR cannot redirect it: every hop must be the base's own."""
    fork = {
        **FORK,
        ".agents/skills/s.md": "base skill\n",
        "shared": "-> .agents",
        ".claude/skills": "-> ../shared/skills",
    }
    ws, log = fetch(tmp_path, upstream, fork, {"evil/skills/s.md": "PR skill\n", "shared": "-> evil"})
    assert log.endswith("exit 1"), log
    assert "'.claude/skills' is a symlink or a gitlink where the reset expects" in log
    assert not (ws / "prompt.md").exists()


def test_out_that_cannot_be_emptied_is_replaced(tmp_path: pathlib.Path, upstream: Upstream) -> None:
    """A read-only directory in out/ made removing out/ fail: no meta.json, a planted review left."""
    _, base_sha, head_sha = upstream.make(FORK, {}, {})
    root = make_root(tmp_path / "root")
    ws = make_ws(tmp_path / "ws", head=head_sha, base=base_sha)
    assert run_entry(root, ["--phase", "fetch", str(ws)], env=upstream.env()).returncode == 0
    set_adapter(
        root,
        'out="${OUT_DIR}/../out"\nmkdir -p "${out}/d"\n: > "${out}/d/f"\necho "### Forged" > "${out}/review.md"\n'
        'chmod 500 "${out}/d"\necho "### Summary" > "${OUT_DIR}/review.md"\n',
    )
    try:
        result = run_entry(root, ["--phase", "review", str(ws)])
        assert result.returncode == 0, result.stdout.decode()
        assert sorted(p.name for p in (ws / "out").iterdir()) == ["meta.json", "review.md"]
        assert (ws / "out" / "review.md").read_text() == "### Summary\n"
        assert json.loads((ws / "out" / "meta.json").read_text())["status"] == "ok"
        # The old out/ is aside, under a random name.
        aside = [p.name for p in ws.iterdir() if p.name.startswith("out.harness-")]
        assert len(aside) == 1 and re.fullmatch(r"out\.harness-[0-9a-f]{16}", aside[0])
    finally:
        for d in ws.glob("out*/d"):
            d.chmod(0o755)


def test_truncation_is_decided_from_the_bytes_read(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The size fstat reports can be stale (the agent may still write): it decides nothing but emptiness."""
    path = tmp_path / "review.md"
    path.write_bytes(b"### S\n" + b"x" * 70000)
    real_fstat = os.fstat

    def stale_fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        return os.stat_result((st.st_mode, *tuple(st)[1:6], 10, *tuple(st)[7:]))

    monkeypatch.setattr(os, "fstat", stale_fstat)
    review, truncated = run_review.read_review(str(path)) or (b"", False)
    assert truncated
    assert review.endswith(run_review.TRUNCATION_NOTE)
