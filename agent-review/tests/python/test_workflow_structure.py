"""The structure checks of workflow_structure.py on crafted inputs.

The call-site rules are tested on a small frozen workflow (``fixture``) with
the same jobs, roots and call lines as agent-review.yml, which every script
calls as a Python entry point; each case breaks it in one way. Only the
"real ... passes" tests read the live tree.
"""

import copy
import pathlib
import subprocess
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
# The call of each entry point, as the fixture and agent-review.yml write it.
CALLS = {
    "authorize": f'{PY}{TRUSTED}authorize"',
    "run-review": f"-- {PY}{OPT}run-review --phase fetch",
    "collect-result": f"{PY}{OPT}collect-result",
    "publish-comment": f'{PY}{TRUSTED}publish-comment" render',
}


def fixture() -> dict[str, Any]:
    """agent-review.yml's call sites: every entry point called in the Python form."""
    fetch = {"name": "Fetch trusted files", "run": f'[ -f {TRUSTED}run-review" ] || exit 1\n'}
    review_steps = [
        fetch,
        {"name": "Fetch the caller configuration", "run": "true\n"},
        {"name": "Prepare the sandbox inputs", "run": 'sudo cp -R "${RUNNER_TEMP}/trusted/." /opt/agent-review/\n'},
        {
            "name": "Fetch the pull request in the sandbox",
            "run": f'sudo -E "${{AWF_BIN}}" {CALLS["run-review"]} "${{WS}}"\n',
        },
        {"name": "Collect the result", "run": f'{CALLS["collect-result"]} "${{WS}}/out" r\n'},
    ]
    return {
        "jobs": {
            "authorize": {"steps": [fetch, {"name": "Authorize", "run": CALLS["authorize"] + "\n"}]},
            "prepare": {"steps": [fetch, {"name": "Install the harness", "run": "true\n"}]},
            "review": {"steps": copy.deepcopy(review_steps)},
            "review-keyless": {"steps": copy.deepcopy(review_steps)},
            "publish": {
                "steps": [
                    fetch,
                    {"name": "Render the review", "run": f"{CALLS['publish-comment']} --review r.md\n"},
                    {"name": "Publish the review", "run": f'url=$({PY}{TRUSTED}publish-comment" post --pr 9)\n'},
                ]
            },
        }
    }


def kinds() -> dict[str, str | None]:
    return dict.fromkeys(ENTRIES, "python")


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


@pytest.mark.parametrize("name", ENTRIES)
def test_fixture_matches_the_real_call_lines(name: str) -> None:
    real = "\n".join(script for _, _, _, script in ws.run_scripts(WORKFLOW))
    assert KINDS[name] == "python"
    assert CALLS[name] in real


def test_fixture_passes() -> None:
    assert problems(fixture(), kinds()) == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # The bash-era call forms, each now a failure.
        (CALLS["authorize"], f'bash {TRUSTED}authorize"'),
        (CALLS["run-review"], f"-- {OPT}run-review --phase fetch"),
        (CALLS["collect-result"], f"/usr/bin/bash {OPT}collect-result"),
        (
            f"{CALLS['publish-comment']} --review r.md\n",
            f'pc={TRUSTED}publish-comment"\n{PY}"${{pc}}" render --review r.md\n',
        ),
    ],
)
def test_bash_era_call_forms_fail(old: str, new: str) -> None:
    workflow = fixture()
    assert edit_runs(workflow, old, new)
    found = ws.call_site_problems(workflow, kinds())
    assert found
    assert all("is Python; call it exactly as" in p for p in found)


def test_wrong_root_fails() -> None:
    workflow = fixture()
    assert edit_runs(workflow, f'{TRUSTED}authorize"', f"{OPT}authorize")
    assert edit_runs(workflow, f"{OPT}collect-result", f'{TRUSTED}collect-result"')
    found = ws.call_site_problems(workflow, kinds())
    assert any("authorize/Authorize: '/opt/agent-review/bin/authorize'" in p for p in found)
    assert any("review/Collect the result: '${RUNNER_TEMP}/trusted/bin/collect-result'" in p for p in found)


def test_trusted_root_in_review_jobs_only_before_the_copy() -> None:
    workflow = fixture()
    assert edit_runs(workflow, f"{PY}{OPT}run-review", f'{PY}{TRUSTED}run-review"')
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
        f'"${{HOME}}"{PY}{TRUSTED}authorize"',
        f'x{PY}{TRUSTED}authorize"',
    ],
)
def test_other_call_forms_fail(call: str) -> None:
    workflow = fixture()
    assert edit_runs(workflow, CALLS["authorize"], call)
    assert problems(workflow, kinds())


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
    workflow["jobs"]["extra"] = {"steps": [{"name": "x", "run": CALLS["authorize"] + "\n"}]}
    workflow["jobs"]["prepare"]["steps"].append({"name": "y", "run": f'{PY}{TRUSTED}nope"\n'})
    found = ws.call_site_problems(workflow, kinds())
    assert any("no allowed root for job 'extra'" in p for p in found)
    assert any("bin/nope is not a known entry point" in p for p in found)


def test_a_non_template_file_is_not_an_entry_point() -> None:
    found = ws.call_site_problems(fixture(), {**kinds(), "authorize": None})
    assert any("bin/authorize is not a known entry point" in p for p in found)


ROOT_LINE = 'ar_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)'
GOOD_CALL = '/usr/bin/python3 -I -S -B "${ar_root}/bin/probe"'


def shell(*lines: str) -> dict[str, str]:
    return {"harnesses/x/run.sh": "#!/usr/bin/env bash\n" + "".join(f"{line}\n" for line in lines)}


def test_shell_files() -> None:
    kinds = {"probe": "python", "run-review": "bash"}
    assert ws.shell_python_problems(shell("# python in a comment", ROOT_LINE, GOOD_CALL), kinds) == []
    assert ws.shell_python_problems(shell(ROOT_LINE, f"out=$({GOOD_CALL})", f"x && {GOOD_CALL}"), kinds) == []
    assert ws.ar_root_line("bin/run-review") == 'ar_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)'
    for line in [
        'python3 "${ar_root}/bin/probe"',
        '/usr/bin/python3 -I -S -B "${ar_root}/bin/run-review"',
        "/usr/bin/python3 -I -S -B -c 'import os'",
        '/usr/bin/python3 -I -S -B "${GITHUB_WORKSPACE}/bin/probe"',
        '/usr/bin/python3 -I -S -B "${root}/bin/probe"',
        '"${HOME}"/usr/bin/python3 -I -S -B "${ar_root}/bin/probe"',
        "exec python3",
    ]:
        assert ws.shell_python_problems(shell(ROOT_LINE, line), kinds), line


def test_shell_root_must_be_set_from_the_files_path() -> None:
    kinds = {"probe": "python"}
    assert any("does not set" in p for p in ws.shell_python_problems(shell(GOOD_CALL), kinds))
    for bad in ["ar_root=/tmp/evil", "export ar_root=${HOME}", 'ar_root=$(cd "$(dirname "$0")/../.." && pwd)']:
        found = ws.shell_python_problems(shell(ROOT_LINE, bad, GOOD_CALL), kinds)
        assert any("set 'ar_root' only as" in p for p in found), bad


def test_python_shebang_in_a_shell_file_fails() -> None:
    sources = {"harnesses/x/helper.sh": "#!/usr/bin/env python3\nprint(1)\n"}
    assert ws.shell_python_problems(sources, {})


def test_shebang_problems() -> None:
    lines = {
        "agent-review/harnesses/x/helper": "#!/usr/bin/env python3",
        "agent-review/bin/tool": "#!/usr/bin/python3 -I",
        "agent-review/harnesses/x/run.sh": "#!/usr/bin/env bash",
        "agent-review/tests/tool": "#!/usr/bin/env python3",
        "agent-review/lib/agent_review/x.py": "#!/usr/bin/python3",
        "agent-review/prompt.md.tmpl": "# python",
    }
    assert ws.shebang_problems(lines) == [
        "agent-review/bin/tool: a Python shebang ('#!/usr/bin/python3 -I'); use an entry point in bin/",
        "agent-review/harnesses/x/helper: a Python shebang ('#!/usr/bin/env python3'); use an entry point in bin/",
    ]


def test_real_shebangs_pass() -> None:
    assert ws.shebang_problems(REPO["first_lines"]) == []


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


NOT_TEMPLATE = "not the exact Python entry-point template"


@pytest.mark.parametrize(
    ("text", "mode", "expected"),
    [
        (ws.render_entry("probe"), "100755", "mode 100644"),
        (ws.render_entry("probe") + "print('extra')\n", "100644", NOT_TEMPLATE),
        (
            ws.render_entry("probe").replace("sys.flags.safe_path", "True"),
            "100644",
            "not the exact Python entry-point template",
        ),
        (ws.render_entry("other"), "100644", NOT_TEMPLATE),
        ("#!/usr/bin/python3 -ISB\nimport sys\n", "100755", NOT_TEMPLATE),
        ("", "100644", NOT_TEMPLATE),
        ("#!/usr/bin/env bash\necho hi\n", "100755", NOT_TEMPLATE),
        ("#!/usr/bin/bash\necho hi\n", "100644", NOT_TEMPLATE),
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
        ("100644", "agent-review/harnesses/x/helper.py"),
        ("100644", "agent-review/bin/tool.py"),
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
        "run(['/usr/bin/gh', 'api'])\n",
        "from agent_review import runner\nrunner.run(runner.Zone.TRUSTED_HOST, ['x'])\n",
        "from agent_review.runner import subprocess\n",
        "from agent_review.runner import os\n",
        "from agent_review import runner\nrunner.subprocess.run(['x'])\n",
        "from agent_review import runner\nrunner.os.system('id')\n",
        "import os\ngetattr(os, 'system')('id')\n",
        "import os as _\n",
        "import sys\nsys.modules['os']\n",
        "globals()['__builtins__']\n",
        "vars()\n",
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
    source = (
        "import re\nimport os.path\nimport pathlib\nimport sys\nfrom os import fspath\n"
        "re.compile('x')\nos.fspath('x')\ngetattr(sys.flags, 'isolated')\nsys.executable\nos.execute = 1\n"
    )
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


# --- Sandbox output -----------------------------------------------------------------

COMMON_SH = REPO["shell"]["harnesses/common.sh"]


def test_real_sandbox_output_filters_pass() -> None:
    assert ws.sandbox_output_problems(WORKFLOW, COMMON_SH) == []


@pytest.mark.parametrize(
    "old",
    [" -e 's/##\\[/#\\xe2\\x80\\x8b#[/g'", " -e 's/^/awf| /'", " -e 's/\\r//g'"],
)
def test_awf_output_filter_is_required(old: str) -> None:
    workflow = copy.deepcopy(WORKFLOW)
    # The review jobs share some steps through YAML aliases; all four awf calls must fail.
    assert edit_runs(workflow, ws.AWF_OUTPUT_FILTER, ws.AWF_OUTPUT_FILTER.replace(old, ""))
    assert len(ws.sandbox_output_problems(workflow, COMMON_SH)) == 4


def test_diag_filter_is_required() -> None:
    weakened = COMMON_SH.replace("-e 's/##\\[/#\\xe2\\x80\\x8b#[/g' ", "")
    assert weakened != COMMON_SH
    assert ws.sandbox_output_problems(WORKFLOW, weakened)


def test_diag_breaks_legacy_commands(tmp_path: pathlib.Path) -> None:
    script = tmp_path / "t.sh"
    script.write_text(
        f'. "{ws.AR_DIR}/harnesses/common.sh"\nprintf "a ##[warning]x\\n::error::y\\n" | ADAPTER_NAME=t diag\n'
    )
    result = subprocess.run(["/usr/bin/bash", str(script)], capture_output=True, timeout=30, check=True)
    assert result.stderr == "t| a #\u200b#[warning]x\nt| ::error::y\n".encode()


# --- The collect gate -------------------------------------------------------------

GATE = "always() && steps.key.outputs.skipped != 'true' && steps.teardown.outcome == 'success'"


def gate_workflow(condition: object, teardown_id: str = "teardown") -> dict[str, Any]:
    collect: dict[str, object] = {"name": "Collect the result", "run": "true\n"}
    if condition is not None:
        collect["if"] = condition
    steps = [{"name": "Stop the sandbox", "id": teardown_id, "run": "true\n"}, collect]
    return {"jobs": {"review": {"steps": steps}, "review-keyless": {"steps": copy.deepcopy(steps)}}}


def test_real_collect_gate_passes() -> None:
    assert ws.collect_gate_problems(WORKFLOW) == []
    assert (
        sum(
            step.get("name") == "Collect the result"
            for job in WORKFLOW["jobs"].values()
            for step in job.get("steps", [])
        )
        == 2
    )


@pytest.mark.parametrize(
    "condition",
    [
        GATE,
        f"${{{{ {GATE} }}}}",
        "steps.teardown.outcome == 'success'",
        "always() && steps.teardown.outcome == 'success'",
    ],
)
def test_collect_gate_accepts(condition: str) -> None:
    assert ws.collect_gate_problems(gate_workflow(condition)) == []


@pytest.mark.parametrize(
    "condition",
    [
        None,
        "always()",
        "always() && steps.key.outputs.skipped != 'true'",
        "always() && steps.teardown.outcome != 'success'",
        "always() && steps.teardown.outcome == 'failure'",
        f"{GATE} || always()",
        "always() || steps.teardown.outcome == 'success'",
        "always() && !(steps.key.outputs.skipped == 'true' && steps.teardown.outcome == 'success')",
        "always() && (steps.teardown.outcome == 'success')",
        "always() && steps.teardown.conclusion == 'success'",
    ],
)
def test_collect_gate_rejects(condition: str | None) -> None:
    assert len(ws.collect_gate_problems(gate_workflow(condition))) == 2


def test_collect_gate_needs_the_teardown_step() -> None:
    found = ws.collect_gate_problems(gate_workflow(GATE, teardown_id="stop"))
    assert len(found) == 2
    assert all("no earlier step with id 'teardown'" in p for p in found)


def test_collect_gate_needs_a_collect_step() -> None:
    assert ws.collect_gate_problems({"jobs": {"review": {"steps": []}}}) == ["no 'Collect the result' step found"]


def test_collect_gate_in_the_real_workflow_is_required() -> None:
    workflow = copy.deepcopy(WORKFLOW)
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == "Collect the result":
                step["if"] = step["if"].replace(" && steps.teardown.outcome == 'success'", "")
    assert len(ws.collect_gate_problems(workflow)) == 2
