"""In-sandbox driver of the agent review: ``run-review``; harness-agnostic.

Usage: ``run-review [--phase fetch|review|all] <workspace>``

Runs INSIDE the AWF sandbox (see docs/agent-review.md), in two sandbox
runs, so that the agent never has network access:

``fetch``
    fetches the PR from github.com (the only allowed domain), with every
    object it needs, prepares the checkout and renders the prompt; no
    inference key, no harness;
``review``
    runs the harness with no allowed domain (only the inference listener)
    and collects its review.

``all`` (the default) does both in one run, for local tests. Everything
fetched from the pull request is untrusted: git runs with hooks,
global/system config, submodules and LFS disabled (``sandbox_git``), and
nothing from the checkout is executed here. The harness adapter is the only
component that acts on the content, and it holds no secret.

Inputs, written by the trusted host before the sandbox starts:
``<workspace>/in/params.json`` (validated parameters: repo, pr, shas,
harness, ...) and ``<workspace>/in/pr.json`` (the raw GitHub API response
for the PR, untrusted). Outputs: ``<workspace>/out/review.md`` (the review,
at most REVIEW_MAX_BYTES) and ``<workspace>/out/meta.json`` (the status of
the run).

Exit codes: 0 review written (or, for ``fetch``, the checkout prepared), 2
the harness ran but produced no review, 1 anything else.

The harness contract is unchanged: ``bash <adapter>/run.sh`` under
``timeout``, in the checkout, with ``PROMPT_FILE``, ``REPO_DIR``,
``OUT_DIR``, ``CONTEXT_DIR``, ``MODEL``, ``HARNESS_PREFIX``, ``PR_NUMBER``
and ``STUB_PADDING_KIB`` set, and the run's ``HOME``, ``TMPDIR`` and forced
``GIT_*`` variables on top of the inherited environment; exit 0 means a
review, 2 none.
"""

import contextlib
import json
import os
import secrets
import shutil
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import agent_review
from agent_review import files, prompt, reset, runner, validate
from agent_review.review_errors import Fatal
from agent_review.sandbox_git import GIT_ENV, Git

USAGE = "Usage: run-review [--phase fetch|review|all] <workspace>"
PHASES = ("fetch", "review", "all")
# Leaves room for the comment header under GitHub's 65536-character limit
# and under the 64 KiB 'collect-result' cap on the host.
REVIEW_MAX_BYTES = 60000
TRUNCATION_NOTE = b"\n\n_[Review truncated: it exceeded %d bytes.]_\n" % REVIEW_MAX_BYTES
PARAMS_MAX_BYTES = 1 << 20
EXIT_NO_REVIEW = 2
# A three-dot diff needs the merge base, which a shallow fetch may lack.
FETCH_DEPTH = 50
DEEPEN_STEPS = (200, 1000, 5000)
HEADING = b"### "
_SCAN_CHUNK = 1 << 20

TIMEOUT_MINUTES = validate.pattern(r"[1-9][0-9]{0,2}")
STUB_PADDING_KIB = validate.pattern(r"[0-9]{1,5}")


def one_line(text: str) -> str:
    """``text`` with CR and LF escaped: values in messages (a parameter, a path
    from the PR tree) cannot add lines to the log."""
    return text.replace("\r", "\\r").replace("\n", "\\n")


def _write(stream: Any, text: str) -> None:
    """One line; non-UTF-8 bytes of a path are written back as they were."""
    stream.buffer.write((one_line(text) + "\n").encode("utf-8", errors="surrogateescape"))
    stream.flush()


def say(text: str) -> None:
    """A progress line on stdout."""
    _write(sys.stdout, text)


def trusted_root() -> str:
    """The directory holding bin/, lib/ and the trusted files (/opt/agent-review in the sandbox)."""
    lib = os.path.dirname(os.path.dirname(os.path.realpath(agent_review.__file__)))
    return os.path.dirname(lib)


@dataclass(frozen=True)
class Params:
    repo: str
    pr_number: str
    head_sha: str
    base_sha: str
    harness: str
    model: str
    timeout_minutes: str
    stub_padding_kib: str
    project: str


def _tostring(value: Any) -> str:
    """jq's ``tostring``: strings as they are, anything else as compact JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_params(path: str) -> Params:
    """The validated parameters (validated in 'authorize' already; checked again
    because they become paths, URLs and command arguments)."""
    try:
        data = json.loads(files.read_text(path, PARAMS_MAX_BYTES))
    except (OSError, ValueError, RecursionError) as e:
        raise Fatal(f"Cannot read the parameters from '{path}': {e}") from e
    if not isinstance(data, dict):
        raise Fatal(f"'{path}' does not hold a JSON object.")

    def param(key: str, default: str | None = None) -> str:
        value = data.get(key)
        if default is not None and (value is None or value is False):
            return default
        return _tostring(value)

    repo = param("repo")
    project = param("project", "") or repo.split("/", 1)[-1]
    params = Params(
        repo=repo,
        pr_number=param("pr"),
        head_sha=param("head_sha"),
        base_sha=param("base_sha"),
        harness=param("harness"),
        model=param("model"),
        timeout_minutes=param("harness_timeout_minutes"),
        stub_padding_kib=param("stub_padding_kib", "0"),
        project=project,
    )
    for value, pattern, what in (
        (params.repo, validate.REPO, "repo"),
        (params.pr_number, validate.POSITIVE_INT, "PR number"),
        (params.head_sha, validate.SHA, "head sha"),
        (params.base_sha, validate.SHA, "base sha"),
        (params.harness, validate.HARNESS, "harness"),
        (params.model, validate.MODEL, "model"),
        (params.timeout_minutes, TIMEOUT_MINUTES, "timeout"),
        (params.stub_padding_kib, STUB_PADDING_KIB, "stub padding"),
        (params.project, validate.PROJECT, "project"),
    ):
        if not validate.matches(pattern, value):
            raise Fatal(f"Invalid {what} '{value}'.")
    return params


def remove(path: str) -> None:
    """``rm -rf``: a symlink is removed, never followed."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


class Run:
    """One run of run-review on a workspace."""

    def __init__(self, ws: str, root: str, params: Params, config: reset.Config) -> None:
        self.ws = ws
        self.root = root
        self.params = params
        self.config = config
        self.in_dir = os.path.join(ws, "in")
        self.repo_dir = os.path.join(ws, "repo")
        self.context_dir = os.path.join(self.repo_dir, reset.CONTEXT_NAME)
        self.harness_out = os.path.join(ws, "harness-out")
        self.result_dir = os.path.join(ws, "out")
        self.scratch_home = os.path.join(ws, "scratch-home")
        self.scratch_tmp = os.path.join(ws, "scratch-tmp")
        self.prompt_file = os.path.join(ws, "prompt.md")
        self.adapter_dir = os.path.join(root, "harnesses", params.harness)
        self.harness_prefix = os.path.join(root, "harness", params.harness)
        # The environment overrides of every command (git and the harness):
        # what the bash version exported.
        self.env: dict[str, str] = dict(GIT_ENV)

    def _scratch(self, *dirs: str) -> None:
        """Fresh directories, and HOME and TMPDIR in the workspace, the only
        directory the sandbox may write to ('filesystem.allowWrite')."""
        for directory in (*dirs, self.scratch_home, self.scratch_tmp):
            remove(directory)
        for directory in (*dirs, self.scratch_home, self.scratch_tmp):
            os.makedirs(directory, exist_ok=True)
        self.env.update(HOME=self.scratch_home, TMPDIR=self.scratch_tmp)

    # --- Fetch ----------------------------------------------------------------

    def fetch(self) -> None:
        p = self.params
        for path in (self.prompt_file, self.harness_out, self.result_dir):
            remove(path)
        self._scratch(self.repo_dir)
        git = Git(self.repo_dir, self.env)
        git.run("init", "-q")
        git.run("remote", "add", "origin", f"https://github.com/{p.repo}.git")

        # Fetch by sha so the reviewed commit is exactly the one authorized;
        # never fall back to the moving 'refs/pull/N/head'. Full objects, no
        # partial clone: the review phase has no network, so nothing can be
        # fetched lazily there.
        git.run("fetch", "-q", "--no-tags", f"--depth={FETCH_DEPTH}", "origin", p.head_sha, p.base_sha)
        if git.line("rev-parse", "--verify", f"{p.head_sha}^{{commit}}") != p.head_sha:
            raise Fatal(f"Fetched head does not match {p.head_sha}.")
        if git.line("rev-parse", "--verify", f"{p.base_sha}^{{commit}}") != p.base_sha:
            raise Fatal(f"Fetched base does not match {p.base_sha}.")
        merge_base = self._merge_base(git)

        git.run("checkout", "-q", "--detach", p.head_sha)

        # --- Context files ---
        # The PR may itself contain '.agent-review-context'; git removes it
        # safely (it does not follow symlinks), then the directory is created fresh.
        git.run("rm", "-r", "-q", "-f", "--ignore-unmatch", "--", reset.CONTEXT_NAME)
        remove(self.context_dir)
        os.makedirs(os.path.join(self.context_dir, "pr-instruction-files"))
        info = os.path.join(self.repo_dir, ".git", "info")
        os.makedirs(info, exist_ok=True)
        files.append_bytes(os.path.join(info, "exclude"), f"/{reset.CONTEXT_NAME}/\n".encode())
        self._git_to_file(git, "diff.patch", "diff", "--no-ext-diff", "--no-textconv", merge_base, p.head_sha)
        self._git_to_file(git, "changed-files.txt", "diff", "--no-ext-diff", "--name-status", merge_base, p.head_sha)

        # --- Reset agent instruction files to the base version ---
        reset.reset_instruction_files(
            git,
            self.ws,
            head_sha=p.head_sha,
            base_sha=p.base_sha,
            patterns=self.config.reset_patterns,
            context_dir=self.context_dir,
        )
        say(
            reset.verify_followed_files(
                git, self.repo_dir, p.base_sha, self.config.rule_files, self.config.addendum_named
            )
        )
        reset.verify_no_linked_directories(
            git,
            self.repo_dir,
            base_sha=p.base_sha,
            patterns=self.config.reset_patterns,
            scratch_parent=self.ws,
            env=self.env,
        )

        # --- Render the prompt ---
        title, body = prompt.read_pr(os.path.join(self.in_dir, "pr.json"))
        values = prompt.PromptValues(
            project=p.project,
            repo=p.repo,
            pr_number=p.pr_number,
            head_sha=p.head_sha,
            base_sha=p.base_sha,
            merge_base=merge_base,
            rules=prompt.rules_bytes(self.config.rule_files),
            addendum=prompt.addendum_bytes(self.config.addendum),
            title=title,
            body=body,
        )
        files.write_new(self.prompt_file, prompt.render(self.config.template, values, prompt.new_nonce()))
        say(f"run-review: fetched {p.repo}#{p.pr_number} at {p.head_sha} (merge base {merge_base}).")

    def _merge_base(self, git: Git) -> str:
        """Deepens the shallow fetch until base and head have a merge base."""
        p = self.params
        for deepen in DEEPEN_STEPS:
            found = git.line("merge-base", p.base_sha, p.head_sha, quiet=True)
            if found is not None:
                return found
            git.run("fetch", "-q", "--no-tags", f"--deepen={deepen}", "origin", p.head_sha, p.base_sha)
        # As in bash: no merge-base attempt after the last deepening.
        git.run("fetch", "-q", "--no-tags", "--unshallow", "origin", p.head_sha, p.base_sha)
        found = git.line("merge-base", p.base_sha, p.head_sha)
        if found is None:
            raise Fatal("No merge base between base and head.")
        return found

    def _git_to_file(self, git: Git, name: str, *args: str) -> None:
        path = os.path.join(self.context_dir, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        try:
            git.run(*args, stdout_fd=fd)
        finally:
            os.close(fd)

    # --- Review -----------------------------------------------------------------

    def review(self) -> int:
        p = self.params
        # The checkout and prompt come from the fetch phase, which ran no agent.
        if not (os.path.isfile(self.prompt_file) and os.path.isdir(os.path.join(self.repo_dir, ".git"))):
            raise Fatal(f"No prepared checkout in '{self.ws}'; run the fetch phase first.")
        self._scratch(self.harness_out, self.result_dir)

        say(f"run-review: running harness '{p.harness}' on {p.repo}#{p.pr_number} at {p.head_sha}.")
        harness_exit = self._run_harness()
        self._fresh_result_dir()
        status = {0: "ok", 2: "no-review", 124: "timeout", 137: "timeout"}.get(harness_exit, "failed")

        # The harness output is agent-controlled: accept only a regular file.
        # The host re-checks this ('collect-result'); this check keeps the
        # failure readable.
        collected = read_review(os.path.join(self.harness_out, "review.md")) if status == "ok" else None
        if collected is None:
            if status == "ok":
                status = "no-review"
            self._write_meta(status, harness_exit, truncated=False, review_bytes=0)
            _write(sys.stderr, f"run-review: no review produced (status '{status}', harness exit {harness_exit}).")
            return EXIT_NO_REVIEW if status == "no-review" else 1

        review, truncated = collected
        files.write_new(os.path.join(self.result_dir, "review.md"), review)
        self._write_meta("ok", harness_exit, truncated=truncated, review_bytes=len(review))
        say(f"run-review: review written ({len(review)} bytes, truncated={'true' if truncated else 'false'}).")
        return 0

    def _fresh_result_dir(self) -> None:
        """Replaces out/ by an empty directory after the harness ran.

        The harness can write anywhere in the workspace, out/ included, and
        could make it impossible to empty (a read-only subdirectory), so the
        old out/ is renamed aside, under a name it cannot guess, and removed
        as far as possible. Only what this script writes into the new out/ is
        the result. A harness process left running could still write there
        until the sandbox is torn down: the workflow collects nothing while
        any sandbox container survives, and collect-result accepts only a
        regular review.md and meta.json, re-checked on the host.
        """
        if os.path.lexists(self.result_dir):
            aside = f"{self.result_dir}.harness-{secrets.token_hex(8)}"
            os.rename(self.result_dir, aside)
            with contextlib.suppress(OSError):  # else left in the workspace, outside out/
                remove(aside)
        os.mkdir(self.result_dir)

    def _run_harness(self) -> int:
        """The adapter's exit code (bash's '$?': 128 + n for a signal, 127 if it cannot start)."""
        p = self.params
        env = {
            **self.env,
            "PROMPT_FILE": self.prompt_file,
            "REPO_DIR": self.repo_dir,
            "MODEL": p.model,
            "HARNESS_PREFIX": self.harness_prefix,
            "PR_NUMBER": p.pr_number,
            "CONTEXT_DIR": self.context_dir,
            "STUB_PADDING_KIB": p.stub_padding_kib,
            # The harness writes to its own directory; only this script writes the result.
            "OUT_DIR": self.harness_out,
        }
        argv = [
            "timeout",
            "--kill-after=30s",
            f"{p.timeout_minutes}m",
            "bash",
            os.path.join(self.adapter_dir, "run.sh"),
        ]
        try:
            # stdout and stderr pass through to the sandbox's output.
            result = runner.run(
                runner.Zone.SANDBOX,
                argv,
                env=env,
                cwd=self.repo_dir,
                stdout_fd=1,
                capture_stderr=False,
                check=False,
            )
        except runner.CommandError as e:
            _write(sys.stderr, f"run-review: {e}")
            return 127
        return result.returncode if result.returncode >= 0 else 128 - result.returncode

    def _write_meta(self, status: str, harness_exit: int, *, truncated: bool, review_bytes: int) -> None:
        meta = {
            "harness": self.params.harness,
            "model": self.params.model,
            "status": status,
            "harness_exit": harness_exit,
            "truncated": truncated,
            "review_bytes": review_bytes,
        }
        os.makedirs(self.result_dir, exist_ok=True)
        # The layout of 'jq -n' (two-space indent), which the bash version wrote.
        text = json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
        files.write_new(os.path.join(self.result_dir, "meta.json"), text.encode("utf-8"))


def _heading_offset(fd: int) -> int | None:
    """The offset of the first line that starts with '### ', or None."""
    if os.pread(fd, len(HEADING), 0) == HEADING:
        return 0
    needle = b"\n" + HEADING
    pos, tail = 0, b""
    while chunk := os.pread(fd, _SCAN_CHUNK, pos):
        window = tail + chunk
        found = window.find(needle)
        if found >= 0:
            return pos - len(tail) + found + 1
        tail = window[-(len(needle) - 1) :]
        pos += len(chunk)
    return None


def read_review(path: str) -> tuple[bytes, bool] | None:
    """The review to publish and whether it was truncated; None unless ``path`` is a non-empty regular file.

    Models often open their answer with a note ("Writing the final review
    now.") although the prompt asks them to start with '### Summary': the
    review is kept from its first '### ' heading, if any (as awk did, with
    a final newline). Over REVIEW_MAX_BYTES, it is cut and a note appended;
    that is decided from the bytes read, never from the file's size, which
    the agent can change meanwhile. Invalid or cut UTF-8 sequences are
    dropped. The file is scanned, never read whole: the agent controls its
    size.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
            return None
        start = _heading_offset(fd)
        # At most one byte more than the limit, so that 'too long' is known.
        wanted = REVIEW_MAX_BYTES + 1
        chunks, pos, got, at_end = [], start or 0, 0, False
        while got < wanted:
            chunk = os.pread(fd, wanted - got, pos)
            if not chunk:
                at_end = True
                break
            chunks.append(chunk)
            pos += len(chunk)
            got += len(chunk)
    finally:
        os.close(fd)
    body = b"".join(chunks)
    if start is not None and at_end and body and not body.endswith(b"\n"):
        body += b"\n"
    if len(body) > REVIEW_MAX_BYTES:
        return prompt.clip(body, REVIEW_MAX_BYTES - 100) + TRUNCATION_NOTE, True
    return prompt.clip(body, len(body)), False


def parse_args(args: Sequence[str]) -> tuple[str, str]:
    """(phase, workspace), with the bash version's messages."""
    phase = "all"
    rest = list(args)
    if rest and rest[0] == "--phase":
        if len(rest) < 2:
            raise Fatal(USAGE)
        phase, rest = rest[1], rest[2:]
    if phase not in PHASES:
        raise Fatal(f"Invalid phase '{phase}'.")
    if len(rest) != 1:
        raise Fatal(USAGE)
    ws = rest[0]
    if not ws.startswith("/"):
        raise Fatal(f"The workspace must be an absolute path, got '{ws}'.")
    return phase, ws


def run(args: Sequence[str], root: str) -> int:
    phase, ws = parse_args(args)
    params = load_params(os.path.join(ws, "in", "params.json"))
    # Caller configuration (trusted: the caller's validated inputs and its
    # addendum file at the caller's workflow commit), next to this script's
    # own files. Missing files mean "not configured".
    config = reset.load_config(root)
    reset.check_coverage(config, ws, GIT_ENV)
    current = Run(ws, root, params, config)
    if not os.path.isfile(os.path.join(current.adapter_dir, "run.sh")):
        raise Fatal(f"No adapter for harness '{params.harness}'.")
    if not os.path.isdir(current.harness_prefix):
        raise Fatal(f"Harness '{params.harness}' is not installed at '{current.harness_prefix}'.")
    if phase in ("fetch", "all"):
        current.fetch()
    if phase in ("review", "all"):
        return current.review()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv, trusted_root())
    except Fatal as e:
        message = str(e)
    except runner.CommandError as e:
        message = str(e)
    except OSError as e:
        message = f"{e.filename or ''}: {e.strerror or e}".lstrip(": ")
    _write(sys.stderr, f"run-review: {message}")
    return 1
