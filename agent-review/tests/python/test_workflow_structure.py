"""The structure checks of workflow_structure.py on crafted inputs.

The real workflow calls no Python entry point yet, so these cases are the
evidence that the Python branch of each rule works: each bash script
flipped to Python passes with the swapped call line and fails without it.
"""

import copy
from typing import Any

import pytest

import workflow_structure as ws

WORKFLOW: dict[str, Any] = ws.load(ws.REVIEW_WORKFLOW)
REPO = ws.repository()
KINDS: dict[str, str | None] = REPO["kinds"]

# The call lines of agent-review.yml, and their Python form, per entry point.
SWAPS = {
    "authorize": [
        (
            'bash "${RUNNER_TEMP}/trusted/bin/authorize"',
            '/usr/bin/python3 -I -S -B "${RUNNER_TEMP}/trusted/bin/authorize"',
        )
    ],
    "run-review": [
        ("-- /opt/agent-review/bin/run-review", "-- /usr/bin/python3 -I -S -B /opt/agent-review/bin/run-review")
    ],
    "collect-result": [
        (
            "/usr/bin/bash /opt/agent-review/bin/collect-result",
            "/usr/bin/python3 -I -S -B /opt/agent-review/bin/collect-result",
        )
    ],
    "publish-comment": [
        ('pc="${RUNNER_TEMP}/trusted/bin/publish-comment"\n', ""),
        ('bash "${pc}"', '/usr/bin/python3 -I -S -B "${RUNNER_TEMP}/trusted/bin/publish-comment"'),
        (
            'bash "${RUNNER_TEMP}/trusted/bin/publish-comment"',
            '/usr/bin/python3 -I -S -B "${RUNNER_TEMP}/trusted/bin/publish-comment"',
        ),
    ],
}


def edit_runs(workflow: dict[str, Any], old: str, new: str) -> int:
    """Replaces ``old`` in every 'run:' script; returns the number of scripts changed."""
    changed = 0
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if old in step.get("run", ""):
                step["run"] = step["run"].replace(old, new)
                changed += 1
    return changed


def swapped(names: list[str]) -> dict[str, Any]:
    workflow = copy.deepcopy(WORKFLOW)
    for name in names:
        for old, new in SWAPS[name]:
            assert edit_runs(workflow, old, new), (name, old)
    return workflow


def flipped(names: list[str]) -> dict[str, str | None]:
    return {**KINDS, **dict.fromkeys(names, "python")}


def test_real_workflow_passes() -> None:
    assert ws.call_site_problems(WORKFLOW, KINDS) == []
    assert ws.python_word_problems(WORKFLOW, KINDS) == []


@pytest.mark.parametrize("name", sorted(SWAPS))
def test_each_port_passes_with_the_python_call(name: str) -> None:
    workflow = swapped([name])
    assert ws.call_site_problems(workflow, flipped([name])) == []
    assert ws.python_word_problems(workflow, flipped([name])) == []


def test_all_ports_together_pass() -> None:
    workflow = swapped(sorted(SWAPS))
    assert ws.call_site_problems(workflow, flipped(sorted(SWAPS))) == []
    assert ws.python_word_problems(workflow, flipped(sorted(SWAPS))) == []


@pytest.mark.parametrize("name", sorted(SWAPS))
def test_a_ported_file_with_the_old_call_fails(name: str) -> None:
    problems = ws.call_site_problems(WORKFLOW, flipped([name]))
    assert problems
    assert all(f"bin/{name} is Python" in p for p in problems)


@pytest.mark.parametrize("name", sorted(SWAPS))
def test_a_bash_file_with_the_python_call_fails(name: str) -> None:
    problems = ws.call_site_problems(swapped([name]), KINDS) + ws.python_word_problems(swapped([name]), KINDS)
    assert problems


def test_wrong_root_fails() -> None:
    kinds = flipped(["authorize", "collect-result"])
    workflow = swapped(["authorize", "collect-result"])
    edit_runs(workflow, '"${RUNNER_TEMP}/trusted/bin/authorize"', "/opt/agent-review/bin/authorize")
    edit_runs(workflow, "/opt/agent-review/bin/collect-result", '"${RUNNER_TEMP}/trusted/bin/collect-result"')
    problems = ws.call_site_problems(workflow, kinds)
    assert any("authorize/Authorize: '/opt/agent-review/bin/authorize'" in p for p in problems)
    assert any("review/Collect the result: '${RUNNER_TEMP}/trusted/bin/collect-result'" in p for p in problems)


def test_trusted_root_in_review_jobs_only_before_the_copy() -> None:
    workflow = copy.deepcopy(WORKFLOW)
    edit_runs(workflow, "-- /opt/agent-review/bin/run-review", '-- "${RUNNER_TEMP}/trusted/bin/run-review"')
    problems = ws.call_site_problems(workflow, KINDS)
    assert any("Fetch the pull request in the sandbox" in p and "only '/opt/agent-review'" in p for p in problems)


@pytest.mark.parametrize(
    "call",
    [
        'python3 "${RUNNER_TEMP}/trusted/bin/authorize"',
        '/usr/bin/python3 -I -B "${RUNNER_TEMP}/trusted/bin/authorize"',
        '/usr/bin/python3 -I -S -B -c "${RUNNER_TEMP}/trusted/bin/authorize"',
        'p="${RUNNER_TEMP}/trusted/bin/authorize"; /usr/bin/python3 -I -S -B "$p"',
        '/usr/bin/python3 -ISB "${RUNNER_TEMP}/trusted/bin/authorize"',
        '"${RUNNER_TEMP}/trusted/bin/authorize"',
    ],
)
def test_other_python_call_forms_fail(call: str) -> None:
    workflow = copy.deepcopy(WORKFLOW)
    edit_runs(workflow, 'bash "${RUNNER_TEMP}/trusted/bin/authorize"', call)
    kinds = flipped(["authorize"])
    assert ws.call_site_problems(workflow, kinds) + ws.python_word_problems(workflow, kinds)


@pytest.mark.parametrize(
    "script",
    ["python3 -c 'print(1)'", "/usr/bin/python3 -m pip install x", "pip install pytest", "pipx run x", "python -V"],
)
def test_python_and_pip_elsewhere_in_the_workflow_fail(script: str) -> None:
    workflow = copy.deepcopy(WORKFLOW)
    workflow["jobs"]["prepare"]["steps"].append({"name": "x", "run": script})
    assert ws.python_word_problems(workflow, KINDS)


def test_setup_python_fails() -> None:
    workflow = copy.deepcopy(WORKFLOW)
    workflow["jobs"]["publish"]["steps"].insert(0, {"uses": "actions/setup-python@0123456789abcdef"})
    assert ws.python_word_problems(workflow, KINDS)


def test_unknown_entry_point_and_job_fail() -> None:
    workflow = copy.deepcopy(WORKFLOW)
    workflow["jobs"]["extra"] = {"steps": [{"name": "x", "run": 'bash "${RUNNER_TEMP}/trusted/bin/authorize"'}]}
    workflow["jobs"]["prepare"]["steps"].append({"name": "y", "run": 'bash "${RUNNER_TEMP}/trusted/bin/nope"'})
    problems = ws.call_site_problems(workflow, KINDS)
    assert any("no allowed root for job 'extra'" in p for p in problems)
    assert any("bin/nope is not a known entry point" in p for p in problems)


def test_shell_files() -> None:
    kinds = {"probe": "python", "run-review": "bash"}
    good = {"harnesses/x/run.sh": '# python here is a comment\n/usr/bin/python3 -I -S -B "${root}/bin/probe"\n'}
    assert ws.shell_python_problems(good, kinds) == []
    for line in [
        'python3 "${root}/bin/probe"',
        '/usr/bin/python3 -I -S -B "${root}/bin/run-review"',
        "/usr/bin/python3 -I -S -B -c 'import os'",
        "pip install x",
        "exec python3",
    ]:
        problems = ws.shell_python_problems({"harnesses/x/run.sh": line + "\n"}, kinds)
        if "pip" in line and "python" not in line:
            continue  # pip in a shell file is not a Python call; the AST and tree rules cover the rest
        assert problems, line


def test_real_shell_files_pass() -> None:
    assert "harnesses/stub/run.sh" in REPO["shell"]
    assert "harnesses/stub/install.sh" in REPO["shell"]
    assert ws.shell_python_problems(REPO["shell"], KINDS) == []


# --- Entry points and tree -------------------------------------------------------

LIB_WITH_MAIN = {"lib/agent_review/probe.py": "def main() -> int:\n    return 0\n"}


def test_entry_template() -> None:
    text = ws.render_entry("probe")
    assert ws.entry_kind("probe", text) == "python"
    assert ws.entry_problems({"probe": (text, "100644")}, LIB_WITH_MAIN) == []
    assert ws.render_entry("publish-comment").count("from agent_review.publish_comment import main") == 1


def test_real_entry_points_pass() -> None:
    assert ws.entry_problems(REPO["entries"], REPO["lib"]) == []
    assert REPO["kinds"]["probe"] == "python"


@pytest.mark.parametrize(
    ("text", "mode", "expected"),
    [
        (ws.render_entry("probe"), "100755", "mode 100644"),
        (ws.render_entry("probe") + "print('extra')\n", "100644", "neither"),
        (ws.render_entry("probe").replace("sys.flags.safe_path", "True"), "100644", "neither"),
        (ws.render_entry("other"), "100644", "neither"),
        ("#!/usr/bin/python3 -ISB\nimport sys\n", "100755", "neither"),
        ("", "100644", "neither"),
    ],
)
def test_entry_problems(text: str, mode: str, expected: str) -> None:
    problems = ws.entry_problems({"probe": (text, mode)}, LIB_WITH_MAIN)
    assert any(expected in p for p in problems), problems


def test_entry_needs_its_module_and_main() -> None:
    entries = {"probe": (ws.render_entry("probe"), "100644")}
    assert any("no lib/agent_review/probe.py" in p for p in ws.entry_problems(entries, {}))
    no_main = {"lib/agent_review/probe.py": "def run() -> int:\n    return 0\n"}
    assert any("defines no main()" in p for p in ws.entry_problems(entries, no_main))


def test_real_tree_passes() -> None:
    assert ws.tree_problems(REPO["index"]) == []


@pytest.mark.parametrize(
    ("mode", "path"),
    [
        ("120000", "agent-review/harnesses/stub/link"),
        ("160000", "agent-review/vendor"),
        ("100644", "agent-review/lib/agent_review/__pycache__/x.cpython-312.pyc"),
        ("100644", "agent-review/bin/x.pyc"),
        ("100644", "agent-review/lib/agent_review/x.pth"),
        ("100644", "agent-review/tests/sitecustomize.py"),
        ("100644", "agent-review/usercustomize.py"),
        ("100644", "agent-review/lib/other.py"),
        ("100644", "agent-review/lib/agent_review/_speedups.so"),
        ("100644", "agent-review/lib/agent_review/data.json"),
    ],
)
def test_tree_problems(mode: str, path: str) -> None:
    assert ws.tree_problems([(mode, path)])


# --- AST and token reach ---------------------------------------------------------


def test_real_python_passes() -> None:
    assert ws.ast_problems(REPO["python"]) == []
    assert ws.token_reach_problems(REPO["python"]) == []


@pytest.mark.parametrize(
    "source",
    [
        "import pickle\n",
        "import importlib\n",
        "from importlib import import_module\n",
        "import ctypes\n",
        "import marshal\n",
        "import urllib.request\n",
        "import fnmatch\n",
        "import yaml\n",
        "import subprocess\n",
        "from subprocess import run\n",
        "eval('1')\n",
        "exec('x = 1')\n",
        "compile('1', 'f', 'eval')\n",
        "__import__('os')\n",
        "breakpoint()\n",
        "import os\nos.system('id')\n",
        "import os\nos.popen('id')\n",
        "import os\nos.execv('/bin/sh', [])\n",
        "import os\nos.posix_spawn('/bin/sh', [], {})\n",
        "import os\nos.fork()\n",
        "from os import system\n",
        "from . import runner\n",
        "f(shell=True)\n",
        "f(shell=False)\n",
        "run(['gh', 'api'])\n",
    ],
)
def test_ast_problems(source: str) -> None:
    assert ws.ast_problems({"lib/agent_review/x.py": source})


def test_subprocess_and_gh_only_in_their_modules() -> None:
    assert ws.ast_problems({ws.RUNNER_MODULE: "import subprocess\n"}) == []
    assert ws.ast_problems({ws.GH_MODULE: "argv = ['gh', 'api']\n"}) == []
    assert ws.ast_problems({ws.RUNNER_MODULE: "subprocess.run(['x'], shell=False)\n"})
    assert ws.ast_problems({"bin/probe": "import subprocess\n"})


def test_re_compile_is_allowed() -> None:
    assert ws.ast_problems({"lib/agent_review/x.py": "import re\nre.compile('x')\n"}) == []


def entry(module: str) -> str:
    return ws.render_entry(module.replace("_", "-"))


def test_token_reach() -> None:
    lib = {
        "lib/agent_review/__init__.py": "",
        "lib/agent_review/gh.py": "from agent_review import runner\n",
        "lib/agent_review/runner.py": "",
        "lib/agent_review/helper.py": "import agent_review.gh\n",
        "lib/agent_review/authorize.py": "from agent_review import gh\n",
        "lib/agent_review/collect_result.py": "from agent_review import runner\n",
        "lib/agent_review/run_review.py": "from agent_review.helper import x\n",
        "lib/agent_review/probe.py": "import agent_review\n",
    }
    sources = {
        **lib,
        "bin/authorize": entry("authorize"),
        "bin/collect-result": entry("collect_result"),
        "bin/run-review": entry("run_review"),
        "bin/probe": entry("probe"),
    }
    problems = ws.token_reach_problems(sources)
    assert problems == [
        "bin/run-review: reaches agent_review.gh (the token); only ['authorize', 'publish-comment'] may"
    ]
    # Through the package's __init__ too.
    sources["lib/agent_review/__init__.py"] = "from agent_review import gh\n"
    assert any(p.startswith("bin/probe:") for p in ws.token_reach_problems(sources))


def test_probe_does_not_reach_gh() -> None:
    modules = {m: t for rel, t in REPO["python"].items() if (m := ws._module_name(rel))}
    closure = ws.import_closure(REPO["python"]["bin/probe"], modules)
    assert "agent_review.probe" in closure
    assert "agent_review.runner" in closure
    assert "agent_review.gh" not in closure
