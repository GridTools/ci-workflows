"""Differential test: the legacy bash 'authorize' against the Python port.

Temporary (D5): this file and tests/legacy/authorize.sh are deleted once it
has run in CI. Both implementations run as the workflow runs them, each
from a staged copy of the trusted tree (bin/authorize, lib/ and
api-bases.txt; the bash script finds api-bases.txt from its own path), with
the same environment and the same fake 'gh' first on PATH.

The matrix crosses every variable the script reads (and the fake gh's
answers, and api-bases.txt) with boundary and hostile values, in every
event and action scenario. The reference is bash under LC_ALL=C.UTF-8 (the
hosted runner's locale; Python does not depend on it). For each case:

- the exit code, the outputs (call_id masked), the gh calls (argv) and the
  log are identical. The log is compared after undoing the workflow-command
  escaping of the Python side ('%', CR, LF; the broken-up '##['): bash
  printed values raw, which let them end the command;
- unless the case belongs to a documented tightening (TIGHTENINGS), where
  Python must fail closed (exit 1, allowed=false) or differ in the log only;
- in every case, Python never allows what bash denies (no loosening).

A sample also runs under LC_ALL=C and en_US.UTF-8: Python's results are
identical in every locale; bash in en_US.UTF-8 is looser (its [0-9] matches
'\uff11', '\u0661' and '\u00b2', its [A-Za-z] matches '\u00e9'), so there only the
no-loosening rule applies.
"""

import concurrent.futures
import os
import pathlib
import re
import shutil
import subprocess
import warnings
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest

AR_DIR = pathlib.Path(__file__).resolve().parents[2]
LEGACY = AR_DIR / "tests" / "legacy" / "authorize.sh"
REFERENCE_LOCALE = "C.UTF-8"
OTHER_LOCALES = ("C", "en_US.UTF-8")
CENTRAL = "GridTools/ci-workflows"
CALLER = "example-org/example"
CSCS = "https://api.inference.cscs.ch/v1"
HEAD = "a" * 40
BASE = "b" * 40
WF_SHA = "c" * 40
UNSET = None
CALL_ID = re.compile(rb"^call_id=[0-9a-f]{16}$", re.MULTILINE)

FAKE_GH = r"""#!/usr/bin/bash
# Logs its argv (NUL-separated, one call per line) and answers from FAKE_*:
# printf formats, so that a test can make it print NUL bytes.
{ printf '%s\0' "$@"; printf '\n'; } >>"${FAKE_LOG}"
case "$2" in
  */pulls/*) k=PR ;;
  */collaborators/*/permission) k=PERM ;;
  *) echo "unexpected call: $*" >&2; exit 1 ;;
esac
out=FAKE_${k}_OUT err=FAKE_${k}_ERR rc=FAKE_${k}_RC
printf -- "${!out-}"
printf -- "${!err-}" >&2
exit "${!rc:-0}"
"""

DEFAULT_API_BASES = (AR_DIR / "api-bases.txt").read_text(encoding="utf-8")


# --- The base scenarios: every event and action ------------------------------------

BASE_ENV: dict[str, str | None] = {
    "REPO": CALLER,
    "EVENT": "workflow_dispatch",
    "ACTION": "",
    "AUTHOR": "author",
    "SENDER": "sender",
    "ACTOR": "actor",
    "EVENT_PR": "",
    "EVENT_HEAD_SHA": "",
    "EVENT_BASE_SHA": "",
    "EVENT_DRAFT": "",
    "EVENT_LABEL": "",
    "IN_PR": "7",
    "IN_HARNESS": "opencode",
    "IN_MODEL": "zai-org/GLM-5.3-Flash",
    "IN_DRY_RUN": "false",
    "IN_LABEL": "agent-review",
    "IN_PROJECT": "",
    "IN_RULES": "AGENTS.md\nCODING_GUIDELINES.md",
    "IN_PATTERNS": "",
    "IN_ADDENDUM": ".github/agent-review/prompt-addendum.md",
    "IN_API_BASE": CSCS,
    "IN_RPH": "100",
    "IN_RPM": "30",
    "IN_TURNS": "80",
    "IN_TIMEOUT": "20",
    "IN_ENVIRONMENT": "agent-review",
    "IN_TEST_REPO": "",
    "TRUSTED_REPO": CENTRAL,
    "TRUSTED_SHA": WF_SHA,
    "CALLER_WF_SHA": "d" * 40,
    "HAS_SHARED_KEY": "false",
    "FAKE_PR_OUT": f"{HEAD}\\t{BASE}\\n",
    "FAKE_PERM_OUT": "write\\n",
}
VARIABLES = [k for k in BASE_ENV if not k.startswith("FAKE_")]


def prt(action: str, **extra: str) -> dict[str, str | None]:
    env: dict[str, str | None] = {
        "EVENT": "pull_request_target",
        "ACTION": action,
        "EVENT_PR": "12",
        "EVENT_HEAD_SHA": HEAD,
        "EVENT_BASE_SHA": BASE,
        "EVENT_DRAFT": "false",
        "IN_PR": "",
    }
    if action == "labeled":
        env["EVENT_LABEL"] = "agent-review"
    env.update(extra)
    return env


SELF = {"REPO": CENTRAL, "IN_HARNESS": "stub", "IN_MODEL": "", "IN_TEST_REPO": "GridTools/gt4py", "IN_PR": "2983"}
SCENARIOS: dict[str, dict[str, str | None]] = {
    "dispatch-keyed": {},
    "dispatch-pi": {"IN_HARNESS": "pi"},
    "dispatch-stub": {"IN_HARNESS": "stub", "IN_MODEL": ""},
    "dispatch-self-test": dict(SELF),
    "prt-labeled": prt("labeled"),
    "prt-labeled-stub": prt("labeled", IN_HARNESS="stub"),
    "prt-opened": prt("opened"),
    "prt-reopened": prt("reopened"),
    "prt-ready": prt("ready_for_review"),
    "prt-opened-draft": prt("opened", EVENT_DRAFT="true"),
    "prt-synchronize": prt("synchronize"),
    "prt-no-action": prt(""),
    "pr-caller-stub": {"EVENT": "pull_request", "IN_HARNESS": "stub", "IN_PR": "", "EVENT_PR": "12"},
    "pr-self-test": {"EVENT": "pull_request", **SELF},
    "pr-keyed": {"EVENT": "pull_request", "EVENT_PR": "12"},
    "push-self-test": {"EVENT": "push", **SELF},
    "schedule-self-test": {"EVENT": "schedule", **SELF},
    "push-caller": {"EVENT": "push", "IN_HARNESS": "stub"},
    "issue-comment": {"EVENT": "issue_comment", "IN_HARNESS": "stub"},
    "no-event": {"EVENT": ""},
}


# --- Values ------------------------------------------------------------------------------


def variants(valid: str) -> list[str]:
    """Hostile spellings of a valid value."""
    return [
        valid,
        f" {valid}",
        f"{valid} ",
        f"\t{valid}",
        f"{valid}\n",
        f"\n{valid}",
        f"{valid}\r",
        f"{valid}\r\n",
        f"{valid}\nx",
        f"{valid}%0Ax",
        f"{valid}\u3000",
        f"\u00a0{valid}",
        f"{valid}\u200b",
        valid.upper(),
        valid[:-1],
        valid + valid[-1:] * 300,
    ]


GENERIC = [
    UNSET,
    "",
    " ",
    "\t",
    "\n",
    "\r\n",
    "x",
    "-x",
    "0",
    "\u0661",  # ARABIC-INDIC DIGIT ONE
    "\uff11",  # FULLWIDTH DIGIT ONE
    "\u00b2",  # SUPERSCRIPT TWO
    "\u00e9",
    "\u212a",  # KELVIN SIGN
    "\u3000",
    "%0A::error::x",
    "##[error]x",
    "::warning::x",
    "x\n::notice::y",
    "\x1b[31mx",
    "\x7f",
    "\x0b",
    "\x1c",
    "$(id)",
    "a'b\"c\\d",
    "*",
    "../x",
    "x" * 5000,
]


def digits(maximum: int) -> list[str]:
    return [
        "1",
        str(maximum),
        str(maximum + 1),
        str(maximum - 1),
        "0",
        "00",
        "01",
        "-1",
        "+1",
        "1.0",
        "1e1",
        "1_0",
        "0x1",
        "\u0661",
        "\uff11",
        "1\uff11",
        "1\u0661",
        "\u00b2",
        " 5",
        "5 ",
        "5\n",
        "5\n6",
        "9999",
        "10000",
    ]


PR_NUMBERS = [
    "1",
    "7",
    "9999999999",
    "10000000000",
    "0",
    "07",
    "7 ",
    "7\n",
    "-7",
    "+7",
    "7.0",
    "\u0667",
    "\uff17",
    "7; id",
]
SHAS = [
    "a" * 40,
    "a" * 39,
    "a" * 41,
    "A" * 40,
    "g" * 40,
    "0123456789abcdef" * 2 + "01234567",
    "a" * 40 + "\n",
    " " + "a" * 40,
]
REPOS = [
    "a/b",
    CALLER,
    CENTRAL,
    "GridTools/gt4py",
    "a/b/c",
    "a",
    "/b",
    "a/",
    "a//b",
    "../x",
    "x/..",
    "../..",
    "./.",
    "a/.",
    ".../...",
    "a b/c",
    "a/b\n",
    "a/b ",
    "\u00e9/b",
    "a/b%2f",
]
LOGINS = [
    "actor",
    "sender",
    "author",
    "dependabot[bot]",
    "a" * 39,
    "a" * 40,
    "a" * 39 + "[bot]",
    "-x",
    "x-",
    "a_b",
    "a.b",
    "a[bot]x",
    "a[bot][bot]",
    "[bot]",
    "a/b",
    "..",
]
PATH_LISTS = [
    "AGENTS.md",
    "a\r\nb.md",
    "a.md\r",
    "AGENTS.md\n\n# c\n",
    "  AGENTS.md  ",
    "\tAGENTS.md\t",
    "\x0bAGENTS.md\x0c",
    "\x1cAGENTS.md",
    "\u3000AGENTS.md",
    "AGENTS.md\u3000",
    "\u2028AGENTS.md",
    "\u1680AGENTS.md",
    "\u205fAGENTS.md",
    "\u2003AGENTS.md",
    "\u00a0AGENTS.md",
    "\u0085AGENTS.md",
    "\u200bAGENTS.md",
    "a\x0bb",
    "docs/",
    "docs//",
    "docs/**/",
    "*.md",
    "**/*.mdc",
    "a/./b",
    "./a",
    "a/.",
    ".",
    "..",
    "a..b",
    "...",
    "a/b/",
    "/a",
    ":(glob)x",
    "a b.md",
    "a\\b",
    "#x\n../y",
    " # comment\nAGENTS.md",
    "x" * 200,
    "x" * 201,
    "x/" * 99 + "xy",
    "\n\n\n",
    "a\n" * 50,
]
ADDENDA = [
    "a.md",
    "a.md\n",
    "\na.md",
    "a.md\n ",
    " \n",
    "\n",
    " ",
    "a.md\r",
    "a.md\n\r",
    "\r\na.md\r\n",
    "a.md\nb.md",
    "#x",
    " #x",
    "a?ref=main",
    "x%2f.md",
    "\u3000a.md",
    "a.md\u3000",
    "\u2028a.md",
]
API_BASE_VALUES = [CSCS, "", f"{CSCS}/", f"{CSCS} ", f" {CSCS}", f"{CSCS}\n", f"#{CSCS}", CSCS.upper(), "x", "\r"]
LABELS = [
    "agent-review",
    "needs review: agent",
    "a" * 50,
    "a" * 51,
    " agent-review",
    "a\\b",
    "a\tb",
    "a b ",
    "-a",
    ".a",
    "a/b:c.d_e",
]
SPECIFIC: dict[str, list[str]] = {
    "REPO": REPOS,
    "TRUSTED_REPO": REPOS,
    "IN_TEST_REPO": REPOS,
    "TRUSTED_SHA": SHAS,
    "EVENT_HEAD_SHA": SHAS,
    "EVENT_BASE_SHA": SHAS,
    "CALLER_WF_SHA": SHAS,
    "IN_PR": PR_NUMBERS,
    "EVENT_PR": PR_NUMBERS,
    "AUTHOR": LOGINS,
    "SENDER": LOGINS,
    "ACTOR": LOGINS,
    "EVENT": [
        "pull_request_target",
        "workflow_dispatch",
        "pull_request",
        "push",
        "schedule",
        "workflow_run",
        "merge_group",
        "pull_request_review",
        "PUSH",
        "push ",
    ],
    "ACTION": [
        "labeled",
        "opened",
        "reopened",
        "ready_for_review",
        "synchronize",
        "edited",
        "unlabeled",
        "closed",
        "labeled ",
        "Labeled",
    ],
    "EVENT_DRAFT": ["true", "false", "True", "true ", "1"],
    "EVENT_LABEL": ["agent-review", "bug", "agent-review ", "Agent-review", "needs review: agent"],
    "IN_HARNESS": ["stub", "opencode", "pi", "Stub", "stub ", "opencode\n", "memdoor", "open code"],
    "IN_MODEL": ["m", "m:1", "a" * 128, "a" * 129, "/m", "-m", "a b", "zai-org/GLM-5.3-Flash"],
    "IN_DRY_RUN": ["true", "false", "True", "yes", "1", "false "],
    "IN_LABEL": LABELS,
    "IN_PROJECT": ["GT4Py", "icon 4py", "a" * 64, "a" * 65, " x", "x ", "a;b", "a\tb", "-x", "_x"],
    "IN_RULES": PATH_LISTS,
    "IN_PATTERNS": PATH_LISTS,
    "IN_ADDENDUM": ADDENDA,
    "IN_API_BASE": API_BASE_VALUES,
    "IN_RPH": digits(100),
    "IN_RPM": digits(30),
    "IN_TURNS": digits(80),
    "IN_TIMEOUT": digits(20),
    "IN_ENVIRONMENT": ["agent-review", "a" * 64, "a" * 65, "-x", ".x", "a b", "a/b", "a_b.c-d"],
    "HAS_SHARED_KEY": ["true", "false", "True", "false ", "1"],
}

# The fake gh's answers (printf formats: '\\t' is a tab, '\\0' a NUL byte).
H, B = HEAD, BASE
PR_ANSWERS: list[dict[str, str]] = [
    {"FAKE_PR_OUT": f"{H}\\t{B}\\n"},
    {"FAKE_PR_OUT": f"{H} {B}"},
    {"FAKE_PR_OUT": f"  {H}\\t\\t{B}  \\n\\n"},
    {"FAKE_PR_OUT": f"{H}\\t{B}\\t{H}\\n"},
    {"FAKE_PR_OUT": f"{H}\\r\\t{B}\\n"},
    {"FAKE_PR_OUT": f"{H}\\t{B}\\r\\n"},
    {"FAKE_PR_OUT": f"{H}\\n{B}\\n"},
    {"FAKE_PR_OUT": f"\\n{H}\\t{B}\\n"},
    {"FAKE_PR_OUT": f"{H}\\v{B}\\n"},
    {"FAKE_PR_OUT": f"{H}\\f{B}\\n"},
    {"FAKE_PR_OUT": f"{H}\\n"},
    {"FAKE_PR_OUT": ""},
    {"FAKE_PR_OUT": f"{H.upper()}\\t{B}\\n"},
    {"FAKE_PR_OUT": f"{H[:20]}\\0{H[20:]}\\t{B}\\n"},
    {"FAKE_PR_OUT": f"{H}\\t{B}\\0\\n"},
    {"FAKE_PR_OUT": f"{H}\\t{B}\\xff\\n"},
    {"FAKE_PR_OUT": f"{H}\\t{B}\\n", "FAKE_PR_ERR": "warning\\n"},
    {"FAKE_PR_OUT": "", "FAKE_PR_ERR": "HTTP 502\\n", "FAKE_PR_RC": "1"},
    {"FAKE_PR_OUT": "", "FAKE_PR_ERR": "HTTP 404\\n%0A::error::x ##[warning]y\\n", "FAKE_PR_RC": "1"},
    {"FAKE_PR_OUT": f"{H}\\t{B}\\n", "FAKE_PR_ERR": "", "FAKE_PR_RC": "1"},
    {"FAKE_PR_OUT": '{"message":"Not Found"}', "FAKE_PR_ERR": "gh: Not Found (HTTP 404)\\n", "FAKE_PR_RC": "1"},
]
PERM_ANSWERS: list[dict[str, str]] = [
    {"FAKE_PERM_OUT": v}
    for v in [
        "write\\n",
        "admin\\n",
        "admin",
        "write\\n\\n\\n",
        " write\\n",
        "write \\n",
        "write\\r\\n",
        "write\\t\\n",
        "write\\nx\\n",
        "\\nwrite\\n",
        "wr\\0ite\\n",
        "write\\0\\n",
        "maintain\\n",
        "triage\\n",
        "read\\n",
        "none\\n",
        "WRITE\\n",
        "\\n",
        "",
        "%0A::error::x\\n",
    ]
] + [
    {"FAKE_PERM_OUT": "write\\n", "FAKE_PERM_ERR": "warning\\n"},
    {"FAKE_PERM_OUT": "", "FAKE_PERM_ERR": "write\\n"},
    {"FAKE_PERM_OUT": "", "FAKE_PERM_ERR": "HTTP 502\\n", "FAKE_PERM_RC": "1"},
    {"FAKE_PERM_OUT": "write\\n", "FAKE_PERM_RC": "4"},
]
API_BASES_FILES = {
    "default": DEFAULT_API_BASES,
    "two": f"# comment\n{CSCS}\nhttps://b.example/v1\n",
    "crlf": f"# comment\r\n{CSCS}\r\n",
    "no-final-newline": f"{CSCS}",
    "padded": f" {CSCS}\n{CSCS} \n",
    "blank": "\n\n# only comments\n",
    "empty": "",
    "comment-default": f"#{CSCS}\n\n{CSCS}\n",
}


@dataclass(frozen=True)
class Case:
    name: str
    env: dict[str, str | bytes | None]
    api_bases: str = "default"
    tags: frozenset[str] = field(default_factory=frozenset)


def cases() -> Iterator[Case]:
    for scenario, overrides in SCENARIOS.items():
        base: dict[str, str | bytes | None] = {**BASE_ENV, **overrides}
        yield Case(f"{scenario}", base)
        for var in VARIABLES:
            values: list[str | None] = [*GENERIC, *SPECIFIC.get(var, [])]
            valid = base[var]
            if isinstance(valid, str) and valid:
                values += variants(valid)
            for i, value in enumerate(dict.fromkeys(values)):
                yield Case(f"{scenario}/{var}#{i}", {**base, var: value})
        for kind, answers in (("pr", PR_ANSWERS), ("perm", PERM_ANSWERS)):
            for i, answer in enumerate(answers):
                yield Case(f"{scenario}/gh-{kind}#{i}", {**base, **answer})
                yield Case(f"{scenario}/gh-{kind}#{i}/bot", {**base, **answer, "ACTOR": "dependabot[bot]"})
        # Not UTF-8: the workflow cannot pass such a value.
        for var in ("IN_MODEL", "EVENT_LABEL", "CALLER_WF_SHA", "IN_RULES"):
            yield Case(f"{scenario}/{var}/not-utf8", {**base, var: b"x\xff"})
    for name in API_BASES_FILES:
        for i, value in enumerate(API_BASE_VALUES):
            for scenario in ("dispatch-keyed", "dispatch-stub"):
                yield Case(
                    f"{scenario}/api-bases-{name}#{i}",
                    {**BASE_ENV, **SCENARIOS[scenario], "IN_API_BASE": value},
                    api_bases=name,
                )


# --- Running ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    rc: int
    output: bytes
    stdout: bytes
    stderr: bytes
    gh_calls: bytes

    def outputs(self) -> list[tuple[str, str]]:
        lines = CALL_ID.sub(b"call_id=<masked>", self.output).decode("utf-8", errors="surrogateescape")
        return [tuple(line.split("=", 1)) for line in lines.split("\n") if line]  # type: ignore[misc]

    @property
    def allowed(self) -> bool:
        return ("allowed", "true") in self.outputs()


def stage(tmp: pathlib.Path) -> dict[tuple[str, str], pathlib.Path]:
    """Trusted trees per (implementation, api-bases variant), and the fake gh."""
    roots = {}
    for name, text in API_BASES_FILES.items():
        for impl in ("bash", "python"):
            root = tmp / f"{impl}-{name}"
            (root / "bin").mkdir(parents=True)
            if impl == "bash":
                shutil.copyfile(LEGACY, root / "bin" / "authorize")
            else:
                shutil.copyfile(AR_DIR / "bin" / "authorize", root / "bin" / "authorize")
                shutil.copytree(AR_DIR / "lib", root / "lib", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (root / "api-bases.txt").write_bytes(text.encode("utf-8"))
            roots[(impl, name)] = root
    fakebin = tmp / "fakebin"
    fakebin.mkdir()
    (fakebin / "gh").write_text(FAKE_GH)
    (fakebin / "gh").chmod(0o755)
    return roots


def run(root: pathlib.Path, impl: str, case: Case, locale: str, work: pathlib.Path) -> Result:
    work.mkdir(parents=True)
    out, log = work / "out", work / "gh.log"
    env: dict[str, str | bytes] = {
        "PATH": f"{root.parent / 'fakebin'}:/usr/bin:/bin",
        "LC_ALL": locale,
        "GITHUB_OUTPUT": str(out),
        "FAKE_LOG": str(log),
    }
    env.update({k: v for k, v in case.env.items() if v is not None})
    entry = str(root / "bin" / "authorize")
    argv = ["/usr/bin/bash", entry] if impl == "bash" else ["/usr/bin/python3", "-I", "-S", "-B", entry]
    p = subprocess.run(argv, env=env, cwd=work, capture_output=True, timeout=60, check=False)
    return Result(p.returncode, read_if_any(out), p.stdout, p.stderr, read_if_any(log))


def read_if_any(path: pathlib.Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


ESCAPED = re.compile(r"%(25|0D|0A)")


def unescape(stdout: bytes) -> bytes:
    """The Python log as bash would have printed it: '%', CR, LF unescaped, '##[' restored."""
    text = stdout.decode("utf-8", errors="strict")
    text = ESCAPED.sub(lambda m: {"25": "%", "0D": "\r", "0A": "\n"}[m.group(1)], text)
    return text.replace("#\u200b#[", "##[").encode("utf-8")


# --- Documented differences -------------------------------------------------------------

# Characters of [:space:] in glibc's C.UTF-8 that are not ASCII: bash trims
# them around list entries; Python trims ASCII whitespace only (C locale).
UNICODE_SPACES = "\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2008\u2009\u200a\u2028\u2029\u205f\u3000"
TIGHTENINGS = {
    "not-utf8": "A variable is not UTF-8: Python fails (bash compared bytes). The workflow cannot pass such a value.",
    "unicode-space": "A list or addendum line starts or ends with a non-ASCII space: bash (C.UTF-8) trimmed it,"
    " Python trims ASCII whitespace only, so the entry is invalid.",
    "gh-nul": "gh printed a NUL byte: bash's $(...) dropped it, Python keeps it, so the value never matches.",
    "gh-stderr": "gh exited 0 but wrote to stderr: bash's '2>&1' mixed it into the value (or, for a sha, raced"
    " with it); Python treats it as an API failure.",
    "dot-segment": "A repository name with a '.' or '..' segment reaches a gh endpoint: gh.api refuses it"
    " (bash called 'gh api repos/../x/...').",
    "gh-failure-text": "gh failed and printed to stdout: bash quoted stdout and stderr in its error, Python stderr"
    " only (the log text differs, nothing else).",
    "gh-not-utf8": "gh printed bytes that are not UTF-8: bash echoed them raw in its error, Python shows U+FFFD"
    " (the log text differs, nothing else).",
}
TIGHTENINGS["api-bases-cr"] = (
    "api-bases.txt has CRLF line ends and the default is used: bash wrote the CR into GITHUB_OUTPUT, Python"
    " refuses a line break in an output and fails. (The file is trusted and has LF line ends.)"
)
# Differences in the log text only: the exit code, outputs and gh calls agree.
LOG_ONLY = frozenset({"gh-failure-text", "gh-not-utf8"})


def tags(case: Case) -> set[str]:
    env = case.env
    found = set()
    if "\r" in API_BASES_FILES[case.api_bases]:
        found.add("api-bases-cr")
    if any(isinstance(v, bytes) for v in env.values()):
        found.add("not-utf8")
    for var in ("IN_RULES", "IN_PATTERNS", "IN_ADDENDUM"):
        value = env.get(var)
        if isinstance(value, str) and any(c in UNICODE_SPACES for c in value):
            found.add("unicode-space")
    for k in ("FAKE_PR_OUT", "FAKE_PERM_OUT"):
        if "\\0" in str(env.get(k) or ""):
            found.add("gh-nul")
        if "\\xff" in str(env.get(k) or ""):
            found.add("gh-not-utf8")
    for k in ("PR", "PERM"):
        if env.get(f"FAKE_{k}_ERR") and not env.get(f"FAKE_{k}_RC"):
            found.add("gh-stderr")
        if env.get(f"FAKE_{k}_RC") and env.get(f"FAKE_{k}_OUT"):
            found.add("gh-failure-text")
    for var in ("REPO", "IN_TEST_REPO"):
        value = env.get(var)
        if isinstance(value, str) and any(s in (".", "..") for s in value.split("/")):
            found.add("dot-segment")
    return found


def compare(case: Case, bash: Result, py: Result) -> str | None:
    """None if the results agree as documented, else what differs."""
    if py.stderr != b"":
        return f"Python wrote to stderr: {py.stderr[-500:]!r}"
    if py.allowed and (not bash.allowed or py.outputs() != bash.outputs()):
        return f"LOOSENING: Python allowed, bash {bash.rc} {bash.outputs()}"
    same = (
        py.rc == bash.rc
        and py.outputs() == bash.outputs()
        and py.gh_calls == bash.gh_calls
        and unescape(py.stdout) == bash.stdout
    )
    if same:
        return None
    found = tags(case)
    if not found:
        return (
            f"rc {bash.rc}/{py.rc}\n  outputs {bash.outputs()}\n       vs {py.outputs()}\n"
            f"  gh {bash.gh_calls!r}\n  vs {py.gh_calls!r}\n  log {bash.stdout!r}\n   vs {unescape(py.stdout)!r}"
        )
    if found <= LOG_ONLY:
        if py.rc == bash.rc and py.outputs() == bash.outputs() and py.gh_calls == bash.gh_calls:
            return None
    elif py.rc in (0, 1) and py.outputs() == [("allowed", "false")] and not (bash.rc == 1 and py.rc == 0):
        # Python denies: a fail, or a skip where bash allowed or skipped too.
        return None
    return f"tagged {sorted(found)} but Python did not deny: rc {bash.rc}/{py.rc} {py.outputs()}"


def parallel(fn: Callable[[int, Case], tuple[Case, str | None]], items: list[Case]) -> list[tuple[Case, str | None]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=(os.cpu_count() or 2) * 2) as pool:
        return list(pool.map(fn, range(len(items)), items))


def available(locale: str) -> bool:
    names = subprocess.run(["/usr/bin/locale", "-a"], capture_output=True, check=True).stdout.decode().split()
    return locale.lower().replace("-", "") in {n.lower().replace("-", "") for n in names}


ALL_CASES = list(cases())
# The matrix takes several minutes: it runs in the self-test's 'differential'
# job, which sets AUTHORIZE_DIFFERENTIAL=1, not in the 'python' job.
# AUTHORIZE_DIFFERENTIAL_FILTER (a substring of case names) narrows it locally.
pytestmark = pytest.mark.skipif(
    os.environ.get("AUTHORIZE_DIFFERENTIAL") != "1", reason="set AUTHORIZE_DIFFERENTIAL=1 to run the matrix"
)
CASES = [c for c in ALL_CASES if os.environ.get("AUTHORIZE_DIFFERENTIAL_FILTER", "") in c.name]


def test_matrix_is_large() -> None:
    assert len(ALL_CASES) > 15000
    names = [c.name for c in ALL_CASES]
    assert {n.split("/")[0] for n in names} == set(SCENARIOS)


def test_bash_and_python_agree(tmp_path: pathlib.Path) -> None:
    roots = stage(tmp_path)

    def one(i: int, case: Case) -> tuple[Case, str | None]:
        work = tmp_path / "w" / str(i)
        bash = run(roots[("bash", case.api_bases)], "bash", case, REFERENCE_LOCALE, work / "bash")
        py = run(roots[("python", case.api_bases)], "python", case, REFERENCE_LOCALE, work / "python")
        shutil.rmtree(work)
        return case, compare(case, bash, py)

    results = parallel(one, CASES)
    problems = [f"{c.name}: {p}" for c, p in results if p]
    counts = Counter(t for c, p in results for t in tags(c))
    warnings.warn(
        f"differential: {len(results)} cases, bash LC_ALL={REFERENCE_LOCALE}; runner LANG={os.environ.get('LANG')!r}"
        f" LC_ALL={os.environ.get('LC_ALL')!r}; cases per tightening: {dict(sorted(counts.items()))}",
        stacklevel=1,
    )
    assert not problems, f"{len(problems)} differences:\n" + "\n".join(problems[:40])


@pytest.mark.parametrize("locale", OTHER_LOCALES)
def test_python_ignores_the_locale_and_bash_is_never_stricter(tmp_path: pathlib.Path, locale: str) -> None:
    if not available(locale):
        pytest.skip(f"locale {locale} not installed")
    roots = stage(tmp_path)
    sample = CASES[::10]

    def one(i: int, case: Case) -> tuple[Case, str | None]:
        work = tmp_path / "w" / str(i)
        ref = run(roots[("python", case.api_bases)], "python", case, REFERENCE_LOCALE, work / "ref")
        py = run(roots[("python", case.api_bases)], "python", case, locale, work / "python")
        bash = run(roots[("bash", case.api_bases)], "bash", case, locale, work / "bash")
        shutil.rmtree(work)
        if (py.rc, py.outputs(), py.stdout, py.gh_calls) != (ref.rc, ref.outputs(), ref.stdout, ref.gh_calls):
            return case, f"Python differs between {REFERENCE_LOCALE} and {locale}"
        if py.allowed and (not bash.allowed or py.outputs() != bash.outputs()):
            return case, f"LOOSENING against bash in {locale}: bash {bash.rc} {bash.outputs()}"
        return case, None

    results = parallel(one, sample)
    problems = [f"{c.name}: {p}" for c, p in results if p]
    assert not problems, "\n".join(problems[:40])


def test_without_github_output_both_fail(tmp_path: pathlib.Path) -> None:
    roots = stage(tmp_path)
    for impl in ("bash", "python"):
        entry = str(roots[(impl, "default")] / "bin" / "authorize")
        argv = ["/usr/bin/bash", entry] if impl == "bash" else ["/usr/bin/python3", "-I", "-S", "-B", entry]
        env = {k: v for k, v in BASE_ENV.items() if v is not None and not k.startswith("FAKE_")}
        for output in (None, ""):
            e = {**env, "PATH": "/usr/bin:/bin"} | ({} if output is None else {"GITHUB_OUTPUT": output})
            p = subprocess.run(argv, env=e, cwd=tmp_path, capture_output=True, timeout=60, check=False)
            assert p.returncode == 1, (impl, output, p)
