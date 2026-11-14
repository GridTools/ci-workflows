"""The only place that starts processes.

Commands are argument lists, never shell strings. How the program is found
and which environment it gets depend on the trust zone the caller runs in
(docs/adr/0003-python-runtime.md):

``TRUSTED_HOST``
    ``authorize`` and ``publish-comment post``: they run before any untrusted
    code, or on a fresh VM. A bare program name is looked up on ``PATH``
    and the environment is inherited, as the bash versions do (the tests put
    a fake ``gh`` first on ``PATH``).
``SANDBOX``
    ``run-review``, inside AWF: ``PATH`` lookup and the inherited
    environment, plus the caller's overrides (the forced ``GIT_*``
    settings). An inherited ``GIT_ALLOW_PROTOCOL`` reaches git.
``TAINTED_HOST``
    ``collect-result``, on the host after the sandbox has run: the program
    must be an absolute path under ``/usr/bin`` or ``/bin``, and the
    environment is fixed (``FIXED_ENV`` plus overrides, never ``PATH``):
    ``~/.local/bin`` and the like may hold planted programs.

The lookup never uses a relative ``PATH`` entry (an empty entry means the
working directory), and the program is resolved to an absolute path before
it runs.
"""

import enum
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class Zone(enum.Enum):
    TRUSTED_HOST = "trusted-host"
    SANDBOX = "sandbox"
    TAINTED_HOST = "tainted-host"


# The whole environment of a command on the tainted host.
FIXED_ENV: Mapping[str, str] = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}
TAINTED_TOOL_DIRS = ("/usr/bin", "/bin")
# Bytes of stderr kept in a CommandError.
STDERR_EXCERPT_BYTES = 4096


class CommandError(Exception):
    """A command could not be started, timed out or exited non-zero."""

    def __init__(self, program: str, message: str, returncode: int | None = None, stderr: bytes = b"") -> None:
        super().__init__(f"{program}: {message}")
        self.program = program
        self.returncode = returncode
        self.stderr = stderr[-STDERR_EXCERPT_BYTES:]


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: bytes
    stderr: bytes


def _check_argv(argv: Sequence[str]) -> list[str]:
    if isinstance(argv, str | bytes) or not argv:
        raise ValueError("argv must be a non-empty list of strings")
    args = list(argv)
    for arg in args:
        if not isinstance(arg, str):
            raise TypeError(f"argument {arg!r} is not a string")
        if "\0" in arg:
            raise ValueError("an argument contains a NUL character")
    return args


def find_program(name: str, path: str) -> str:
    """Resolves a bare program name on ``path``, skipping relative entries."""
    if not name or "/" in name or name in (".", ".."):
        raise ValueError(f"not a bare program name: {name!r}")
    for directory in path.split(os.pathsep):
        if not directory.startswith("/"):
            continue
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise CommandError(name, "not found on PATH")


def resolve(zone: Zone, program: str, env: Mapping[str, str]) -> str:
    """The absolute path of ``program`` as ``zone`` allows it."""
    if zone is Zone.TAINTED_HOST:
        directory, name = os.path.split(program)
        if directory not in TAINTED_TOOL_DIRS or name in ("", ".", ".."):
            raise ValueError(
                f"on the tainted host, programs must be absolute paths under /usr/bin or /bin: {program!r}"
            )
        return program
    if program.startswith("/"):
        return program
    return find_program(program, env.get("PATH", ""))


def environment(zone: Zone, overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a command gets in ``zone``."""
    overrides = dict(overrides or {})
    for key, value in overrides.items():
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError(f"invalid environment override {key!r}")
    if zone is Zone.TAINTED_HOST:
        if "PATH" in overrides:
            raise ValueError("PATH is fixed on the tainted host")
        return {**FIXED_ENV, **overrides}
    return {**os.environ, **overrides}


def run(
    zone: Zone,
    argv: Sequence[str],
    *,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    timeout: float | None = None,
    check: bool = True,
    capture_stderr: bool = True,
) -> Result:
    """Runs ``argv`` (never through a shell) and returns its output as bytes.

    ``env`` holds overrides on top of the zone's environment. Without
    ``stdin`` the command reads from /dev/null. With ``capture_stderr``
    false, stderr goes to this process's stderr. With ``check``, a non-zero
    exit raises CommandError.
    """
    args = _check_argv(argv)
    full_env = environment(zone, env)
    program = args[0]
    args[0] = resolve(zone, program, full_env)
    try:
        completed = subprocess.run(  # noqa: S603 (argument list, resolved program)
            args,
            input=stdin,
            stdin=subprocess.DEVNULL if stdin is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_stderr else None,
            env=full_env,
            cwd=cwd,
            timeout=timeout,
            check=False,
            close_fds=True,
        )
    except subprocess.TimeoutExpired as e:
        raise CommandError(program, f"timed out after {timeout} s") from e
    except OSError as e:
        raise CommandError(program, f"could not run: {e.strerror}") from e
    stderr = completed.stderr if capture_stderr else b""
    if check and completed.returncode != 0:
        raise CommandError(program, f"exit code {completed.returncode}", completed.returncode, stderr)
    return Result(completed.returncode, completed.stdout, stderr)
