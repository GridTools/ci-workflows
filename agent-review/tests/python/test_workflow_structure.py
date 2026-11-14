"""The structure checks of workflow_structure.py on crafted inputs.

The call-site rules are tested on a small frozen workflow (``fixture``) with
the same jobs, roots and call lines as agent-review.yml, in which any set of
entry points can be ported to Python. So these cases are the evidence that
the Python branch of each rule works, and no port has to edit them; only
the "real ... passes" tests read the live tree, whatever its port state.
"""

import copy
from typing import Any

import pytest

import workflow_structure as ws

WORKFLOW: dict[str, Any] = ws.load(ws.REVIEW_WORKFLOW)
REPO = ws.repository()
KINDS: dict[str, str | None] = REPO["kinds"]

ENTRIES = ("authorize", "collect-result", "publish-comment", "run-review")
TRUSTED = '"${RUNNER_TEMP}/trusted/bin/'
OPT = "/opt/agent-review/bin/"
PY = "/usr/bin/python3 -I -S -B "


def fixture(python: frozenset[str] = frozenset()) -> dict[str, Any]:
    """agent-review.yml's call sites, with the entry points in ``python`` ported."""

    def call(name: str, root: str, bash: str) -> str:
        if name not in python:
            return bash
        return f'{PY}{TRUSTED}{name}"' if root == TRUSTED else f"{PY}{OPT}{name}"

    fetch = {"name": "Fetch trusted files", "run": f'[ -f {TRUSTED}run-review" ] || exit 1\n'}
    review_steps = [
        fetch,
        {"name": "Fetch the caller configuration", "run": "true\n"},
        {"name": "Prepare the sandbox inputs", "run": 'sudo cp -R "${RUNNER_TEMP}/trusted/." /opt/agent-review/\n'},
        {
            "name": "Fetch the pull request in the sandbox",
            "run": f'sudo -E "${{AWF_BIN}}" -- {call("run-review", OPT, OPT + "run-review")} --phase fetch "${{WS}}"\n',
        },
        {
            "name": "Collect the result",
            "run": call("collect-result", OPT, f"/usr/bin/bash {OPT}collect-result")
            + ' "${WS}/out" "${RUNNER_TEMP}/r"\n',
        },
    ]
    if "publish-comment" in python:
        render = f"{call('publish-comment', TRUSTED, '')} render --review r.md\n"
    else:
        render = f'pc={TRUSTED}publish-comment"\nbash "${{pc}}" render --review r.md\n'
    post = call("publish-comment", TRUSTED, f'bash {TRUSTED}publish-comment"')
    return {
        "jobs": {
            "authorize": {
                "steps": [fetch, {"name": "Authorize", "run": call("authorize", TRUSTED, f'bash {TRUSTED}authorize"')}]
            },
            "prepare": {"steps": [fetch, {"name": "Install the harness", "run": "true\n"}]},
            "review": {"steps": copy.deepcopy(review_steps)},
            "review-keyless": {"steps": copy.deepcopy(review_steps)},
            "publish": {
                "steps": [
                    fetch,
                    {"name": "Render the review", "run": render},
                    {"name": "Publish the review", "run": f"url=$({post} post --pr 9)\n"},
                ]
            },
        }
    }


def kinds(python: frozenset[str] = frozenset()) -> dict[str, str | None]:
    return {name: ("python" if name in python else "bash") for name in ENTRIES}


def problems(workflow: dict[str, Any], entry_kinds: dict[str, str | None]) -> list[str]:
    return ws.call_site_problems(workflow, entry_kinds) + ws.python_word_problems(workflow, entry_kinds)


def edit_runs(workflow: dict[str, Any], old: str, new: str) -> int:
    """Replaces ``old`` in every 'run:' script; returns the number of scripts changed."""
    changed = 0
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if old in step.get("run", ""):
                step["run"] = step["run"].replace(old, new)
                changed += 1
    return changed


def test_real_workflow_passes() -> None:
    assert ws.call_site_problems(WORKFLOW, KINDS) == []
    assert ws.python_word_problems(WORKFLOW, KINDS) == []


# The bash call snippets the fixture copies from agent-review.yml; checked
# against the real file only while that script is still bash.
BASH_SNIPPETS = {
    "authorize": [f'bash {TRUSTED}authorize"'],
    "run-review": [f"-- {OPT}run-review --phase fetch", f'[ -f {TRUSTED}run-review" ]'],
    "collect-result": [f"/usr/bin/bash {OPT}collect-result"],
    "publish-comment": [f'pc={TRUSTED}publish-comment"', 'bash "${pc}" render', f'bash {TRUSTED}publish-comment" post'],
}


@pytest.mark.parametrize("name", ENTRIES)
def test_fixture_matches_the_real_call_lines_while_unported(name: str) -> None:
    if KINDS.get(name) != "bash":
        pytest.skip(f"bin/{name} is ported")
    real = "\n".join(script for _, _, _, script in ws.run_scripts(WORKFLOW))
    mine = "\n".join(script for _, _, _, script in ws.run_scripts(fixture()))
    for snippet in BASH_SNIPPETS[name]:
        assert snippet in real, snippet
        assert snippet in mine, snippet


def test_unported_fixture_passes() -> None:
    assert problems(fixture(), kinds()) == []


@pytest.mark.parametrize("name", ENTRIES)
def test_each_port_passes_with_the_python_call(name: str) -> None:
    ported = frozenset({name})
    assert problems(fixture(ported), kinds(ported)) == []


def test_all_ports_together_pass() -> None:
    ported = frozenset(ENTRIES)
    assert problems(fixture(ported), kinds(ported)) == []


@pytest.mark.parametrize("name", ENTRIES)
def test_a_ported_file_with_the_old_call_fails(name: str) -> None:
    found = ws.call_site_problems(fixture(), kinds(frozenset({name})))
    assert found
    assert all(f"bin/{name} is Python" in p for p in found)


@pytest.mark.parametrize("name", ENTRIES)
def test_a_bash_file_with_the_python_call_fails(name: str) -> None:
    assert problems(fixture(frozenset({name})), kinds())


def test_wrong_root_fails() -> None:
    ported = frozenset({"authorize", "collect-result"})
    workflow = fixture(ported)
    assert edit_runs(workflow, f'{TRUSTED}authorize"', f"{OPT}authorize")
    assert edit_runs(workflow, f"{OPT}collect-result", f'{TRUSTED}collect-result"')
    found = ws.call_site_problems(workflow, kinds(ported))
    assert any("authorize/Authorize: '/opt/agent-review/bin/authorize'" in p for p in found)
    assert any("review/Collect the result: '${RUNNER_TEMP}/trusted/bin/collect-result'" in p for p in found)


def test_trusted_root_in_review_jobs_only_before_the_copy() -> None:
    workflow = fixture()
    assert edit_runs(workflow, f"-- {OPT}run-review", f'-- {TRUSTED}run-review"')
    found = ws.call_site_problems(workflow, kinds())
    assert any("Fetch the pull request in the sandbox" in p and "only '/opt/agent-review'" in p for p in found)


@pytest.mark.parametrize(
    "call",
    [
        f'python3 {TRUSTED}authorize"',
        f'/usr/bin/python3 -I -B {TRUSTED}authorize"',
        f'/usr/bin/python3 -I -S -B -c {TRUSTED}authorize"',
        f'p={TRUSTED}authorize"; /usr/bin/python3 -I -S -B "$p"',
        f'/usr/bin/python3 -ISB {TRUSTED}authorize"',
        f'{TRUSTED}authorize"',
        f'bash {TRUSTED}authorize"',
    ],
)
def test_other_python_call_forms_fail(call: str) -> None:
    ported = frozenset({"authorize"})
    workflow = fixture(ported)
    assert edit_runs(workflow, f'{PY}{TRUSTED}authorize"', call)
    assert problems(workflow, kinds(ported))


@pytest.mark.parametrize(
    "script",
    ["python3 -c 'print(1)'", "/usr/bin/python3 -m pip install x", "pip install pytest", "pipx run x", "python -V"],
)
def test_python_and_pip_elsewhere_in_the_workflow_fail(script: str) -> None:
    workflow = fixture()
    workflow["jobs"]["prepare"]["steps"].append({"name": "x", "run": script})
    assert ws.python_word_problems(workflow, kinds())


def test_setup_python_fails() -> None:
    workflow = fixture()
    workflow["jobs"]["publish"]["steps"].insert(0, {"uses": "actions/setup-python@0123456789abcdef"})
    assert ws.python_word_problems(workflow, kinds())


def test_unknown_entry_point_and_job_fail() -> None:
    workflow = fixture()
    workflow["jobs"]["extra"] = {"steps": [{"name": "x", "run": f'bash {TRUSTED}authorize"'}]}
    workflow["jobs"]["prepare"]["steps"].append({"name": "y", "run": f'bash {TRUSTED}nope"'})
    found = ws.call_site_problems(workflow, kinds())
    assert any("no allowed root for job 'extra'" in p for p in found)
    assert any("bin/nope is not a known entry point" in p for p in found)


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
        "import os as o\no.system('id')\n",
        "from os import *\n",
        "from json import *\n",
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


def test_ordinary_code_is_allowed() -> None:
    source = "import re\nimport os.path\nimport pathlib\nfrom os import fspath\nre.compile('x')\nos.fspath('x')\n"
    assert ws.ast_problems({"lib/agent_review/x.py": source}) == []


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
