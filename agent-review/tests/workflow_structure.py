"""Structural checks of the agent review workflow and its Python code.

Run through test-workflow-structure.py (the CLI). The checks are functions
returning a list of problems, so tests/python/test_workflow_structure.py can
feed them crafted inputs as well as the real files.

Workflow (agent-review.yml, read with YAML aliases resolved, as GitHub
does):

- only the keyed 'review' job declares an environment;
- a job without 'environment:' references no secret, except the one boolean
  check in 'authorize' that the key is not a shared secret;
- only 'publish' has a write permission;
- no expression ('${{') inside any 'run:' script, in any workflow file;
- 'review-keyless' runs the same steps as 'review' except the listed ones;
- the keyed job's event backstop, concurrency group and per-call artifact
  names;
- the collect gate: in each review job, 'Collect the result' runs only if
  the earlier 'Stop the sandbox' step (id 'teardown') succeeded. Its 'if:'
  is a plain '&&' chain (no '||', no parentheses except 'always()') with
  that condition as one term. collect-result's single-link check relies on no sandbox
  process being alive.

Python (docs/adr/0003-python-runtime.md):

- call sites: every reference to '<root>/bin/<name>' uses the root allowed
  for its job and step, names an entry point, and is exactly
  '/usr/bin/python3 -I -S -B <root>/bin/<name>' at the start of a command,
  with the file named inline, never through a variable (or an existence
  test, '[ -f "<root>/bin/<name>" ]');
- no other mention of 'python', no 'pip', no 'setup-python' in
  agent-review.yml; in the shell files under bin/ and harnesses/, only the
  Python call form, with the root in 'ar_root' set from the file's path;
  no Python shebang and no '.py' file outside lib/agent_review/ and tests/;
- entry points: each file in bin/ is exactly the Python template (all the
  scripts are ported; a bash script there fails), with mode 100644 (not
  executable);
- the tracked tree under agent-review/: no symlink or submodule, no '.pth',
  '.pyc' or '__pycache__', no 'sitecustomize.py' or 'usercustomize.py';
  lib/ holds only agent_review/, and that only '.py' files;
- imports (AST) of lib/ and the Python entry points: only the allowlisted
  standard-library modules and agent_review.*; no eval/exec/compile/
  __import__/breakpoint, no os.system/os.popen/os.exec*/os.spawn*/fork
  (nor 'import os as ...' or 'import *', which would hide them);
  subprocess only in agent_review/runner.py; no 'shell=' argument anywhere;
- token reach: only the 'authorize' and 'publish-comment' entry points can
  import agent_review.gh; only gh.py names the 'gh' program (by basename)
  and uses the trusted-host zone. The token itself is in a step's env, which
  the workflow decides; this keeps the code that runs there narrow.
- indirect access: no getattr/setattr/delattr on an imported module, no
  globals()/vars()/locals(), no sys.modules (except the probe), no
  standard-library module imported through agent_review.*.
"""

import ast
import pathlib
import re
import subprocess
from collections.abc import Iterator, Mapping
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
AR_DIR = ROOT / "agent-review"
WORKFLOWS = ROOT / ".github" / "workflows"
REVIEW_WORKFLOW = WORKFLOWS / "agent-review.yml"

# The only 'secrets.' reference allowed in a job without an environment.
SHARED_KEY_CHECK = ("authorize", "Authorize", "HAS_SHARED_KEY", "${{ secrets.inference_api_key != '' }}")
# Steps of 'review-keyless' that differ from those of 'review'.
KEYLESS_DIFFERENT = {"Check the inference key": "Check the harness wire"}

# --- Entry points ----------------------------------------------------------

PYTHON_CALL = "/usr/bin/python3 -I -S -B"
ENTRY_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}", re.ASCII)
# Every Python entry point is exactly this text, apart from the module name.
# Keep it in sync with tests/lib.sh (the first line is the marker).
ENTRY_TEMPLATE = """\
# agent-review Python entry point: run only as '/usr/bin/python3 -I -S -B <root>/bin/<name>'.
# The fixed template of agent-review/tests/workflow_structure.py; the code is in lib/agent_review/.
import sys

if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode and sys.flags.safe_path):
    sys.stderr.write("agent-review entry points run only as '/usr/bin/python3 -I -S -B <root>/bin/<name>'.\\n")
    sys.exit(2)

import os  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))

from agent_review.{module} import main  # noqa: E402

sys.exit(main())
"""
PYTHON_MARKER = ENTRY_TEMPLATE.split("\n", 1)[0]


def module_of(name: str) -> str:
    return name.replace("-", "_")


def render_entry(name: str) -> str:
    return ENTRY_TEMPLATE.replace("{module}", module_of(name))


def entry_kind(name: str, text: str) -> str | None:
    """'python' for exactly the template, else None (fail closed; bin/ holds no bash any more)."""
    first = text.split("\n", 1)[0]
    if first == PYTHON_MARKER and text == render_entry(name):
        return "python"
    return None


def entry_problems(entries: Mapping[str, tuple[str, str]], lib_sources: Mapping[str, str]) -> list[str]:
    """``entries``: bin/ name -> (text, git mode); ``lib_sources``: path -> text."""
    problems = []
    for name, (text, mode) in sorted(entries.items()):
        kind = entry_kind(name, text)
        if not ENTRY_NAME.fullmatch(name):
            problems.append(f"bin/{name}: invalid entry point name")
        if kind is None:
            problems.append(f"bin/{name}: not the exact Python entry-point template")
        if kind != "python":
            continue
        if mode != "100644":
            problems.append(f"bin/{name}: a Python entry point must have mode 100644, not {mode}")
        source = lib_sources.get(f"lib/agent_review/{module_of(name)}.py")
        if source is None:
            problems.append(f"bin/{name}: no lib/agent_review/{module_of(name)}.py")
        elif not any(isinstance(node, ast.FunctionDef) and node.name == "main" for node in ast.parse(source).body):
            problems.append(f"bin/{name}: lib/agent_review/{module_of(name)}.py defines no main()")
    return problems


# --- Call sites --------------------------------------------------------------

TRUSTED_ROOT = "${RUNNER_TEMP}/trusted"
OPT_ROOT = "/opt/agent-review"
ROOT_REF = re.compile(r"(\$\{RUNNER_TEMP\}/trusted|/opt/agent-review)/bin/([A-Za-z0-9_.-]+)", re.ASCII)
# The review jobs switch from the fetched files to the root-owned copy here.
OPT_FROM_STEP = "Prepare the sandbox inputs"
TRUSTED_JOBS = ("authorize", "prepare", "publish")
REVIEW_JOBS = ("review", "review-keyless")
# What may precede a reference: the text before it on its line.
EXISTENCE_CHECK = '[ -f "'
PYTHON_FORMS = {TRUSTED_ROOT: f'{PYTHON_CALL} "', OPT_ROOT: f"{PYTHON_CALL} "}
# What must follow a Python reference: the closing quote, or a separator.
PYTHON_AFTER = {TRUSTED_ROOT: re.compile(r'"(?: |$)'), OPT_ROOT: re.compile(r"(?: |$)")}


def python_call(root: str, name: str) -> str:
    """The exact Python call of an entry point, as written in agent-review.yml."""
    quote = '"' if root == TRUSTED_ROOT else ""
    return f"{PYTHON_FORMS[root]}{root}/bin/{name}{quote}"


def run_scripts(workflow: Mapping[str, Any]) -> Iterator[tuple[str, int, str, str]]:
    """Yields (job, step index, step name, run script)."""
    for job_id, job in workflow["jobs"].items():
        for index, step in enumerate(job.get("steps", [])):
            if "run" in step:
                yield job_id, index, step.get("name", str(index)), step["run"]


def allowed_root(workflow: Mapping[str, Any], job_id: str, index: int) -> str | None:
    if job_id in TRUSTED_JOBS:
        return TRUSTED_ROOT
    if job_id in REVIEW_JOBS:
        names = [s.get("name") for s in workflow["jobs"][job_id]["steps"]]
        if OPT_FROM_STEP not in names:
            return None
        return OPT_ROOT if index >= names.index(OPT_FROM_STEP) else TRUSTED_ROOT
    return None


def call_site_problems(workflow: Mapping[str, Any], kinds: Mapping[str, str | None]) -> list[str]:
    """Roots and call forms of every '<root>/bin/<name>' in a 'run:' script."""
    problems = []
    for job_id, index, step_name, script in run_scripts(workflow):
        where = f"{job_id}/{step_name}"
        for line in script.split("\n"):
            for m in ROOT_REF.finditer(line):
                root, name = m.group(1), m.group(2)
                before, after = line[: m.start()], line[m.end() :]
                want_root = allowed_root(workflow, job_id, index)
                if want_root is None:
                    problems.append(f"{where}: no allowed root for job '{job_id}'")
                elif root != want_root:
                    problems.append(f"{where}: '{root}/bin/{name}', but this step may use only '{want_root}'")
                kind = kinds.get(name)
                if kind is None:
                    problems.append(f"{where}: bin/{name} is not a known entry point")
                    continue
                if before.endswith(EXISTENCE_CHECK) and after.startswith('" ]'):
                    continue
                start = len(before) - len(PYTHON_FORMS[root])
                bounded = start == 0 or (start > 0 and CALL_BOUNDARY.fullmatch(before[start - 1]) is not None)
                if not before.endswith(PYTHON_FORMS[root]) or not bounded or PYTHON_AFTER[root].match(after) is None:
                    problems.append(f"{where}: bin/{name} is Python; call it exactly as {python_call(root, name)}")
    return problems


def strings(node: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], str]]:
    """Yields (path, string) for every string value in a YAML tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from strings(value, (*path, str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from strings(value, (*path, str(index)))
    elif isinstance(node, str):
        yield path, node


PYTHON_WORD = re.compile(r"python", re.IGNORECASE)
# What may precede '/usr/bin/python3': a command boundary, never part of a
# word or path ('"${HOME}"/usr/bin/python3').
CALL_BOUNDARY = re.compile(r"[\s(;|&]")
_BOUNDARY = r"(?<![^\s(;|&])"
PIP_WORD = re.compile(r"(?<![A-Za-z0-9_])pip(?:x|[0-9.]*)(?![A-Za-z0-9_])|setup-python|ensurepip", re.IGNORECASE)
YAML_PYTHON_CALL = re.compile(
    _BOUNDARY + re.escape(PYTHON_CALL) + r' (?:"\$\{RUNNER_TEMP\}/trusted|/opt/agent-review)/bin/([a-z][a-z0-9-]*)',
    re.ASCII,
)


def python_word_problems(workflow: Mapping[str, Any], kinds: Mapping[str, str | None]) -> list[str]:
    """No Python in agent-review.yml except the exact call form of a Python entry point."""
    problems = []
    for path, value in strings(workflow):
        where = "/".join(path)
        allowed = {
            m.start() + len("/usr/bin/") for m in YAML_PYTHON_CALL.finditer(value) if kinds.get(m.group(1)) == "python"
        }
        for m in PYTHON_WORD.finditer(value):
            if m.start() not in allowed:
                problems.append(f"{where}: 'python' outside the call form of a Python entry point")
        for m in PIP_WORD.finditer(value):
            problems.append(f"{where}: '{m.group(0)}'")
    return problems


# In shell files the root is the variable 'ar_root', set from the file's own
# path by exactly ar_root_line() (the agent-review/ directory).
SHELL_ROOT_VAR = "ar_root"
SHELL_PYTHON_CALL = re.compile(
    _BOUNDARY + re.escape(PYTHON_CALL) + r' "\$\{' + SHELL_ROOT_VAR + r'\}/bin/([a-z][a-z0-9-]*)"', re.ASCII
)


def ar_root_line(rel: str) -> str:
    """The line that sets the root in the shell file ``rel`` (relative to agent-review/)."""
    ups = "/".join([".."] * (len(rel.split("/")) - 1))
    return f'{SHELL_ROOT_VAR}=$(cd "$(dirname "${{BASH_SOURCE[0]}}")/{ups}" && pwd)'


def shell_python_problems(sources: Mapping[str, str], kinds: Mapping[str, str | None]) -> list[str]:
    """In shell files: Python only as '/usr/bin/python3 -I -S -B "${ar_root}/bin/<python entry>"'.

    Comment lines are skipped, but not the shebang. A file that calls Python
    must set 'ar_root' with exactly ar_root_line(), and nothing else.
    """
    problems = []
    for rel, text in sorted(sources.items()):
        calls = False
        for number, line in enumerate(text.split("\n"), 1):
            if line.lstrip().startswith("#") and not (number == 1 and line.startswith("#!")):
                continue
            allowed = {
                m.start() + len("/usr/bin/")
                for m in SHELL_PYTHON_CALL.finditer(line)
                if kinds.get(m.group(1)) == "python"
            }
            calls = calls or bool(allowed)
            if any(m.start() not in allowed for m in PYTHON_WORD.finditer(line)):
                problems.append(
                    f"{rel}:{number}: Python only as '{PYTHON_CALL} \"${{{SHELL_ROOT_VAR}}}/bin/<entry point>\"'"
                )
            if re.match(rf"\s*(?:export\s+|local\s+|readonly\s+|declare\s+\S+\s+)?{SHELL_ROOT_VAR}\b", line) and (
                line.strip() != ar_root_line(rel)
            ):
                problems.append(f"{rel}:{number}: set '{SHELL_ROOT_VAR}' only as '{ar_root_line(rel)}'")
        if calls and ar_root_line(rel) not in [line.strip() for line in text.split("\n")]:
            problems.append(f"{rel}: calls Python but does not set '{SHELL_ROOT_VAR}' as '{ar_root_line(rel)}'")
    return problems


# --- Sandbox output ------------------------------------------------------------------

# Every line of AWF's output goes through exactly this filter: carriage
# returns dropped, '##[' (the runner's legacy command syntax, matched
# anywhere in a line) broken up with a zero-width space, a prefix so no line
# starts with '::' (invariant 11).
AWF_OUTPUT_FILTER = "| /usr/bin/sed -u -e 's/\\r//g' -e 's/##\\[/#\\xe2\\x80\\x8b#[/g' -e 's/^/awf| /'"
DIAG_FILTER = "sed -e 's/##\\[/#\\xe2\\x80\\x8b#[/g' -e \"s/^/${ADAPTER_NAME:-harness}| /\" >&2"


def sandbox_output_problems(workflow: Mapping[str, Any], common_sh: str) -> list[str]:
    """Every 'awf' call in agent-review.yml, and 'diag' in common.sh, filters its output."""
    problems = []
    awf_steps = 0
    for job_id, _, step_name, script in run_scripts(workflow):
        if '"${AWF_BIN}"' not in script:
            continue
        awf_steps += 1
        lines = script.split("\n")
        for number, line in enumerate(lines):
            if line.strip().startswith("-- ") and not (
                number + 1 < len(lines) and lines[number + 1].strip() == AWF_OUTPUT_FILTER
            ):
                problems.append(f"{job_id}/{step_name}: the awf command is not followed by '{AWF_OUTPUT_FILTER}'")
    if awf_steps == 0:
        problems.append("no awf call found")
    if DIAG_FILTER not in common_sh:
        problems.append(f"harnesses/common.sh: 'diag' does not end with '{DIAG_FILTER}'")
    return problems


# --- The collect gate -------------------------------------------------------------

COLLECT_STEP = "Collect the result"
TEARDOWN_ID = "teardown"
TEARDOWN_SUCCESS = "steps.teardown.outcome == 'success'"


def collect_gate_problems(workflow: Mapping[str, Any]) -> list[str]:
    """'Collect the result' runs only after a successful teardown, in every job that has it."""
    problems = []
    found = 0
    for job_id, job in workflow["jobs"].items():
        steps = job.get("steps", [])
        for index, step in enumerate(steps):
            if step.get("name") != COLLECT_STEP:
                continue
            found += 1
            where = f"{job_id}/{COLLECT_STEP}"
            if not any(s.get("id") == TEARDOWN_ID for s in steps[:index]):
                problems.append(f"{where}: no earlier step with id '{TEARDOWN_ID}'")
            condition = str(step.get("if", "")).strip()
            if condition.startswith("${{") and condition.endswith("}}"):
                condition = condition[3:-2].strip()
            terms = [term.strip() for term in condition.split("&&")]
            # Only 'always()' may hold parentheses: no grouping, so no '!(...)'.
            plain = "||" not in condition and all(t == "always()" or not set(t) & set("()") for t in terms)
            if not plain:
                problems.append(f"{where}: 'if:' is not a plain '&&' chain: {condition!r}")
            elif TEARDOWN_SUCCESS not in terms:
                problems.append(f"{where}: 'if:' does not require {TEARDOWN_SUCCESS!r}: {condition!r}")
    if found == 0:
        problems.append(f"no '{COLLECT_STEP}' step found")
    return problems


# --- Tree ----------------------------------------------------------------------

FORBIDDEN_NAMES = ("sitecustomize.py", "usercustomize.py", "__pycache__")
FORBIDDEN_SUFFIXES = (".pth", ".pyc", ".pyo")


def tree_problems(index: list[tuple[str, str]]) -> list[str]:
    """``index``: (git mode, path) of every tracked file under agent-review/."""
    problems = []
    for mode, path in index:
        parts = path.split("/")
        if mode == "120000":
            problems.append(f"{path}: a symlink")
        elif mode == "160000":
            problems.append(f"{path}: a submodule")
        if any(part in FORBIDDEN_NAMES for part in parts) or path.endswith(FORBIDDEN_SUFFIXES):
            problems.append(f"{path}: forbidden name")
        in_python_dirs = path.startswith(("agent-review/lib/agent_review/", "agent-review/tests/"))
        if path.endswith(".py") and not in_python_dirs:
            problems.append(f"{path}: Python files belong in lib/agent_review/ (or tests/)")
        if path.startswith("agent-review/lib/"):
            if not path.startswith("agent-review/lib/agent_review/"):
                problems.append(f"{path}: lib/ may hold only agent_review/")
            elif not path.endswith(".py"):
                problems.append(f"{path}: agent_review/ may hold only .py files")
    return problems


def shebang_problems(first_lines: Mapping[str, str]) -> list[str]:
    """No Python shebang outside lib/agent_review/ and tests/: such a file would run without '-I -S'.

    ``first_lines``: path under agent-review/ -> its first line.
    """
    return [
        f"{path}: a Python shebang ('{line}'); use an entry point in bin/"
        for path, line in sorted(first_lines.items())
        if line.startswith("#!")
        and PYTHON_WORD.search(line)
        and not path.startswith(("agent-review/lib/agent_review/", "agent-review/tests/"))
    ]


# --- Imports and calls (AST) -------------------------------------------------------

STDLIB_ALLOWED = frozenset(
    {
        "__future__",
        "argparse",
        "base64",
        "collections",
        "contextlib",
        "dataclasses",
        "datetime",
        "enum",
        "errno",
        "functools",
        "hashlib",
        "io",
        "itertools",
        "json",
        "math",
        "os",
        "pathlib",
        "re",
        "secrets",
        "selectors",
        "shutil",
        "stat",
        "string",
        "subprocess",
        "sys",
        "tempfile",
        "textwrap",
        "time",
        "tomllib",
        "typing",
        "unicodedata",
    }
)
RUNNER_MODULE = "lib/agent_review/runner.py"
GH_MODULE = "lib/agent_review/gh.py"
BANNED_CALLS = frozenset(
    {"eval", "exec", "compile", "__import__", "breakpoint", "globals", "locals", "vars", "__builtins__"}
)
# Banned as attributes of anything, not only of 'os', so that a re-exported
# or aliased module cannot reach them ('runner.os.system').
BANNED_OS = re.compile(r"system|popen|exec[lv]p?e?|spawn[lv]p?e?|posix_spawnp?|fork|forkpty", re.ASCII)
# 'sys.modules' gives any loaded module, whatever the import rules say.
SYS_MODULES_ALLOWED = frozenset({"lib/agent_review/probe.py"})
# Only gh.py may run programs on the trusted host, where the token is.
TRUSTED_ZONE_ALLOWED = frozenset({RUNNER_MODULE, GH_MODULE})
GH_ENTRY_POINTS = frozenset({"authorize", "publish-comment"})


def _module_name(rel: str) -> str | None:
    """'lib/agent_review/x.py' -> 'agent_review.x'; entry points -> None."""
    if not rel.startswith("lib/") or not rel.endswith(".py"):
        return None
    name = rel[len("lib/") : -len(".py")].replace("/", ".")
    return name.removesuffix(".__init__")


def _imported_names(tree: ast.AST) -> set[str]:
    """The names that import statements bind in ``tree``."""
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {a.asname or a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {a.asname or a.name for a in node.names}
    return bound


def ast_problems(sources: Mapping[str, str]) -> list[str]:
    """``sources``: path relative to agent-review/ -> text, for lib/ and the Python bin/ files."""
    problems = []
    for rel, text in sorted(sources.items()):
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as e:
            problems.append(f"{rel}: does not parse ({e.msg})")
            continue
        modules = _imported_names(tree)
        for node in ast.walk(tree):
            where = f"{rel}:{getattr(node, 'lineno', 0)}"
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
                # 'import os as o' would hide 'o.system' from the attribute rule.
                problems += [f"{where}: 'import os as {a.asname}'" for a in node.names if a.name == "os" and a.asname]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    problems.append(f"{where}: relative import; use 'agent_review.<module>'")
                    continue
                if any(a.name == "*" for a in node.names):
                    problems.append(f"{where}: 'from {node.module} import *'")
                names = [node.module or ""]
                if node.module == "os":
                    problems += [f"{where}: os.{a.name}" for a in node.names if BANNED_OS.fullmatch(a.name)]
                if (node.module or "").split(".")[0] == "agent_review":
                    # A standard-library module re-exported by a package module
                    # ('from agent_review.runner import subprocess').
                    problems += [
                        f"{where}: '{a.name}' imported through {node.module}"
                        for a in node.names
                        if a.name in STDLIB_ALLOWED or a.name in ("importlib", "pickle", "marshal", "ctypes")
                    ]
            for name in names:
                top = name.split(".")[0]
                if top == "subprocess" and rel != RUNNER_MODULE:
                    problems.append(f"{where}: subprocess outside agent_review/runner.py")
                elif top not in STDLIB_ALLOWED and top != "agent_review":
                    problems.append(f"{where}: import of '{name}' is not allowlisted")
            if isinstance(node, ast.Name) and node.id in BANNED_CALLS:
                problems.append(f"{where}: '{node.id}'")
            if isinstance(node, ast.Attribute):
                if BANNED_OS.fullmatch(node.attr):
                    problems.append(f"{where}: '.{node.attr}'")
                if node.attr == "subprocess" and rel != RUNNER_MODULE:
                    problems.append(f"{where}: '.subprocess' outside agent_review/runner.py")
                if node.attr == "TRUSTED_HOST" and rel not in TRUSTED_ZONE_ALLOWED:
                    problems.append(f"{where}: the trusted-host zone (the token) outside agent_review/gh.py")
                if (
                    node.attr == "modules"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "sys"
                    and rel not in SYS_MODULES_ALLOWED
                ):
                    problems.append(f"{where}: 'sys.modules'")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("getattr", "setattr", "delattr")
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in modules
            ):
                problems.append(f"{where}: {node.func.id}() on the module '{node.args[0].id}'")
            if isinstance(node, ast.keyword) and node.arg == "shell":
                problems.append(f"{where}: a 'shell=' argument")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.rsplit("/", 1)[-1] == "gh"
                and rel != GH_MODULE
            ):
                problems.append(f"{where}: the 'gh' program outside agent_review/gh.py")
    return problems


def _imports(text: str, known: set[str]) -> set[str]:
    """The agent_review modules that ``text`` imports."""
    found = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name in known}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if node.module in known:
                found.add(node.module)
            found |= {f"{node.module}.{a.name}" for a in node.names if f"{node.module}.{a.name}" in known}
    return found


def import_closure(text: str, modules: Mapping[str, str]) -> set[str]:
    """Every agent_review module reachable from ``text``; ``modules``: name -> text."""
    known = set(modules)
    seen: set[str] = set()
    todo = _imports(text, known)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        parent = name.rpartition(".")[0]
        if parent in known:
            todo.add(parent)
        todo |= _imports(modules[name], known) - seen
    return seen


def token_reach_problems(sources: Mapping[str, str]) -> list[str]:
    """Only the 'authorize' and 'publish-comment' entry points reach agent_review.gh."""
    modules = {m: text for rel, text in sources.items() if (m := _module_name(rel))}
    problems = []
    for rel, text in sorted(sources.items()):
        if not rel.startswith("bin/"):
            continue
        name = rel[len("bin/") :]
        if "agent_review.gh" in import_closure(text, modules) and name not in GH_ENTRY_POINTS:
            problems.append(f"{rel}: reaches agent_review.gh (the token); only {sorted(GH_ENTRY_POINTS)} may")
    return problems


# --- The repository ---------------------------------------------------------------


def git_index(prefix: str = "agent-review") -> list[tuple[str, str]]:
    """(mode, path) of the tracked files under ``prefix``: what the trusted tarball ships."""
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-s", "-z", "--", prefix],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    index = []
    for record in out.split("\0"):
        if record:
            meta, path = record.split("\t", 1)
            index.append((meta.split(" ")[0], path))
    return index


def read(rel: str) -> str:
    return (AR_DIR / rel).read_text(encoding="utf-8")


def repository() -> dict[str, Any]:
    """The tracked files the checks need."""
    index = git_index()
    modes = {path.removeprefix("agent-review/"): mode for mode, path in index}
    entries = {rel[len("bin/") :]: (read(rel), mode) for rel, mode in modes.items() if rel.startswith("bin/")}
    kinds = {name: entry_kind(name, text) for name, (text, _) in entries.items()}
    lib = {rel: read(rel) for rel in modes if rel.startswith("lib/") and rel.endswith(".py")}
    python = {**lib, **{f"bin/{n}": entries[n][0] for n, k in kinds.items() if k == "python"}}
    shell = {
        rel: read(rel)
        for rel in modes
        if rel.startswith("harnesses/") and (rel.endswith(".sh") or read(rel).startswith("#!"))
    }
    first_lines = {path: _first_line(path) for mode, path in index if mode != "120000"}
    return {
        "index": index,
        "entries": entries,
        "kinds": kinds,
        "lib": lib,
        "python": python,
        "shell": shell,
        "first_lines": first_lines,
    }


def _first_line(path: str) -> str:
    with (ROOT / path).open("rb") as f:
        return f.readline(256).decode("utf-8", errors="replace").rstrip("\n")


def load(path: pathlib.Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)
