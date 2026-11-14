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
import selectors
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class Zone(enum.Enum):
    TRUSTED_HOST = "trusted-host"
    SANDBOX = "sandbox"
    TAINTED_HOST = "tainted-host"


# The whole environment of a command on the tainted host.
FIXED_ENV: Mapping[str, str] = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}
TAINTED_TOOL_DIRS = ("/usr/bin", "/bin")
# Bytes of stderr kept (the last ones) in a Result or CommandError.
STDERR_EXCERPT_BYTES = 4096
# Default cap on a command's stdout; more kills the command.
MAX_STDOUT_BYTES = 16 * 1024 * 1024
_CHUNK = 1 << 16


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


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def _communicate(
    proc: "subprocess.Popen[bytes]", stdin: bytes | None, max_stdout: int, deadline: float | None
) -> tuple[bytes, bytes, str | None]:
    """Feeds stdin and reads stdout and stderr until EOF; returns (stdout, stderr tail, abort reason)."""
    out = bytearray()
    err = bytearray()
    pending = memoryview(stdin or b"")
    assert proc.stdout is not None  # noqa: S101 (always a pipe here)
    stdin_fd = proc.stdin.fileno() if proc.stdin is not None else -1
    stdout_fd = proc.stdout.fileno()
    with selectors.DefaultSelector() as selector:
        if proc.stdin is not None:
            os.set_blocking(stdin_fd, False)
            selector.register(stdin_fd, selectors.EVENT_WRITE)
        selector.register(stdout_fd, selectors.EVENT_READ)
        if proc.stderr is not None:
            selector.register(proc.stderr.fileno(), selectors.EVENT_READ)
        while selector.get_map():
            remaining = _remaining(deadline)
            if remaining == 0.0:
                return bytes(out), bytes(err), "timeout"
            for key, _ in selector.select(remaining):
                fd = key.fd
                if fd == stdin_fd:
                    try:
                        pending = pending[os.write(fd, pending[:_CHUNK]) :]
                    except BrokenPipeError:
                        pending = pending[len(pending) :]
                    if not pending and proc.stdin is not None:
                        selector.unregister(fd)
                        proc.stdin.close()
                    continue
                chunk = os.read(fd, _CHUNK)
                if not chunk:
                    selector.unregister(fd)
                elif fd == stdout_fd:
                    out += chunk
                    if len(out) > max_stdout:
                        return bytes(out), bytes(err), "output"
                else:
                    err += chunk
                    del err[:-STDERR_EXCERPT_BYTES]
    return bytes(out), bytes(err), None


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
    max_stdout: int = MAX_STDOUT_BYTES,
) -> Result:
    """Runs ``argv`` (never through a shell) and returns its output as bytes.

    ``env`` holds overrides on top of the zone's environment. Without
    ``stdin`` the command reads from /dev/null. With ``capture_stderr``
    false, stderr goes to this process's stderr; otherwise its last
    STDERR_EXCERPT_BYTES are kept. More than ``max_stdout`` bytes of stdout,
    or the ``timeout``, kills the command and raises CommandError. With
    ``check``, a non-zero exit raises CommandError.
    """
    args = _check_argv(argv)
    full_env = environment(zone, env)
    program = args[0]
    args[0] = resolve(zone, program, full_env)
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        proc = subprocess.Popen(  # noqa: S603 (argument list, resolved program)
            args,
            stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_stderr else None,
            env=full_env,
            cwd=cwd,
            close_fds=True,
        )
    except OSError as e:
        raise CommandError(program, f"could not run: {e.strerror}") from e
    with proc:
        stdout, stderr, abort = _communicate(proc, stdin, max_stdout, deadline)
        if abort is None:
            try:
                returncode = proc.wait(timeout=_remaining(deadline))
            except subprocess.TimeoutExpired:
                abort = "timeout"
        if abort is not None:
            proc.kill()
            proc.wait()
            if abort == "timeout":
                raise CommandError(program, f"timed out after {timeout} s", stderr=stderr)
            raise CommandError(program, f"more than {max_stdout} bytes of output", stderr=stderr)
    if check and returncode != 0:
        raise CommandError(program, f"exit code {returncode}", returncode, stderr)
    return Result(returncode, stdout, stderr)
