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
  names.

Python (docs/adr/0003-python-runtime.md):

- call sites: every reference to '<root>/bin/<name>' uses the root allowed
  for its job and step, and the call form of the file's kind, derived from
  the file itself: '/usr/bin/python3 -I -S -B <root>/bin/<name>' for a
  Python entry point, the bash forms for a bash script. Python call sites
  name the file inline, never through a variable;
- no other mention of 'python', no 'pip', no 'setup-python' in
  agent-review.yml; in the shell files under bin/ and harnesses/, only the
  Python call form;
- entry points: each file in bin/ is a bash script (by its shebang) or exactly the Python
  template, and Python entry points are not executable (mode 100644);
- the tracked tree under agent-review/: no symlink or submodule, no '.pth',
  '.pyc' or '__pycache__', no 'sitecustomize.py' or 'usercustomize.py';
  lib/ holds only agent_review/, and that only '.py' files;
- imports (AST) of lib/ and the Python entry points: only the allowlisted
  standard-library modules and agent_review.*; no eval/exec/compile/
  __import__/breakpoint, no os.system/os.popen/os.exec*/os.spawn*/fork;
  subprocess only in agent_review/runner.py; no 'shell=' argument anywhere;
- token reach: only the 'authorize' and 'publish-comment' entry points can
  import agent_review.gh, and only gh.py names the 'gh' program.
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

# collect-result uses the absolute one: it runs on the tainted host.
BASH_SHEBANGS = ("#!/usr/bin/env bash", "#!/usr/bin/bash")
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
    """'bash', 'python', or None for a file that is neither (fail closed)."""
    first = text.split("\n", 1)[0]
    if first in BASH_SHEBANGS:
        return "bash"
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
            problems.append(
                f"bin/{name}: neither a bash script ({' or '.join(BASH_SHEBANGS)}) nor the exact Python template"
            )
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
# What may precede a reference, by kind: the text before it on its line.
EXISTENCE_CHECK = '[ -f "'
BASH_FORMS = ('bash "', "/usr/bin/bash ", "-- ", 'pc="')
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
                if kind == "python":
                    if not before.endswith(PYTHON_FORMS[root]) or PYTHON_AFTER[root].match(after) is None:
                        problems.append(f"{where}: bin/{name} is Python; call it exactly as {python_call(root, name)}")
                elif not before.endswith(BASH_FORMS):
                    problems.append(f"{where}: bin/{name} is bash; unexpected call form '{line.strip()}'")
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
PIP_WORD = re.compile(r"(?<![A-Za-z0-9_])pip(?:x|[0-9.]*)(?![A-Za-z0-9_])|setup-python|ensurepip", re.IGNORECASE)
YAML_PYTHON_CALL = re.compile(
    re.escape(PYTHON_CALL) + r' (?:"\$\{RUNNER_TEMP\}/trusted|/opt/agent-review)/bin/([a-z][a-z0-9-]*)', re.ASCII
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


SHELL_PYTHON_CALL = re.compile(
    re.escape(PYTHON_CALL) + r' "\$\{[A-Za-z_][A-Za-z0-9_]*\}/bin/([a-z][a-z0-9-]*)"', re.ASCII
)


def shell_python_problems(sources: Mapping[str, str], kinds: Mapping[str, str | None]) -> list[str]:
    """In shell files: Python only as '/usr/bin/python3 -I -S -B "${ROOT}/bin/<python entry>"'."""
    problems = []
    for rel, text in sorted(sources.items()):
        for number, line in enumerate(text.split("\n"), 1):
            if line.lstrip().startswith("#"):
                continue
            allowed = {
                m.start() + len("/usr/bin/")
                for m in SHELL_PYTHON_CALL.finditer(line)
                if kinds.get(m.group(1)) == "python"
            }
            if any(m.start() not in allowed for m in PYTHON_WORD.finditer(line)):
                problems.append(f"{rel}:{number}: Python only as '{PYTHON_CALL} \"${{ROOT}}/bin/<entry point>\"'")
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
        if path.startswith("agent-review/lib/"):
            if not path.startswith("agent-review/lib/agent_review/"):
                problems.append(f"{path}: lib/ may hold only agent_review/")
            elif not path.endswith(".py"):
                problems.append(f"{path}: agent_review/ may hold only .py files")
    return problems


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
        "itertools",
        "json",
        "math",
        "os",
        "re",
        "secrets",
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
BANNED_CALLS = frozenset({"eval", "exec", "compile", "__import__", "breakpoint"})
BANNED_OS = re.compile(r"system|popen|exec.*|spawn.*|posix_spawnp?|fork|forkpty", re.ASCII)
GH_ENTRY_POINTS = frozenset({"authorize", "publish-comment"})


def _module_name(rel: str) -> str | None:
    """'lib/agent_review/x.py' -> 'agent_review.x'; entry points -> None."""
    if not rel.startswith("lib/") or not rel.endswith(".py"):
        return None
    name = rel[len("lib/") : -len(".py")].replace("/", ".")
    return name.removesuffix(".__init__")


def ast_problems(sources: Mapping[str, str]) -> list[str]:
    """``sources``: path relative to agent-review/ -> text, for lib/ and the Python bin/ files."""
    problems = []
    for rel, text in sorted(sources.items()):
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as e:
            problems.append(f"{rel}: does not parse ({e.msg})")
            continue
        for node in ast.walk(tree):
            where = f"{rel}:{getattr(node, 'lineno', 0)}"
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    problems.append(f"{where}: relative import; use 'agent_review.<module>'")
                    continue
                names = [node.module or ""]
                if node.module == "os":
                    problems += [f"{where}: os.{a.name}" for a in node.names if BANNED_OS.fullmatch(a.name)]
            for name in names:
                top = name.split(".")[0]
                if top == "subprocess" and rel != RUNNER_MODULE:
                    problems.append(f"{where}: subprocess outside agent_review/runner.py")
                elif top not in STDLIB_ALLOWED and top != "agent_review":
                    problems.append(f"{where}: import of '{name}' is not allowlisted")
            if isinstance(node, ast.Name) and node.id in BANNED_CALLS:
                problems.append(f"{where}: '{node.id}'")
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and BANNED_OS.fullmatch(node.attr)
            ):
                problems.append(f"{where}: os.{node.attr}")
            if isinstance(node, ast.keyword) and node.arg == "shell":
                problems.append(f"{where}: a 'shell=' argument")
            if isinstance(node, ast.Constant) and node.value == "gh" and rel != GH_MODULE:
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
        if (rel.startswith("bin/") and kinds.get(rel[len("bin/") :]) == "bash")
        or (rel.startswith("harnesses/") and (rel.endswith(".sh") or read(rel).startswith("#!")))
    }
    return {"index": index, "entries": entries, "kinds": kinds, "lib": lib, "python": python, "shell": shell}


def load(path: pathlib.Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)
