"""Test fixtures for run-review: crafted repositories, trusted roots and workspaces.

A crafted pull request lives in a local "upstream" repository. run-review
always fetches from ``https://github.com/<repo>.git`` with the ``file``
transport refused; the tests redirect that URL with an inherited
``url.<file URL>.insteadOf`` (``GIT_CONFIG_COUNT``) and allow the ``file``
transport with ``GIT_ALLOW_PROTOCOL``, which overrides ``protocol.*.allow``.
Neither implementation has a test hook: both read their environment as in
production (the sandbox zone inherits it).
"""

import json
import os
import pathlib
import shutil
import subprocess
from collections.abc import Mapping, Sequence

AR_DIR = pathlib.Path(__file__).resolve().parents[1]
LEGACY = AR_DIR / "tests" / "legacy" / "run-review.sh"
PYTHON_CALL = ["/usr/bin/python3", "-I", "-S", "-B"]

COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}

Tree = Mapping[str, str | bytes | None]


def git(repo: pathlib.Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *args],
        env={**os.environ, **COMMIT_ENV},
        input=stdin,
        capture_output=True,
        check=True,
    ).stdout


def write_tree(repo: pathlib.Path, tree: Tree) -> None:
    """Applies ``tree`` to the work tree: text or bytes, '-> target' for a symlink, None to delete."""
    for rel, content in tree.items():
        path = repo / rel
        if content is None:
            git(repo, "rm", "-r", "-q", "--", rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            git(repo, "rm", "-r", "-q", "--", rel)
            shutil.rmtree(path, ignore_errors=True)
        if isinstance(content, str) and content.startswith("-> "):
            path.symlink_to(content[3:])
        elif isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        git(repo, "add", "--", rel)


def commit(repo: pathlib.Path, tree: Tree, message: str = "c") -> str:
    write_tree(repo, tree)
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD").decode().strip()


class Upstream:
    """A crafted upstream repository: a fork point, a base and a head commit."""

    def __init__(self, path: pathlib.Path, slug: str = "example-org/example") -> None:
        self.path = path
        self.slug = slug
        path.mkdir(parents=True)
        git(path, "init", "-q", "-b", "main")
        git(path, "config", "uploadpack.allowAnySHA1InWant", "true")

    def make(self, fork: Tree, base: Tree, head: Tree) -> tuple[str, str, str]:
        """(fork, base, head): base and head both branch off the fork point."""
        fork_sha = commit(self.path, fork, "fork")
        git(self.path, "checkout", "-q", "-b", "pr")
        head_sha = commit(self.path, head, "head")
        git(self.path, "checkout", "-q", "main")
        base_sha = commit(self.path, base, "base")
        return fork_sha, base_sha, head_sha

    def env(self) -> dict[str, str]:
        """Environment that makes run-review fetch from here instead of github.com."""
        return {
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.file://{self.path}.insteadOf",
            "GIT_CONFIG_VALUE_0": f"https://github.com/{self.slug}.git",
        }


def make_root(
    directory: pathlib.Path,
    *,
    legacy: bool = False,
    rules: str = "",
    extra: str = "",
    addendum: bytes = b"",
    central: str | None = None,
) -> pathlib.Path:
    """A trusted root as the review job lays it out under /opt/agent-review."""
    directory.mkdir(parents=True)
    for name in ("bin", "lib", "harnesses"):
        shutil.copytree(AR_DIR / name, directory / name, symlinks=True)
    for name in ("prompt.md.tmpl", "instruction-files.txt"):
        shutil.copy(AR_DIR / name, directory / name)
    if legacy:
        shutil.copy(LEGACY, directory / "bin" / "run-review")
    if central is not None:
        (directory / "instruction-files.txt").write_text(central)
    subprocess.run(
        ["/usr/bin/bash", str(directory / "harnesses/stub/install.sh"), str(directory / "harness/stub")],
        capture_output=True,
        check=True,
    )
    config = directory / "config"
    config.mkdir()
    (config / "rule-files.txt").write_text(rules)
    (config / "extra-instruction-files.txt").write_text(extra)
    (config / "prompt-addendum.md").write_bytes(addendum)
    return directory


def set_adapter(root: pathlib.Path, script: str) -> None:
    """Replaces the stub adapter's run.sh."""
    (root / "harnesses" / "stub" / "run.sh").write_text(script)


def make_ws(
    directory: pathlib.Path,
    *,
    repo: str = "example-org/example",
    pr: int | str = 7,
    head: str,
    base: str,
    pr_json: bytes | Mapping[str, object] | None = None,
    **params: object,
) -> pathlib.Path:
    (directory / "in").mkdir(parents=True)
    values: dict[str, object] = {
        "repo": repo,
        "pr": pr,
        "head_sha": head,
        "base_sha": base,
        "harness": "stub",
        "model": "none",
        "harness_timeout_minutes": 5,
        "stub_padding_kib": 0,
        "project": "Example",
    }
    values.update(params)
    (directory / "in" / "params.json").write_text(json.dumps(values))
    if pr_json is None:
        pr_json = {"title": "A title", "body": "A body."}
    data = pr_json if isinstance(pr_json, bytes) else json.dumps(pr_json).encode()
    (directory / "in" / "pr.json").write_bytes(data)
    return directory


def entry_argv(root: pathlib.Path) -> list[str]:
    """The production command line of the root's run-review, by its kind (as tests/lib.sh decides)."""
    entry = root / "bin" / "run-review"
    if entry.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash"):
        return ["/usr/bin/bash", str(entry)]
    return [*PYTHON_CALL, str(entry)]


def run_review(
    root: pathlib.Path, args: Sequence[str], env: Mapping[str, str] | None = None, timeout: float = 600
) -> subprocess.CompletedProcess[bytes]:
    """Runs run-review; stdout and stderr together, as AWF's '2>&1'."""
    return subprocess.run(
        [*entry_argv(root), *args],
        env={**os.environ, "LC_ALL": "C.UTF-8", **(env or {})},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
