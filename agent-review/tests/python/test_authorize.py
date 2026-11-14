"""agent_review.authorize: the validators, the bash emulations and the decision.

The black-box cases are in tests/test-authorize.sh (unchanged from the bash
version); these test each piece, with the bash traps of
docs/adr/0003-python-runtime.md, and the entry point in a hostile
environment.
"""

import os
import pathlib
import re
import subprocess

import pytest

from agent_review import authorize, validate
from agent_review.authorize import Fail, Skip

AR_DIR = pathlib.Path(__file__).resolve().parents[2]
CSCS = "https://api.inference.cscs.ch/v1"
HEAD, BASE = "a" * 40, "b" * 40


# --- Reading the environment ---------------------------------------------------


def test_unset_is_empty_and_values_are_kept_as_they_are() -> None:
    env = authorize.read_environment({b"REPO": b" a/b\n", b"OTHER": b"x"})
    assert env["REPO"] == " a/b\n"
    assert env["IN_PR"] == ""
    assert set(env) == set(authorize.VARIABLES)


def test_values_that_are_not_utf8_fail() -> None:
    with pytest.raises(Fail, match="IN_MODEL is not valid UTF-8"):
        authorize.read_environment({b"IN_MODEL": b"x\xff"})
    with pytest.raises(Fail, match="REPO"):
        authorize.read_environment({b"REPO": b"\xed\xa0\x80"})  # an encoded surrogate


# --- Shell semantics ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("  a  ", "a"),
        ("\t\r\n\v\fa\f\v\n\r\t", "a"),
        ("   ", ""),
        ("a b", "a b"),
        # Not [:space:] in the C locale: kept, so the entry is invalid.
        ("\u3000a", "\u3000a"),
        ("a\u00a0", "a\u00a0"),
        ("\x1ca\x1f", "\x1ca\x1f"),
        ("\u2028a\x85", "\u2028a\x85"),
    ],
)
def test_trim_is_ascii_only(text: str, want: str) -> None:
    assert authorize.trim(text) == want
    # str.strip() would remove the non-ASCII ones.
    if want != text.strip():
        assert text.strip() != authorize.trim(text)


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        ("", [""]),
        ("a", ["a"]),
        ("a\n", ["a", ""]),
        ("a\r\nb", ["a\r", "b"]),
        ("a\x0bb\x0cc\x1cd\x85e\u2028f", ["a\x0bb\x0cc\x1cd\x85e\u2028f"]),
    ],
)
def test_heredoc_lines_split_on_lf_only(text: str, lines: list[str]) -> None:
    assert authorize.heredoc_lines(text) == lines


@pytest.mark.parametrize(
    ("text", "count"), [("", 0), ("\n", 0), ("a", 1), ("a\n", 1), ("a\n ", 2), ("\r", 1), ("a\n\nb", 2)]
)
def test_count_nonempty_lines_is_grep_c_dot(text: str, count: int) -> None:
    assert authorize.count_nonempty_lines(text) == count


@pytest.mark.parametrize(
    ("stdout", "want"),
    [
        (b"write\n", "write"),
        (b"write\n\n\n", "write"),
        (b"write\r\n", "write\r"),
        (b" write \n", " write "),
        (b"\nwrite", "\nwrite"),
        (b"wr\0ite\n", "wr\0ite"),  # bash drops the NUL; it must not match
        (b"write\xff", "write\ufffd"),
    ],
)
def test_command_output_strips_trailing_newlines_only(stdout: bytes, want: str) -> None:
    assert authorize.command_output(stdout) == want


@pytest.mark.parametrize(
    ("text", "fields"),
    [
        (f"{HEAD}\t{BASE}", (HEAD, BASE)),
        (f"  {HEAD} \t {BASE}  ", (HEAD, BASE)),
        (f"{HEAD}\t{BASE}\tx", (HEAD, f"{BASE}\tx")),
        (f"{HEAD}\t{BASE}\nx\ty", (HEAD, BASE)),
        (f"{HEAD}\r\t{BASE}", (f"{HEAD}\r", BASE)),
        (f"{HEAD}\t{BASE}\r", (HEAD, f"{BASE}\r")),
        (f"{HEAD}\x0b{BASE}", (f"{HEAD}\x0b{BASE}", "")),
        (f"{HEAD}", (HEAD, "")),
        (f"\n{HEAD}\t{BASE}", ("", "")),
        ("", ("", "")),
    ],
)
def test_read_two_fields_is_bash_read(text: str, fields: tuple[str, str]) -> None:
    assert authorize.read_two_fields(text) == fields


# --- Validators -------------------------------------------------------------------------

GOOD_PATHS = ["AGENTS.md", "src/pkg/AGENTS.md", "_x", ".github/x.md", "x" * 200, "a.b..md"[:3]]
BAD_PATHS = [
    "",
    "/etc/passwd",
    "../x",
    "a..b",
    "docs/../x",
    "a//b",
    "./a",
    "a/./b",
    "a/.",
    ".",
    "..",
    "a/",
    "-rf",
    "a b",
    "*.md",
    ":(glob)x",
    "x" * 201,
    "a\nb",
    "a\r",
    "\u00e9",
    "\uff41",
    "a?ref=main",
    "x%2f.md",
]


@pytest.mark.parametrize("entry", GOOD_PATHS)
def test_check_path_accepts(entry: str) -> None:
    authorize.check_path("rule_files", entry, False)


@pytest.mark.parametrize("entry", BAD_PATHS)
def test_check_path_rejects(entry: str) -> None:
    with pytest.raises(Fail, match=re.escape(f"Invalid entry in rule_files: '{entry}'.")):
        authorize.check_path("rule_files", entry, False)


@pytest.mark.parametrize("entry", ["*.md", "docs/", "docs/**/*.mdc", "**/AGENTS.md", "*"])
def test_check_path_glob_accepts(entry: str) -> None:
    authorize.check_path("instruction_files", entry, True)


@pytest.mark.parametrize("entry", ["docs//", "/docs/", "a/./b/", "../x/", "docs/**/../x", "/", "a?", "[a]"])
def test_check_path_glob_rejects(entry: str) -> None:
    with pytest.raises(Fail, match="Invalid entry in instruction_files"):
        authorize.check_path("instruction_files", entry, True)


def test_check_list_skips_blanks_and_comments_and_trims() -> None:
    authorize.check_list("rule_files", "# a comment\n\n  AGENTS.md  \r\nsrc/AGENTS.md\n \t\n  # x/../y", False)
    with pytest.raises(Fail, match=re.escape("'../y'")):
        authorize.check_list("rule_files", "AGENTS.md\n../y", False)
    with pytest.raises(Fail, match=re.escape("'\u3000AGENTS.md'")):
        authorize.check_list("rule_files", "\u3000AGENTS.md", False)


@pytest.mark.parametrize("text", ["", "a.md", "  a.md\n", "\n\na.md\r\n", "\n\ta.md \n"])
def test_check_addendum_accepts(text: str) -> None:
    authorize.check_addendum(text)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("a.md\nb.md", "takes one path"),
        ("a.md\n ", "takes one path"),
        ("a.md\n\r", "takes one path"),
        ("\r\na.md", "takes one path"),  # the CR is a character on its own line
        (" ", "Invalid entry in prompt_addendum: ''"),
        ("\n", "Invalid entry in prompt_addendum: ''"),
        ("# comment", "Invalid entry in prompt_addendum"),
        ("#../../user", "Invalid entry in prompt_addendum"),
    ],
)
def test_check_addendum_rejects(text: str, message: str) -> None:
    with pytest.raises(Fail, match=re.escape(message)):
        authorize.check_addendum(text)


@pytest.mark.parametrize(("value", "maximum"), [("1", 30), ("30", 30), ("9", 100), ("100", 100)])
def test_check_cap_accepts(value: str, maximum: int) -> None:
    authorize.check_cap("requests_per_minute", value, maximum)


@pytest.mark.parametrize(
    "value",
    [
        *["0", "31", "01", "00", "-1", "+1", "1.0", "1e1", "1_0", "0x1", " 5", "5 ", "5\n", "", "10000", "99999"],
        *["\uff11", "\u0661", "1\uff11", "\u00b2", "\U0001d7cf"],
    ],
)
def test_check_cap_rejects_what_int_accepts(value: str) -> None:
    with pytest.raises(Fail, match=re.escape(f"Input requests_per_minute must be an integer in 1..30, got '{value}'.")):
        authorize.check_cap("requests_per_minute", value, 30)


def test_key_mode() -> None:
    assert authorize.key_mode("stub") is False
    assert authorize.key_mode("opencode") is True
    assert authorize.key_mode("pi") is True
    with pytest.raises(Fail, match="No harness given"):
        authorize.key_mode("")
    for harness in ("Stub", "stub ", "pi\n", "memdoor"):
        with pytest.raises(Fail, match="Unknown harness"):
            authorize.key_mode(harness)


def test_check_shared_key() -> None:
    authorize.check_shared_key("false")
    with pytest.raises(Fail, match="readable in a job without an environment"):
        authorize.check_shared_key("true")
    for flag in ("", "True", "false ", "0"):
        with pytest.raises(Fail, match="Invalid HAS_SHARED_KEY"):
            authorize.check_shared_key(flag)


def test_choose_model() -> None:
    assert authorize.choose_model(False, "anything at all", "stub") == "none"
    assert authorize.choose_model(True, "zai-org/GLM-5.3-Flash", "pi") == "zai-org/GLM-5.3-Flash"
    assert authorize.choose_model(True, "a" * 128, "pi") == "a" * 128
    with pytest.raises(Fail, match="No model given for harness 'pi'"):
        authorize.choose_model(True, "", "pi")
    for model in ("a" * 129, "-m", "/m", "a b", "m\n", "\u00e9"):
        with pytest.raises(Fail, match="Invalid model id"):
            authorize.choose_model(True, model, "pi")


def test_parse_api_bases_is_grep_v() -> None:
    text = f"# comment\n\n{CSCS}\n #not a comment\nhttps://b/v1\r\nlast"
    assert authorize.parse_api_bases(text) == [CSCS, " #not a comment", "https://b/v1\r", "last"]
    assert authorize.parse_api_bases("") == []
    assert authorize.parse_api_bases("\n# x\n") == []


def test_choose_api_base_is_an_exact_entry() -> None:
    allowed = [CSCS, "https://b/v1"]
    assert authorize.choose_api_base("", allowed) == CSCS
    assert authorize.choose_api_base("https://b/v1", allowed) == "https://b/v1"
    for value in (f"{CSCS}/", f" {CSCS}", CSCS.upper(), f"{CSCS}\n", "#" + CSCS):
        with pytest.raises(Fail, match="is not an allowed inference endpoint"):
            authorize.choose_api_base(value, allowed)
    with pytest.raises(Fail, match="No allowed inference endpoint"):
        authorize.choose_api_base(CSCS, [])


def test_the_real_api_bases_file() -> None:
    assert authorize.load_api_bases(str(AR_DIR)) == [CSCS]
    assert authorize.load_api_bases("/nonexistent") == []


@pytest.mark.parametrize(
    ("pattern", "good", "bad"),
    [
        (authorize.LABEL, ["agent-review", "needs review: agent", "a" * 50], [" a", "a" * 51, "a\\b", "a\tb", ""]),
        (authorize.ENVIRONMENT, ["agent-review", "a" * 64], ["-x", "a b", "a" * 65, "a/b"]),
        (
            authorize.ACTOR,
            ["actor", "dependabot[bot]", "a" * 39, "a" * 39 + "[bot]"],
            ["a" * 40, "-x", "a_b", "[bot]", "a[bot]x", "a[bot][bot]", "a/b", "\u00e9"],
        ),
    ],
)
def test_patterns(pattern: re.Pattern[str], good: list[str], bad: list[str]) -> None:
    assert all(validate.matches(pattern, v) for v in good)
    assert not any(validate.matches(pattern, v) for v in bad)
    assert not any(validate.matches(pattern, v + "\n") for v in good)


# --- The decision, with fake lookups ------------------------------------------------------


def dispatch_env(**overrides: str) -> dict[str, str]:
    env = dict.fromkeys(authorize.VARIABLES, "")
    env.update(
        REPO="example-org/example",
        EVENT="workflow_dispatch",
        ACTOR="actor",
        IN_PR="7",
        IN_HARNESS="opencode",
        IN_MODEL="m",
        IN_DRY_RUN="false",
        IN_LABEL="agent-review",
        IN_RULES="AGENTS.md",
        IN_API_BASE=CSCS,
        IN_RPH="100",
        IN_RPM="30",
        IN_TURNS="80",
        IN_TIMEOUT="20",
        IN_ENVIRONMENT="agent-review",
        TRUSTED_REPO="GridTools/ci-workflows",
        TRUSTED_SHA="c" * 40,
        HAS_SHARED_KEY="false",
    )
    env.update(overrides)
    return env


class Lookups:
    def __init__(self, shas: tuple[str, str] = (HEAD, BASE), level: str = "write") -> None:
        self.shas, self.level = shas, level
        self.calls: list[tuple[str, str, str]] = []
        self.notices: list[str] = []

    def pr_shas(self, repo: str, number: str) -> tuple[str, str]:
        self.calls.append(("pr", repo, number))
        return self.shas

    def permission(self, repo: str, actor: str) -> str:
        self.calls.append(("permission", repo, actor))
        return self.level

    def decide(self, env: dict[str, str]) -> dict[str, str]:
        return dict(authorize.decide(env, lambda: [CSCS], self.notices.append, self.pr_shas, self.permission))


def test_decide_dispatch() -> None:
    lookups = Lookups()
    out = lookups.decide(dispatch_env())
    assert re.fullmatch(r"[0-9a-f]{16}", out.pop("call_id"))
    assert out == {
        "allowed": "true",
        "keyed": "true",
        "pr": "7",
        "head_sha": HEAD,
        "base_sha": BASE,
        "harness": "opencode",
        "model": "m",
        "dry_run": "false",
        "project": "example",
        "api_base": CSCS,
        "source_repo": "example-org/example",
        "trusted_repo": "GridTools/ci-workflows",
        "trusted_sha": "c" * 40,
    }
    assert lookups.calls == [("pr", "example-org/example", "7"), ("permission", "example-org/example", "actor")]


def test_static_inputs_fail_before_any_lookup() -> None:
    lookups = Lookups()
    with pytest.raises(Fail, match="Invalid environment"):
        lookups.decide(dispatch_env(IN_ENVIRONMENT="-x"))
    assert lookups.calls == []


def test_a_denied_writer_skips_before_the_shas_are_checked() -> None:
    # bash checked the writer first, then the pinned shas: a bad sha from a
    # denied actor is a skip, not an error.
    lookups = Lookups(shas=("main", ""), level="read")
    with pytest.raises(Skip, match="actor has 'read' permission; skipping"):
        lookups.decide(dispatch_env())
    lookups = Lookups(shas=("main", ""))
    with pytest.raises(Fail, match="Invalid head sha 'main'"):
        lookups.decide(dispatch_env())


@pytest.mark.parametrize("level", ["read", "triage", "maintain", "none", "", " write", "write\r", "Write", "wr\0ite"])
def test_only_admin_and_write_pass(level: str) -> None:
    with pytest.raises(Skip, match="permission; skipping"):
        Lookups(level=level).decide(dispatch_env())


def test_pull_request_target_actions() -> None:
    base = dispatch_env(
        EVENT="pull_request_target",
        EVENT_PR="12",
        EVENT_HEAD_SHA=HEAD,
        EVENT_BASE_SHA=BASE,
        EVENT_DRAFT="false",
        AUTHOR="author",
        SENDER="sender",
        IN_PR="",
    )
    lookups = Lookups()
    assert lookups.decide({**base, "ACTION": "labeled", "EVENT_LABEL": "agent-review"})["pr"] == "12"
    assert lookups.calls == [("permission", "example-org/example", "sender")]
    for action in authorize.PR_ACTIONS_BY_AUTHOR:
        lookups = Lookups()
        lookups.decide({**base, "ACTION": action})
        assert lookups.calls == [("permission", "example-org/example", "author")]
    with pytest.raises(Skip, match="Label 'agent-review ' does not request a review"):
        Lookups().decide({**base, "ACTION": "labeled", "EVENT_LABEL": "agent-review "})
    with pytest.raises(Skip, match="Draft pull request"):
        Lookups().decide({**base, "ACTION": "opened", "EVENT_DRAFT": "true"})
    for action in ("synchronize", "", "labeled ", "Opened"):
        with pytest.raises(Skip, match="does not start a review"):
            Lookups().decide({**base, "ACTION": action})


def test_stub_events() -> None:
    env = dispatch_env(EVENT="pull_request", IN_HARNESS="stub", IN_MODEL="", IN_PR="", EVENT_PR="12")
    lookups = Lookups()
    out = lookups.decide(env)
    assert (out["dry_run"], out["keyed"], out["model"], out["pr"]) == ("true", "false", "none", "12")
    assert [c[0] for c in lookups.calls] == ["pr"]
    assert any("Caller test mode" in n for n in lookups.notices)
    with pytest.raises(Fail, match="runs the 'stub' harness only, not 'pi'"):
        Lookups().decide({**env, "IN_HARNESS": "pi", "IN_MODEL": "m"})
    for event in ("push", "schedule"):
        with pytest.raises(Fail, match=f"Event '{event}' is for the self-test of GridTools/ci-workflows only"):
            Lookups().decide({**env, "EVENT": event})
        out = Lookups().decide(
            {**env, "EVENT": event, "REPO": "GridTools/ci-workflows", "IN_TEST_REPO": "GridTools/gt4py"}
        )
        assert (out["source_repo"], out["dry_run"]) == ("GridTools/gt4py", "true")
    for event in ("", "issue_comment", "PUSH"):
        with pytest.raises(Fail, match="Unexpected event"):
            Lookups().decide({**env, "EVENT": event})


def test_test_repository_rule() -> None:
    central = {"REPO": "GridTools/ci-workflows", "IN_TEST_REPO": "GridTools/gt4py"}
    with pytest.raises(Fail, match="is for the self-test"):
        Lookups().decide(dispatch_env(IN_TEST_REPO="GridTools/gt4py"))
    with pytest.raises(Fail, match="is for the self-test"):
        Lookups().decide(dispatch_env(**central))  # keyed
    out = Lookups().decide(dispatch_env(IN_HARNESS="stub", **central))
    assert (out["source_repo"], out["dry_run"]) == ("GridTools/gt4py", "true")
    with pytest.raises(Fail, match="Invalid test_repository"):
        Lookups().decide(dispatch_env(IN_HARNESS="stub", REPO="GridTools/ci-workflows", IN_TEST_REPO="a/b/c"))


# --- Output and log helpers -----------------------------------------------------------------


def test_shown_breaks_up_legacy_commands_and_shows_nul() -> None:
    assert authorize.shown("Invalid label '##[error]x'") == "Invalid label '#\u200b#[error]x'"
    assert authorize.shown("'wr\0ite'") == "'wr\\x00ite'"


def test_write_outputs(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "out"
    authorize.write_outputs(str(out), [("allowed", "true"), ("pr", "7")])
    assert out.read_bytes() == b"allowed=true\npr=7\n"
    for bad in ("a\nb", "a\rb"):
        with pytest.raises(ValueError, match="line break"):
            authorize.write_outputs(str(out), [("allowed", "true"), ("x", bad)])
    assert out.read_bytes() == b"allowed=true\npr=7\n"  # nothing half-written
    with pytest.raises(ValueError, match="absolute"):
        authorize.write_outputs("out", [("allowed", "false")])


# --- The entry point ---------------------------------------------------------------------------

FAKE_GH = """#!/usr/bin/bash
echo "gh $*" >>"${FAKE_DIR}/gh.log"
case "$2" in
  */pulls/*) printf '%s\\t%s\\n' "${FAKE_HEAD}" "${FAKE_BASE}" ;;
  */collaborators/*/permission) printf -- "${FAKE_PERM}\\n" ;;
  *) exit 1 ;;
esac
"""
PLANTED = ["json.py", "os.py", "re.py", "secrets.py", "sitecustomize.py", "agent_review/__init__.py"]


def run_entry(tmp_path: pathlib.Path, env: dict[str, str], cwd: pathlib.Path) -> subprocess.CompletedProcess[bytes]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    (fakebin / "gh").write_text(FAKE_GH)
    (fakebin / "gh").chmod(0o755)
    full = {
        **dispatch_env(),
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "GITHUB_OUTPUT": str(tmp_path / "out"),
        "FAKE_DIR": str(tmp_path),
        "FAKE_HEAD": HEAD,
        "FAKE_BASE": BASE,
        "FAKE_PERM": "write",
        **env,
    }
    return subprocess.run(
        ["/usr/bin/python3", "-I", "-S", "-B", str(AR_DIR / "bin" / "authorize")],
        env=full,
        cwd=cwd,
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_entry_point_allows_in_a_hostile_environment(tmp_path: pathlib.Path) -> None:
    markers = tmp_path / "markers"
    markers.mkdir()
    cwd = tmp_path / "cwd"
    for rel in PLANTED:
        path = cwd / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"open({str(markers / rel.replace('/', '-'))!r}, 'w').close()\n")
    hostile = {
        "PYTHONPATH": str(cwd),
        "PYTHONSTARTUP": str(cwd / "json.py"),
        "PYTHONHOME": str(cwd),
        "PYTHONINSPECT": "1",
    }
    result = run_entry(tmp_path, hostile, cwd)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == b""
    assert not list(markers.iterdir())
    out = (tmp_path / "out").read_text()
    assert out.startswith("allowed=true\nkeyed=true\npr=7\n")
    assert (tmp_path / "gh.log").read_text().splitlines() == [
        "gh api repos/example-org/example/pulls/7 --jq [.head.sha, .base.sha] | @tsv",
        "gh api repos/example-org/example/collaborators/actor/permission --jq .permission",
    ]


@pytest.mark.parametrize(
    ("env", "rc", "log"),
    [
        # Values reach the log escaped: they cannot end the command.
        ({"IN_MODEL": "m\n::warning::x%0A"}, 1, b"::error::Invalid model id 'm%0A::warning::x%250A'."),
        ({"IN_LABEL": "x\r::error::y"}, 1, b"::error::Invalid label 'x%0D::error::y'."),
        ({"IN_LABEL": "##[warning]x"}, 1, "::error::Invalid label '#\u200b#[warning]x'.".encode()),
        ({"FAKE_PERM": "wr\\0ite", "FAKE_HEAD": HEAD}, 0, b"::notice::actor has 'wr\\x00ite' permission; skipping"),
        ({"IN_MODEL": "\udcff"}, 1, b"::error::Variable IN_MODEL is not valid UTF-8."),
        # 'read' skips the leading tab: the base sha becomes the head.
        ({"FAKE_HEAD": ""}, 1, b"::error::Invalid base sha ''."),
        ({"FAKE_HEAD": "main"}, 1, b"::error::Invalid head sha 'main'."),
        ({"PATH": "/nonexistent"}, 1, b"::error::Could not resolve pull request example-org/example#7: 'gh: not found"),
    ],
)
def test_entry_point_log_and_exit_codes(tmp_path: pathlib.Path, env: dict[str, str], rc: int, log: bytes) -> None:
    result = run_entry(tmp_path, env, tmp_path)
    assert result.returncode == rc, result.stdout
    assert result.stderr == b""
    assert log in result.stdout
    assert (tmp_path / "out").read_text() == "allowed=false\n"


def test_entry_point_without_github_output(tmp_path: pathlib.Path) -> None:
    result = run_entry(tmp_path, {"GITHUB_OUTPUT": ""}, tmp_path)
    assert result.returncode == 1
    assert b"::error::GITHUB_OUTPUT is not set." in result.stdout
    assert not (tmp_path / "out").exists()


def test_entry_point_with_an_unwritable_output_fails(tmp_path: pathlib.Path) -> None:
    (tmp_path / "target").write_text("")
    os.symlink(tmp_path / "target", tmp_path / "out")
    result = run_entry(tmp_path, {}, tmp_path)
    assert result.returncode == 1
    assert b"::error::" in result.stdout
    assert (tmp_path / "target").read_text() == ""  # the symlink is not followed
