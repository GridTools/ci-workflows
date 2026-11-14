# Python for the agent review's trusted code, and how it runs

- **Status**: valid
- **Authors**: Enrique González Paredes (@egparedes)
- **Created**: 2026-11-14
- **Updated**: 2026-11-14

Why-statement: In the context of the agent review's trusted scripts ([ADR 0001](0001-sandboxed-agent-pr-review.md), [ADR 0002](0002-reusable-workflow.md)), which run on the host next to tokens or inside the sandbox on hostile pull request content, facing bash's weak handling of untrusted text (byte-range `sed` sanitising, ad-hoc parsing, no unit tests), we decided to port them one by one to Python using only the standard library, run only as `/usr/bin/python3 -I -S -B <root>/bin/<name>` from thin entry points that refuse to run otherwise, with process execution, tool lookup and environment chosen per trust zone, to achieve code that can be unit-tested and type-checked without importing anything a pull request or the runner's environment can influence. We considered staying with bash and plain `python3 -I`, and accept a structure test that pins every call site and import, test-only tools installed from hash-pinned requirements, and a runtime tied to the runner image's Python (3.12 on `ubuntu-24.04`).

## Context

- The scripts in `agent-review/bin/` (`authorize`, `run-review`, `collect-result`, `publish-comment`) are about 1,000 lines of bash. `publish-comment` sanitises model output with `sed` in the only job with a write token; `run-review` parses untrusted JSON and paths in the sandbox.
- AWF chroots into the host filesystem, so the sandbox has the runner image's `/usr/bin/python3` (Ubuntu 24.04: 3.12) and standard library; no other Python is guaranteed, and nothing may be installed at run time.
- `python3 -I` alone is not enough on Ubuntu: it still imports `sitecustomize` (apport's hook, from `/etc/python3.12/`) and adds two `dist-packages` directories, whose `.pth` files it executes. `-I -S -B` leaves the standard library only (verified on 3.12.3).
- A shebang cannot carry two options (`#!/usr/bin/python3 -I -B` passes one argument, `-I -B`, which Python rejects), and a bare `python3` is looked up on the runner's `PATH`, which searches home directories that the sandbox can write first.
- `-B` stops Python writing `.pyc` files, not reading an existing one next to the source.
- Python's defaults accept more than bash's checks: `re.match` with `$` accepts a trailing newline; `\d` and `\w` match non-ASCII; `int()` accepts full-width digits, `_` and whitespace.
- The bash test suites are the specification of each script's behaviour (about 100 cases), and they find tools on `PATH` (a fake `gh`) and pass environment variables (`GIT_ALLOW_PROTOCOL=none`).

## Decision

- **Interpreter and flags.** Python entry points run only as `/usr/bin/python3 -I -S -B <root>/bin/<name>`: absolute interpreter, isolated mode (no `PYTHON*` variables, no user site, neither the script's directory nor the working directory on `sys.path`), no `site` (no `sitecustomize`, no `dist-packages`, no `.pth`), no bytecode written. Never `-c`, `-m`, `pip` or `setup-python` in `agent-review.yml`.
- **Entry points** are files without a `.py` suffix, mode 100644 (not executable, so no shebang can run them with other flags), and exactly one fixed template apart from the module name ([`workflow_structure.py`](../../agent-review/tests/workflow_structure.py)). The template exits with status 2 unless `sys.flags.isolated`, `no_site`, `dont_write_bytecode` and `safe_path` are all set, then inserts the real path of `bin/../lib` at `sys.path[0]` and calls `agent_review.<module>.main()`. All logic is in the package `agent-review/lib/agent_review/`, the only content of `lib/`.
- **Roots.** `"${RUNNER_TEMP}/trusted/bin/<name>"` in `authorize`, `prepare` and `publish`; `/opt/agent-review/bin/<name>` (root-owned, read-only in the sandbox) in both review jobs from the "Prepare the sandbox inputs" step on, including the AWF command lines. The library is found from the entry point's real path, so the same files work from both roots. Python call sites name the file inline, never through a variable. Shell files under `bin/` and `harnesses/` call Python only as `/usr/bin/python3 -I -S -B "${ar_root}/bin/<name>"`, with `ar_root` set from the file's own path by one fixed line (`ar_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)` for `harnesses/<name>/`). The call must start a command (start of line, whitespace, `$(`, `;`, `|` or `&` before it). No file outside `lib/agent_review/` and `tests/` is a `.py` file or has a Python shebang: such a file would run without `-I -S`.
- **Standard library only**, from an allowlist of modules; no `eval`, `exec`, `compile`, `__import__`, `globals()`, `vars()`, `importlib`, `pickle`, `marshal`, `ctypes`; no `system`, `popen`, `exec*`, `spawn*` or `fork` attribute of any object; no `shell=` argument anywhere. No indirect route around these rules: no `import os as …`, no `import *`, no `getattr` on a module, no `sys.modules` (except in the probe), no standard-library module imported through `agent_review.*`.
- **Processes and trust zones** ([`runner.py`](../../agent-review/lib/agent_review/runner.py), the only module that imports `subprocess`): argument lists only, the program resolved to an absolute path first, `PATH` entries that are not absolute skipped, stdin from `/dev/null` unless given.
  - *Trusted host* (`authorize`, `publish-comment post`: before any untrusted code, or on a fresh VM): `PATH` lookup and the inherited environment, as in bash. The workflow should set `PATH: /usr/bin:/bin` on those steps when they are ported.
  - *Sandbox* (`run-review`): `PATH` lookup and the inherited environment plus the caller's overrides (the forced `GIT_*` settings); an inherited `GIT_ALLOW_PROTOCOL` reaches git.
  - *Tainted host* (after the sandbox ran; `collect-result` runs there and starts no process): absolute programs under `/usr/bin` or `/bin` only, and a fixed environment (`PATH=/usr/bin:/bin`, `LC_ALL=C.UTF-8`).
- **GitHub API** through `gh api` as a subprocess ([`gh.py`](../../agent-review/lib/agent_review/gh.py)), never `urllib`: the token stays in `gh`, proxy variables are not honoured by accident, and the suites' fake `gh` keeps working. Projections stay in `gh --jq`, so the host never parses a PR object or a comment body (invariant 2). Only the `authorize` and `publish-comment` entry points may import it, and only `gh.py` names the program (by basename) and uses the trusted-host zone. Which steps hold the token is decided by the workflow (the step's `env:`); this rule keeps the code that can run `gh` narrow.
- **Untrusted data**: validation with `re.fullmatch` and ASCII-only classes, and before every `int()` ([`validate.py`](../../agent-review/lib/agent_review/validate.py)); files opened with `O_NOFOLLOW | O_NONBLOCK`, checked with `fstat` on the descriptor, read up to a cap ([`files.py`](../../agent-review/lib/agent_review/files.py)); bytes or strict UTF-8, never the locale's encoding; workflow commands escape `%`, CR and LF, step outputs reject CR and LF; untrusted log text gets a prefix on every line that starts with neither whitespace nor `:` (the runner trims leading whitespace before matching `::`), and every `##[` in it is broken up with a zero-width space (the runner's legacy parser finds `##[command]` anywhere in a line) ([`workflow.py`](../../agent-review/lib/agent_review/workflow.py)). `runner.run` caps a command's stdout (16 MiB by default) and keeps only the tail of its stderr.
- **The tree** under `agent-review/` has no symlink, submodule, `.pth`, `.pyc`, `__pycache__`, `sitecustomize.py` or `usercustomize.py`, and `lib/agent_review/` holds only `.py` files: the tarball the workflow fetches is the git tree.
- **Porting.** One script per pull request; it keeps the CLI contract (arguments, exit codes, outputs, notices), so the workflow change is the call line. The bash suites stay unchanged as black-box tests: `tests/lib.sh` runs each entry point with bash or with the Python command line, decided by the file. The bash script is deleted in the PR that ports it. New logic gets pytest unit tests.
- **Enforcement.** [`test-workflow-structure.py`](../../agent-review/tests/test-workflow-structure.py) checks all of the above on every pull request, deciding each file's kind from the file itself; its rules are unit-tested on crafted inputs in [`tests/python/`](../../agent-review/tests/python/). The self-test's stub run prints `bin/probe` on the host and inside AWF, and the `check` job asserts both lines.
- **Tooling**, test-only: pytest, mypy and PyYAML in the self-test's `python` job, from [`requirements.txt`](../../agent-review/tests/requirements.txt) with hashes (`pip install --require-hashes`) in a virtual environment; ruff and mypy in pre-commit, hooks pinned by commit SHA. Runtime code never imports them.

## Consequences

- Each bash script's replacement can be unit-tested and type-checked, while the bash suites keep checking the same behaviour.
- The runtime depends on the runner image's Python. A new `ubuntu-24.04` image with another 3.12 patch release is harmless; another minor version would fail the self-test's probe check (it expects 3.12) until reviewed. Inside AWF the interpreter is the same binary; the probe's two lines prove that on every self-test run.
- A new standard-library import, entry point or call site needs a change to the structure test's tables, which a reviewer sees.
- The inline `run:` bash in the workflow stays (rendering the AWF configuration, the token report, the publish step's result logic): Python has no YAML parser in the standard library, and "Fetch trusted files" bootstraps the trusted files, so it cannot be Python itself.
- Developers run the tests with `python3 -B -m pytest` from the repository root, with the tools of `requirements.txt` installed; without `-B`, `__pycache__` directories appear in the working tree (ignored by git, so never shipped).

## Alternatives considered

### Stay with bash

- Good, because nothing changes, and the scripts are reviewed.
- Bad, because sanitising and parsing hostile text with `sed`, `grep` and `jq` is hard to get right and to test, and bash has no types.

### `python3 -I` (or `-I -B`) and a shebang

- Good, because it is shorter and the scripts stay executable.
- Bad, because `-I` still runs `sitecustomize` and `.pth` files, a shebang cannot pass two options, and a bare `python3` comes from `PATH`.

### Dependencies (a vendored or pip-installed package)

- Good, because libraries such as a YAML parser or a Markdown sanitiser exist.
- Bad, because nothing may be installed at run time, vendored code needs its own review and updates, and the standard library covers what the scripts do.

## References

- [agent-review.md](../agent-review.md): security invariants, testing.
- Python docs: [Command line and environment](https://docs.python.org/3/using/cmdline.html) (`-I`, `-S`, `-B`, `-P`), [`sys.flags`](https://docs.python.org/3/library/sys.html#sys.flags).
