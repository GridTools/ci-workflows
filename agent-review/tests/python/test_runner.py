"""agent_review.runner: argument lists only, tool lookup and environment per trust zone."""

import os
import pathlib

import pytest

from agent_review import runner
from agent_review.runner import Zone


def tool(directory: pathlib.Path, name: str, script: str) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\n{script}\n")
    path.chmod(0o755)
    return path


def test_arguments_are_not_interpreted_by_a_shell(tmp_path: pathlib.Path) -> None:
    hostile = f"$(touch {tmp_path}/pwned); `touch {tmp_path}/pwned2` | ; && *"
    result = runner.run(Zone.TAINTED_HOST, ["/usr/bin/printf", "%s", hostile])
    assert result.stdout == hostile.encode()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("argv", [[], "ls -l", b"ls"])
def test_argv_must_be_a_list(argv: list[str]) -> None:
    with pytest.raises(ValueError, match="non-empty list"):
        runner.run(Zone.TRUSTED_HOST, argv)


def test_argv_rejects_nul_and_non_strings() -> None:
    with pytest.raises(ValueError, match="NUL"):
        runner.run(Zone.TRUSTED_HOST, ["/usr/bin/true", "a\0b"])
    with pytest.raises(TypeError):
        runner.run(Zone.TRUSTED_HOST, ["/usr/bin/true", 1])  # type: ignore[list-item]


def test_trusted_host_finds_tools_on_path_and_inherits_the_environment(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As the suites' fake 'gh' needs: first on PATH, reading FAKE_* variables.
    tool(tmp_path / "fakebin", "fake-tool", 'echo "fake ${FAKE_VALUE} $*"')
    monkeypatch.setenv("PATH", f"{tmp_path}/fakebin:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_VALUE", "inherited")
    result = runner.run(Zone.TRUSTED_HOST, ["fake-tool", "a b"])
    assert result.stdout == b"fake inherited a b\n"


def test_lookup_skips_relative_path_entries(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool(tmp_path / "rel", "planted", "echo planted")
    tool(tmp_path, "planted", "echo planted")
    monkeypatch.chdir(tmp_path)
    for path in ["", ":", "rel", ".", "rel:/usr/bin"]:
        with pytest.raises(runner.CommandError, match="not found on PATH"):
            runner.find_program("planted", path)
    with pytest.raises(ValueError, match="bare program name"):
        runner.find_program("rel/planted", "/usr/bin")


def test_relative_program_with_a_slash_is_refused() -> None:
    with pytest.raises(ValueError, match="bare program name"):
        runner.run(Zone.SANDBOX, ["./tool"])


def test_sandbox_inherits_the_environment_with_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    # The offline test in test-run-review.sh passes GIT_ALLOW_PROTOCOL this way.
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "none")
    out = runner.run(Zone.SANDBOX, ["env"], env={"GIT_CONFIG_NOSYSTEM": "1"}).stdout.decode()
    assert "GIT_ALLOW_PROTOCOL=none\n" in out
    assert "GIT_CONFIG_NOSYSTEM=1\n" in out


def test_tainted_host_has_a_fixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "must-not-pass")
    out = runner.run(Zone.TAINTED_HOST, ["/usr/bin/env"]).stdout.decode()
    assert sorted(out.splitlines()) == sorted(f"{k}={v}" for k, v in runner.FIXED_ENV.items())
    out = runner.run(Zone.TAINTED_HOST, ["/usr/bin/env"], env={"LC_ALL": "C"}).stdout.decode()
    assert "LC_ALL=C\n" in out
    with pytest.raises(ValueError, match="PATH is fixed"):
        runner.run(Zone.TAINTED_HOST, ["/usr/bin/env"], env={"PATH": "/tmp"})


@pytest.mark.parametrize(
    "program", ["env", "/home/runner/.local/bin/env", "/usr/bin/../../tmp/x", "/usr/bin/", "/bin/."]
)
def test_tainted_host_needs_absolute_system_tools(program: str) -> None:
    with pytest.raises(ValueError, match="absolute paths under /usr/bin or /bin"):
        runner.run(Zone.TAINTED_HOST, [program])


def test_invalid_environment_override() -> None:
    for key, value in [("A=B", "x"), ("", "x"), ("A", "x\0y")]:
        with pytest.raises(ValueError, match="invalid environment override"):
            runner.run(Zone.SANDBOX, ["/usr/bin/true"], env={key: value})


def test_failure_raises_with_stderr() -> None:
    with pytest.raises(runner.CommandError) as e:
        runner.run(Zone.TAINTED_HOST, ["/bin/sh", "-c", "echo oops >&2; exit 3"])
    assert e.value.returncode == 3
    assert e.value.stderr == b"oops\n"
    result = runner.run(Zone.TAINTED_HOST, ["/bin/sh", "-c", "exit 3"], check=False)
    assert result.returncode == 3


def test_stderr_excerpt_is_bounded() -> None:
    with pytest.raises(runner.CommandError) as e:
        runner.run(Zone.TAINTED_HOST, ["/bin/sh", "-c", "head -c 10000 /dev/zero >&2; exit 1"])
    assert len(e.value.stderr) == runner.STDERR_EXCERPT_BYTES


def test_missing_program() -> None:
    with pytest.raises(runner.CommandError, match="could not run"):
        runner.run(Zone.TAINTED_HOST, ["/usr/bin/no-such-tool-here"])
    with pytest.raises(runner.CommandError, match="not found on PATH"):
        runner.run(Zone.TRUSTED_HOST, ["no-such-tool-here"])


def test_stdin_is_devnull_unless_given() -> None:
    assert runner.run(Zone.TAINTED_HOST, ["/usr/bin/cat"]).stdout == b""
    assert runner.run(Zone.TAINTED_HOST, ["/usr/bin/cat"], stdin=b"\x00\xff").stdout == b"\x00\xff"


def test_timeout() -> None:
    with pytest.raises(runner.CommandError, match="timed out"):
        runner.run(Zone.TAINTED_HOST, ["/usr/bin/sleep", "5"], timeout=0.2)


def test_stdout_cap() -> None:
    assert (
        len(runner.run(Zone.TAINTED_HOST, ["/usr/bin/head", "-c", "1000", "/dev/zero"], max_stdout=1000).stdout) == 1000
    )
    with pytest.raises(runner.CommandError, match="more than 1000 bytes of output"):
        runner.run(Zone.TAINTED_HOST, ["/usr/bin/head", "-c", "1001", "/dev/zero"], max_stdout=1000)
    # An endless writer is killed, not read into memory.
    with pytest.raises(runner.CommandError, match="bytes of output"):
        runner.run(Zone.TAINTED_HOST, ["/usr/bin/cat", "/dev/zero"], max_stdout=1 << 20, timeout=30)


def test_large_stdin_and_stdout_do_not_deadlock() -> None:
    data = bytes(range(256)) * 8192  # 2 MiB, more than any pipe buffer
    result = runner.run(Zone.TAINTED_HOST, ["/usr/bin/cat"], stdin=data, timeout=30)
    assert result.stdout == data


def test_stdin_ignored_by_the_command() -> None:
    assert runner.run(Zone.TAINTED_HOST, ["/usr/bin/true"], stdin=b"x" * (1 << 20), timeout=30).returncode == 0


def test_timeout_after_stdout_is_closed() -> None:
    with pytest.raises(runner.CommandError, match="timed out"):
        runner.run(Zone.TAINTED_HOST, ["/bin/sh", "-c", "exec >&- 2>&-; sleep 5"], timeout=0.3)


def test_stderr_keeps_the_tail() -> None:
    script = "printf start >&2; head -c 10000 /dev/zero | tr '\\0' x >&2; printf end >&2"
    result = runner.run(Zone.TAINTED_HOST, ["/bin/sh", "-c", script])
    assert len(result.stderr) == runner.STDERR_EXCERPT_BYTES
    assert result.stderr.endswith(b"xend")
