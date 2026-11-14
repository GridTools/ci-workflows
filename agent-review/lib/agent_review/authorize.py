"""The 'authorize' job of the agent review: ``bin/authorize``.

Validates the caller's inputs, decides whether this event may run a review
and with which key mode, checks the actor's permission and pins the pull
request's head and base sha.

Runs on the host, before anything from the pull request is read, in the
trusted-host zone: ``gh`` (with ``GH_TOKEN``) is found on ``PATH`` and
inherits the environment; only ``agent_review.gh`` runs it, with ``--jq``
projections, so this module never parses a pull request object
(invariant 2). It writes its outputs to ``$GITHUB_OUTPUT``. It fails
closed: an invalid input or an API failure is an error (``allowed=false``,
exit 1); a denied actor or an event that is not for us is a notice
(``allowed=false``, exit 0).

Environment (all from the workflow; ``IN_*`` are the caller's inputs, which
come from the caller's workflow file); an unset variable is the same as an
empty one, as in bash's ``${!v-}``:

- ``REPO EVENT ACTION AUTHOR SENDER ACTOR EVENT_PR EVENT_HEAD_SHA
  EVENT_BASE_SHA EVENT_DRAFT EVENT_LABEL``: the github context and the event
  payload;
- ``IN_PR IN_HARNESS IN_MODEL IN_DRY_RUN IN_LABEL IN_PROJECT IN_RULES
  IN_PATTERNS IN_ADDENDUM IN_API_BASE IN_RPH IN_RPM IN_TURNS IN_TIMEOUT
  IN_ENVIRONMENT IN_TEST_REPO``: the caller's inputs;
- ``TRUSTED_REPO TRUSTED_SHA``: ``job.workflow_repository`` and ``_sha``;
- ``CALLER_WF_SHA``: the caller's ``github.workflow_sha``;
- ``HAS_SHARED_KEY``: ``true`` if ``secrets.inference_api_key`` is non-empty
  in this job, which declares no environment.

Event and harness matrix ('keyed' harnesses call a model with the key):

- ``pull_request_target``: any harness; actions ``labeled`` (with the
  configured label), ``opened``, ``reopened``, ``ready_for_review``; writer
  check on the labeller or the author;
- ``workflow_dispatch``: any harness; writer check on the actor;
- ``pull_request``: ``stub`` only, forced dry run, no writer check: the
  caller test mode (any caller) and the self-test;
- ``push``, ``schedule``: ``stub`` only, forced dry run, no writer check;
  the self-test of the workflow's own repository only.

Ported from bash; the checks run in the bash order, so the same input
gives the same first error. Where bash depended on the locale or on shell
quirks, this module takes the stricter reading (docs/agent-review.md,
"Inputs"): ASCII-only classes and whitespace (bash's C locale), values that
are not UTF-8 are rejected, and ``gh`` output is compared byte for byte
(no NUL dropped, nothing on stderr).
"""

import os
import re
import secrets
from collections.abc import Callable, Mapping, Sequence

from agent_review import files, gh, runner, validate, workflow

DOCS = "See https://github.com/GridTools/ci-workflows/blob/main/docs/agent-review.md#inputs."
VARIABLES = (
    "REPO",
    "EVENT",
    "ACTION",
    "AUTHOR",
    "SENDER",
    "ACTOR",
    "EVENT_PR",
    "EVENT_HEAD_SHA",
    "EVENT_BASE_SHA",
    "EVENT_DRAFT",
    "EVENT_LABEL",
    "IN_PR",
    "IN_HARNESS",
    "IN_MODEL",
    "IN_DRY_RUN",
    "IN_LABEL",
    "IN_PROJECT",
    "IN_RULES",
    "IN_PATTERNS",
    "IN_ADDENDUM",
    "IN_API_BASE",
    "IN_RPH",
    "IN_RPM",
    "IN_TURNS",
    "IN_TIMEOUT",
    "IN_ENVIRONMENT",
    "IN_TEST_REPO",
    "TRUSTED_REPO",
    "TRUSTED_SHA",
    "CALLER_WF_SHA",
    "HAS_SHARED_KEY",
)

# The harness allowlist decides whether the run is keyed: only keyed runs
# get the job with the caller's environment, and so the key.
KEYLESS_HARNESSES = ("stub",)
KEYED_HARNESSES = ("opencode", "pi")

# Ceilings: a caller can lower the caps, never raise them. The review job
# also clamps them to agent-review/awf.yaml. (input name, variable, maximum)
CAPS = (
    ("requests_per_hour", "IN_RPH", 100),
    ("requests_per_minute", "IN_RPM", 30),
    ("max_turns", "IN_TURNS", 80),
    ("harness_timeout_minutes", "IN_TIMEOUT", 20),
)

PR_ACTIONS_BY_AUTHOR = ("opened", "reopened", "ready_for_review")
STUB_EVENTS = ("pull_request", "push", "schedule")
WRITER_LEVELS = ("admin", "write")

# bash's [:space:] in the C locale. Never str.strip() without arguments, which
# also removes Unicode spaces and \x1c-\x1f.
SPACE = " \t\n\v\f\r"
# The separators of 'read' with the default IFS, within one line.
IFS_BLANKS = " \t"
API_BASES_FILE = "api-bases.txt"
API_BASES_MAX_BYTES = 64 * 1024

# The bash patterns, applied with fullmatch and ASCII classes.
# ^[A-Za-z0-9][A-Za-z0-9\ ._:/-]{0,49}$
LABEL = validate.pattern(r"[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,49}")
# ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$
ENVIRONMENT = validate.pattern(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# ^[1-9][0-9]{0,3}$
CAP = validate.pattern(r"[1-9][0-9]{0,3}")
# ^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$
ACTOR = validate.pattern(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?")
# ^[A-Za-z0-9_.][A-Za-z0-9_./-]{0,199}$ and, for patterns,
# ^[A-Za-z0-9_.*][A-Za-z0-9_.*/-]{0,199}$
PLAIN_PATH = validate.pattern(r"[A-Za-z0-9_.][A-Za-z0-9_./-]{0,199}")
GLOB_PATH = validate.pattern(r"[A-Za-z0-9_.*][A-Za-z0-9_.*/-]{0,199}")
REPO = validate.REPO
SHA = validate.SHA
PR_NUMBER = validate.POSITIVE_INT
MODEL = validate.MODEL
PROJECT = validate.PROJECT

PR_SHAS_JQ = "[.head.sha, .base.sha] | @tsv"
PERMISSION_JQ = ".permission"


class Fail(Exception):
    """An invalid input or an API failure: an error, exit 1."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Skip(Exception):
    """Not for us, or a denied actor: a notice, exit 0."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# --- Reading the environment ---------------------------------------------------


def read_environment(environb: Mapping[bytes, bytes]) -> dict[str, str]:
    """The variables of VARIABLES as strict UTF-8; unset is empty (``${!v-}``).

    Bash compared bytes; values that are not UTF-8 cannot come from the
    workflow (its contexts are JSON strings), so they are rejected here.
    """
    env = {}
    for name in VARIABLES:
        raw = environb.get(name.encode("ascii"), b"")
        try:
            env[name] = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise Fail(f"Variable {name} is not valid UTF-8.") from None
    return env


# --- Shell semantics, emulated exactly -------------------------------------------


def trim(text: str) -> str:
    """``${v#"${v%%[![:space:]]*}"}`` and its mirror, in the C locale."""
    return text.strip(SPACE)


def heredoc_lines(text: str) -> list[str]:
    """The lines ``while IFS= read -r line; do ...; done <<<"$text"`` reads.

    The here-string appends a newline, so every line is read, the last one
    too; only LF separates lines (a CR stays part of its line).
    """
    return text.split("\n")


def count_nonempty_lines(text: str) -> int:
    """``grep -c . <<<"$text"``: lines with at least one character."""
    return sum(1 for line in text.split("\n") if line)


def command_output(stdout: bytes) -> str:
    """``$(...)`` of a command's stdout: trailing newlines removed.

    Unlike bash, NUL bytes are kept (bash drops them with a warning), so a
    value with a NUL never compares equal to a valid one. Bytes that are not
    UTF-8 become U+FFFD, which no pattern or allowed value contains.
    """
    return stdout.decode("utf-8", errors="replace").rstrip("\n")


def read_two_fields(text: str) -> tuple[str, str]:
    """``read -r a b <<<"$text"`` with the default IFS.

    The first line only; leading and trailing blanks (space, tab) removed;
    ``a`` is the first field, ``b`` the rest of the line. Other whitespace
    (CR, VT, FF) is not a separator and stays in the fields.
    """
    line = text.split("\n", 1)[0].strip(IFS_BLANKS)
    m = re.fullmatch(r"([^ \t]*)[ \t]*(.*)", line, re.ASCII | re.DOTALL)
    assert m is not None  # noqa: S101 (the pattern matches every string)
    return m.group(1), m.group(2)


# --- Validators ----------------------------------------------------------------


def check_path(name: str, entry: str, glob: bool) -> None:
    """One repository path or pattern.

    No absolute path, no '..' anywhere (as 'run-review' checks), no empty or
    '.' segment. A pattern may end in '/' (a directory, see
    instruction-files.txt); a path may not.
    """
    compiled = GLOB_PATH if glob else PLAIN_PATH
    stem = entry[:-1] if glob and entry.endswith("/") else entry
    wrapped = f"/{stem}/"
    if not validate.matches(compiled, entry) or ".." in wrapped or "/./" in wrapped or "//" in wrapped:
        raise Fail(f"Invalid entry in {name}: '{entry}'.")


def check_list(name: str, text: str, glob: bool) -> None:
    """A list, one entry per line; blank lines and '#' comments are skipped,
    as 'run-review' reads these lists."""
    for line in heredoc_lines(text):
        entry = trim(line)
        if entry == "" or entry.startswith("#"):
            continue
        check_path(name, entry, glob)


def check_addendum(text: str) -> None:
    """One path, no comment syntax: the value becomes part of an API path."""
    if text == "":
        return
    if count_nonempty_lines(text) > 1:
        raise Fail("Input prompt_addendum takes one path.")
    check_path("prompt_addendum", trim(text), False)


def check_cap(name: str, value: str, maximum: int) -> None:
    """An integer in 1..maximum; validated by pattern before int()."""
    if not validate.matches(CAP, value) or int(value, 10) > maximum:
        raise Fail(f"Input {name} must be an integer in 1..{maximum}, got '{value}'.")


def key_mode(harness: str) -> bool:
    """True if the harness is keyed (calls a model with the key)."""
    if harness in KEYLESS_HARNESSES:
        return False
    if harness in KEYED_HARNESSES:
        return True
    if harness == "":
        raise Fail("No harness given (input 'harness').")
    raise Fail(f"Unknown harness '{harness}'.")


def check_shared_key(flag: str) -> None:
    """The key must be an environment secret.

    Here, in a job without an environment, the caller's mapping yields a
    repository or organization secret of that name, which every job of this
    workflow would then receive.
    """
    if flag == "false":
        return
    if flag == "true":
        raise Fail(
            "Secret 'inference_api_key' is readable in a job without an environment: the caller maps a repository"
            " or organization secret. Map it only from the environment's own secret, and delete the shared one."
        )
    raise Fail(f"Invalid HAS_SHARED_KEY '{flag}'.")


def choose_model(keyed: bool, model: str, harness: str) -> str:
    """The stub calls no model. The others have no default model on purpose."""
    if not keyed:
        return "none"
    if model == "":
        raise Fail(f"No model given for harness '{harness}' (input 'model').")
    if not validate.matches(MODEL, model):
        raise Fail(f"Invalid model id '{model}'.")
    return model


def parse_api_bases(text: str) -> list[str]:
    """``grep -v -e '^#' -e '^$'``, one entry per line."""
    return [line for line in text.split("\n") if line != "" and not line.startswith("#")]


def choose_api_base(value: str, allowed: Sequence[str]) -> str:
    """Where the key is sent: an exact entry of the central allowlist."""
    if not allowed:
        raise Fail("No allowed inference endpoint in api-bases.txt.")
    api_base = value or allowed[0]
    if api_base not in allowed:
        raise Fail(f"Input api_base '{api_base}' is not an allowed inference endpoint (agent-review/api-bases.txt).")
    return api_base


# --- GitHub lookups --------------------------------------------------------------


def _gh_message(e: Exception) -> str:
    """What bash's '$(gh ... 2>&1)' showed of a failure, as far as known."""
    if isinstance(e, runner.CommandError) and e.stderr:
        return command_output(e.stderr)
    return str(e)


def lookup_pr_shas(repo: str, number: str) -> tuple[str, str]:
    """(head sha, base sha) of a pull request, unvalidated."""
    try:
        out = gh.api(f"repos/{repo}/pulls/{number}", jq=PR_SHAS_JQ, stderr_is_error=True)
    except (runner.CommandError, ValueError) as e:
        raise Fail(f"Could not resolve pull request {repo}#{number}: '{_gh_message(e)}'.") from None
    return read_two_fields(command_output(out))


def lookup_permission(repo: str, actor: str) -> str:
    """The actor's 'permission' field, unvalidated."""
    try:
        out = gh.api(f"repos/{repo}/collaborators/{actor}/permission", jq=PERMISSION_JQ, stderr_is_error=True)
    except (runner.CommandError, ValueError) as e:
        raise Fail(f"Could not resolve repository permission for '{actor}', denying: '{_gh_message(e)}'.") from None
    return command_output(out)


# --- The decision ----------------------------------------------------------------


def decide(
    env: Mapping[str, str],
    api_bases: Callable[[], list[str]],
    notice: Callable[[str], None],
    pr_shas: Callable[[str, str], tuple[str, str]] = lookup_pr_shas,
    permission: Callable[[str, str], str] = lookup_permission,
) -> list[tuple[str, str]]:
    """The outputs of an allowed run, in order; raises Fail or Skip.

    The checks run in the order of the bash script, so that the same input
    stops at the same check.
    """
    # This workflow's own commit, from which every trusted file is read.
    trusted_repo, trusted_sha, repo = env["TRUSTED_REPO"], env["TRUSTED_SHA"], env["REPO"]
    if not validate.matches(REPO, trusted_repo):
        raise Fail(f"No job.workflow_repository ('{trusted_repo}').")
    if not validate.matches(SHA, trusted_sha):
        raise Fail(f"No job.workflow_sha ('{trusted_sha}').")
    if not validate.matches(REPO, repo):
        raise Fail(f"Invalid repository '{repo}'.")
    notice(f"Agent review workflow {trusted_repo}@{trusted_sha}; caller {repo}@{env['CALLER_WF_SHA']}.")

    # Harness and key mode.
    harness = env["IN_HARNESS"]
    keyed = key_mode(harness)
    check_shared_key(env["HAS_SHARED_KEY"])

    # Static inputs, validated before any API call, so a misconfigured
    # caller fails visibly.
    label = env["IN_LABEL"]
    if not validate.matches(LABEL, label):
        raise Fail(f"Invalid label '{label}'.")
    if env["IN_DRY_RUN"] not in ("true", "false"):
        raise Fail(f"Invalid dry-run flag '{env['IN_DRY_RUN']}'.")
    model = choose_model(keyed, env["IN_MODEL"], harness)
    project = env["IN_PROJECT"] or repo.split("/", 1)[1]
    if not validate.matches(PROJECT, project):
        raise Fail(f"Invalid project name '{project}'.")
    check_list("rule_files", env["IN_RULES"], False)
    check_list("instruction_files", env["IN_PATTERNS"], True)
    check_addendum(env["IN_ADDENDUM"])
    api_base = choose_api_base(env["IN_API_BASE"], api_bases())
    if not validate.matches(ENVIRONMENT, env["IN_ENVIRONMENT"]):
        raise Fail(f"Invalid environment '{env['IN_ENVIRONMENT']}'.")
    for name, variable, maximum in CAPS:
        check_cap(name, env[variable], maximum)

    # The self-test: this repository calling its own workflow with the stub.
    self_test = trusted_repo == repo and not keyed

    # The repository whose pull request is reviewed: the caller's, or
    # (self-test only) another public repository, read-only.
    event = env["EVENT"]
    source_repo = repo
    test_repo = env["IN_TEST_REPO"]
    if test_repo != "":
        if not self_test:
            raise Fail(f"Input test_repository is for the self-test of {trusted_repo} with the 'stub' harness only.")
        if event == "pull_request_target":
            raise Fail("Input test_repository is not supported under 'pull_request_target'.")
        if not validate.matches(REPO, test_repo):
            raise Fail(f"Invalid test_repository '{test_repo}'.")
        source_repo = test_repo

    dry_run = env["IN_DRY_RUN"]
    check_writer = True
    if event == "pull_request_target":
        # Only these actions; a caller listing other 'types' (or none, which
        # means every push to a PR) gets no keyed run from them.
        action = env["ACTION"]
        if action == "labeled":
            if env["EVENT_LABEL"] != label:
                raise Skip(f"Label '{env['EVENT_LABEL']}' does not request a review.")
            actor = env["SENDER"]
        elif action in PR_ACTIONS_BY_AUTHOR:
            if env["EVENT_DRAFT"] == "true":
                raise Skip(f"Draft pull request; add the '{label}' label to request a review.")
            actor = env["AUTHOR"]
        else:
            raise Skip(
                f"Action '{action}' of 'pull_request_target' does not start a review"
                " (allowed: labeled, opened, reopened, ready_for_review)."
            )
        pr, head_sha, base_sha = env["EVENT_PR"], env["EVENT_HEAD_SHA"], env["EVENT_BASE_SHA"]
    elif event == "workflow_dispatch":
        pr = env["IN_PR"]
        head_sha, base_sha = resolve_pr(source_repo, pr, pr_shas)
        actor = env["ACTOR"]
    elif event in STUB_EVENTS:
        # Nothing to spend and nothing to post, so no writer check (fork and
        # Dependabot PRs can run it). The dry run follows from the event name.
        if keyed:
            raise Fail(f"Event '{event}' runs the 'stub' harness only, not '{harness}'.")
        if event != "pull_request":
            if not self_test:
                raise Fail(f"Event '{event}' is for the self-test of {trusted_repo} only.")
        elif not self_test:
            notice(
                "Caller test mode (event 'pull_request', harness 'stub', dry run): the caller's workflow file,"
                " inputs, rule files and prompt addendum are pull request content here. This run proves the"
                " plumbing only, never the reset of the rule files."
            )
        pr = env["IN_PR"] or env["EVENT_PR"]
        head_sha, base_sha = resolve_pr(source_repo, pr, pr_shas)
        actor = env["ACTOR"]
        check_writer = False
        dry_run = "true"
    else:
        raise Fail(f"Unexpected event '{event}'.")
    # Never comment on a pull request of the caller that merely has the same
    # number as the reviewed one.
    if source_repo != repo:
        dry_run = "true"

    if check_writer:
        if not validate.matches(ACTOR, actor):
            raise Fail(f"Invalid actor '{actor}'.")
        level = permission(repo, actor)
        # 'permission' reports the 'maintain' role as 'write', and 'triage'
        # as 'read'.
        if level not in WRITER_LEVELS:
            raise Skip(f"{actor} has '{level}' permission; skipping the agent review.")
        notice(f"{actor} has '{level}' permission; running the agent review.")
    else:
        notice(f"Event '{event}' with harness 'stub': forced dry run, no permission check.")

    if not validate.matches(PR_NUMBER, pr):
        raise Fail(f"Invalid pull request number '{pr}'.")
    if not validate.matches(SHA, head_sha):
        raise Fail(f"Invalid head sha '{head_sha}'.")
    if not validate.matches(SHA, base_sha):
        raise Fail(f"Invalid base sha '{base_sha}'.")

    keyed_flag = "true" if keyed else "false"
    return [
        ("allowed", "true"),
        ("keyed", keyed_flag),
        ("pr", pr),
        ("head_sha", head_sha),
        ("base_sha", base_sha),
        ("harness", harness),
        ("model", model),
        ("dry_run", dry_run),
        ("project", project),
        ("api_base", api_base),
        ("source_repo", source_repo),
        # Distinguishes this call's artifacts from another call's in the same run.
        ("call_id", secrets.token_hex(8)),
        ("trusted_repo", trusted_repo),
        ("trusted_sha", trusted_sha),
    ]


def resolve_pr(repo: str, number: str, pr_shas: Callable[[str, str], tuple[str, str]]) -> tuple[str, str]:
    """The pull request's (head, base) sha, after checking its number."""
    if not validate.matches(PR_NUMBER, number):
        raise Fail(f"Invalid pull request number '{number}'.")
    return pr_shas(repo, number)


def reviewing(outputs: Sequence[tuple[str, str]]) -> str:
    o = dict(outputs)
    return (
        f"Reviewing {o['source_repo']}#{o['pr']} at {o['head_sha']} with harness '{o['harness']}'"
        f" (keyed {o['keyed']}), model '{o['model']}', dry run '{o['dry_run']}'."
    )


# --- I/O ------------------------------------------------------------------------------


def trusted_root() -> str:
    """The directory holding bin/, lib/ and api-bases.txt."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def load_api_bases(root: str) -> list[str]:
    """The entries of api-bases.txt; none if it cannot be read as UTF-8."""
    try:
        text = files.read_text(os.path.join(root, API_BASES_FILE), API_BASES_MAX_BYTES)
    except (OSError, UnicodeDecodeError):
        return []
    return parse_api_bases(text)


def shown(message: str) -> str:
    """A message for the log: values may hold '##[' (the legacy command
    marker) or a NUL (from gh, shown as '\\x00'); workflow.* escapes '%', CR
    and LF."""
    return workflow.neutralise(message.replace("\0", "\\x00"))


def write_outputs(path: str, outputs: Sequence[tuple[str, str]]) -> None:
    """Appends all outputs at once; a CR or LF in a value raises ValueError."""
    if not path.startswith("/"):
        raise ValueError("GITHUB_OUTPUT is not an absolute path")
    data = "".join(workflow.output_line(name, value) for name, value in outputs)
    files.append_bytes(path, data.encode("utf-8", errors="strict"))


def main() -> int:
    output = os.environ.get("GITHUB_OUTPUT", "")
    if output == "":
        workflow.error("GITHUB_OUTPUT is not set.")
        return 1

    def notice(message: str) -> None:
        workflow.notice(shown(message))

    def write_denied() -> bool:
        """``allowed=false``; False (with an error) if it cannot be written."""
        try:
            write_outputs(output, [("allowed", "false")])
        except (OSError, ValueError) as e:
            workflow.error(shown(f"Could not write the outputs: {e}"))
            return False
        return True

    try:
        env = read_environment(os.environb)
        root = trusted_root()
        outputs = decide(env, lambda: load_api_bases(root), notice)
        write_outputs(output, outputs)
    except Skip as e:
        notice(e.message)
        return 0 if write_denied() else 1
    except Fail as e:
        write_denied()
        workflow.error(shown(f"{e.message} {DOCS}"))
        return 1
    except Exception as e:  # fail closed on anything unexpected
        write_denied()
        workflow.error(shown(f"authorize failed: {type(e).__name__}: {e}"))
        return 1
    notice(reviewing(outputs))
    return 0
