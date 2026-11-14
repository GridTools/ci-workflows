"""The Python entry points under the production command line, in a hostile environment.

pytest imports agent_review without '-I -S', so these tests run each entry
point as a subprocess, exactly as the workflow does
('/usr/bin/python3 -I -S -B <root>/bin/<name>'), with PYTHONPATH,
PYTHONSTARTUP, PYTHONHOME and PYTHONUSERBASE pointing at planted code and
from a working directory holding 'json.py', 'agent_review/' and
'sitecustomize.py'. Every planted module leaves a marker file if imported.
The entry points are found from the files (workflow_structure.entry_kind),
not from a list.
"""

import os
import pathlib
import re
import shutil
import subprocess

import pytest

from workflow_structure import entry_kind

AR_DIR = pathlib.Path(__file__).resolve().parents[2]
BIN_DIR = AR_DIR / "bin"
PYTHON_CALL = ["/usr/bin/python3", "-I", "-S", "-B"]
PYTHON_ENTRIES = sorted(
    p.name for p in BIN_DIR.iterdir() if entry_kind(p.name, p.read_text(encoding="utf-8")) == "python"
)
PLANTED = [
    "json.py",
    "os.py",
    "re.py",
    "sitecustomize.py",
    "usercustomize.py",
    "agent_review/__init__.py",
    "agent_review/probe.py",
    "encodings/__init__.py",
]
PROBE_LINE = re.compile(
    r"agent-review-probe python=3\.[0-9]+\.[0-9]+ executable=/usr/bin/python3 isolated=1 ignore_environment=1"
    r" no_user_site=1 no_site=1 dont_write_bytecode=1 safe_path=1 site=0 stdlib_only=1 lib=(?P<lib>/\S+)\n",
    re.ASCII,
)


def plant(directory: pathlib.Path, markers: pathlib.Path) -> None:
    """Modules that write a marker named after themselves when imported."""
    for rel in PLANTED:
        path = directory / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tag = f"{directory.name}-{rel.replace('/', '-')}"
        path.write_text(f"open({str(markers / tag)!r}, 'w').close()\nmain = lambda: 0\n")


@pytest.fixture
def hostile(tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict[str, str], pathlib.Path]:
    """(working directory, environment, marker directory)."""
    markers = tmp_path / "markers"
    markers.mkdir()
    cwd, pythonpath, home, userbase = (tmp_path / n for n in ("cwd", "pythonpath", "home", "userbase"))
    for directory in (cwd, pythonpath):
        plant(directory, markers)
    plant(home / "lib" / "python3.12", markers)
    plant(userbase / "lib" / "python3.12" / "site-packages", markers)
    (pythonpath / "startup.py").write_text(f"open({str(markers / 'startup')!r}, 'w').close()\n")
    env = {
        **os.environ,
        "PYTHONPATH": f"{pythonpath}:{cwd}",
        "PYTHONSTARTUP": str(pythonpath / "startup.py"),
        "PYTHONHOME": str(home),
        "PYTHONUSERBASE": str(userbase),
        "PYTHONINSPECT": "1",
        "PYTHONSAFEPATH": "",
    }
    return cwd, env, markers


def test_there_is_a_python_entry_point() -> None:
    assert "probe" in PYTHON_ENTRIES


@pytest.mark.parametrize("name", PYTHON_ENTRIES)
def test_hostile_environment_loads_nothing(
    name: str, hostile: tuple[pathlib.Path, dict[str, str], pathlib.Path]
) -> None:
    cwd, env, markers = hostile
    result = subprocess.run(
        [*PYTHON_CALL, str(BIN_DIR / name)], cwd=cwd, env=env, capture_output=True, timeout=60, check=False
    )
    # The entry point got past its flag guard, so the test is not vacuous.
    assert b"run only as" not in result.stderr
    assert sorted(p.name for p in markers.iterdir()) == []


def test_probe_in_a_hostile_environment(hostile: tuple[pathlib.Path, dict[str, str], pathlib.Path]) -> None:
    cwd, env, markers = hostile
    result = subprocess.run(
        [*PYTHON_CALL, str(BIN_DIR / "probe")], cwd=cwd, env=env, capture_output=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    m = PROBE_LINE.fullmatch(result.stdout.decode())
    assert m, result.stdout
    assert m.group("lib") == str((AR_DIR / "lib").resolve())
    assert not list(markers.iterdir())


def test_negative_control_the_plants_work(hostile: tuple[pathlib.Path, dict[str, str], pathlib.Path]) -> None:
    """Without '-I -S' the same plants are imported: the test above can fail."""
    cwd, env, markers = hostile
    # Each of these would stop the interpreter from starting at all.
    del env["PYTHONHOME"]
    del env["PYTHONINSPECT"]
    for directory in env["PYTHONPATH"].split(":"):
        shutil.rmtree(pathlib.Path(directory) / "encodings")
    result = subprocess.run(
        ["/usr/bin/python3", "-B", str(BIN_DIR / "probe")],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    fired = {p.name for p in markers.iterdir()}
    assert "pythonpath-sitecustomize.py" in fired
    assert "pythonpath-json.py" not in fired  # the guard stopped it first
    assert result.returncode == 2
    assert b"run only as '/usr/bin/python3 -I -S -B <root>/bin/<name>'" in result.stderr


@pytest.mark.parametrize("name", PYTHON_ENTRIES)
@pytest.mark.parametrize(
    "flags",
    [
        [],  # nothing
        ["-S", "-B"],  # not isolated
        ["-I", "-B"],  # site imported
        ["-I", "-S"],  # bytecode written
        ["-E", "-s", "-S", "-B"],  # not isolated, no safe path
        ["-E", "-s", "-P", "-S", "-B"],  # all but '-I' itself
    ],
)
def test_flag_guard(name: str, flags: list[str], tmp_path: pathlib.Path) -> None:
    result = subprocess.run(
        ["/usr/bin/python3", *flags, str(BIN_DIR / name)], cwd=tmp_path, capture_output=True, timeout=60, check=False
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"run only as" in result.stderr
    assert not list(tmp_path.iterdir())


def test_symlinked_entry_point_finds_its_real_lib(tmp_path: pathlib.Path) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "probe").symlink_to(BIN_DIR / "probe")
    result = subprocess.run(
        [*PYTHON_CALL, str(tmp_path / "bin" / "probe")], capture_output=True, timeout=60, check=False
    )
    m = PROBE_LINE.fullmatch(result.stdout.decode())
    assert m and m.group("lib") == str((AR_DIR / "lib").resolve())


def launcher(script: str, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Runs ``script`` in bash with tests/lib.sh sourced."""
    return subprocess.run(
        ["/usr/bin/bash", "-c", f'. "$1"; shift; {script}', "bash", str(AR_DIR / "tests" / "lib.sh"), *args],
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_launcher_runs_python_entry_points_with_the_production_command_line(tmp_path: pathlib.Path) -> None:
    result = launcher('run_entry "$1"', str(BIN_DIR / "probe"))
    assert result.returncode == 0, result.stderr
    assert PROBE_LINE.fullmatch(result.stdout.decode())
    result = launcher('entry_command "$1" && printf "%s\\n" "${ENTRY_CMD[@]}"', str(BIN_DIR / "probe"))
    assert result.stdout.decode().splitlines() == [*PYTHON_CALL, str(BIN_DIR / "probe")]


@pytest.mark.parametrize("shebang", ["#!/usr/bin/env bash", "#!/usr/bin/bash"])
def test_launcher_runs_bash_scripts_with_bash(shebang: str, tmp_path: pathlib.Path) -> None:
    script = tmp_path / "script"
    script.write_text(f'{shebang}\necho "bash $# $1"\n')
    result = launcher('entry_command "$1" && printf "%s\\n" "${ENTRY_CMD[@]}"', str(script))
    assert result.stdout.decode().splitlines() == ["/usr/bin/bash", str(script)]
    assert launcher('run_entry "$1" "a b"', str(script)).stdout == b"bash 1 a b\n"


def test_launcher_refuses_other_files(tmp_path: pathlib.Path) -> None:
    for text in ["#!/usr/bin/python3\nprint(1)\n", "", "import sys\n"]:
        (tmp_path / "x").write_text(text)
        result = launcher('run_entry "$1"', str(tmp_path / "x"))
        assert result.returncode == 1
        assert result.stdout == b""
