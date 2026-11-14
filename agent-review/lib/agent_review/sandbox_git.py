"""git on pull request content, inside the sandbox (``run-review``).

Everything fetched from the pull request is untrusted, so git runs with
no system or global configuration (the host's may enable LFS filters or
other helpers), no terminal prompt, no LFS smudging and no replace objects
(``GIT_ENV``), and on every call with hooks and fsmonitor disabled,
symlinks checked out as symlinks, the ``file`` and ``ext`` transports
refused, no submodule recursion and no external diff (``SAFE_CONFIG``):
``.git`` could otherwise be influenced by repository content.

``GIT_ENV`` is part of the run's environment, which the harness inherits
too (the bash version exported it). Commands run in the sandbox zone: git
is found on ``PATH`` and the environment is inherited, so an inherited
``GIT_ALLOW_PROTOCOL`` reaches git (the offline test of the review phase
relies on it).
"""

from collections.abc import Mapping, Sequence

from agent_review import runner

GIT_ENV: Mapping[str, str] = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_LFS_SKIP_SMUDGE": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
}
SAFE_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.symlinks=true",
    "protocol.file.allow=never",
    "protocol.ext.allow=never",
    "submodule.recurse=false",
    "diff.external=",
    "advice.detachedHead=false",
)


class Git:
    """git with the safe settings, on the repository at ``repo_dir``.

    ``env`` holds the run's environment overrides (``GIT_ENV`` and the
    scratch ``HOME`` and ``TMPDIR``), on top of the inherited environment.
    """

    def __init__(self, repo_dir: str, env: Mapping[str, str]) -> None:
        self.repo_dir = repo_dir
        self.env = {**env, **GIT_ENV}

    def argv(self, args: Sequence[str]) -> list[str]:
        config = [item for setting in SAFE_CONFIG for item in ("-c", setting)]
        return ["git", *config, "-C", self.repo_dir, *args]

    def run(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        stdout_fd: int | None = None,
        quiet: bool = False,
        check: bool = True,
    ) -> runner.Result:
        """Runs git; its stderr goes to ours unless ``quiet`` (then it is dropped)."""
        return runner.run(
            runner.Zone.SANDBOX,
            self.argv(args),
            env={**self.env, **(env or {})},
            stdin=stdin,
            stdout_fd=stdout_fd,
            capture_stderr=quiet,
            check=check,
        )

    def output(self, *args: str, env: Mapping[str, str] | None = None, stdin: bytes | None = None) -> bytes:
        """The stdout of a git command that must succeed."""
        return self.run(*args, env=env, stdin=stdin).stdout

    def line(self, *args: str, quiet: bool = False) -> str | None:
        """The output without its trailing newlines, as ``$(git ...)``; None if git fails."""
        result = self.run(*args, quiet=quiet, check=False)
        if result.returncode != 0:
            return None
        return result.stdout.rstrip(b"\n").decode("utf-8", errors="strict")
