"""``bin/publish-comment``: validates the agent's review and upserts it as a PR comment.

Usage::

    publish-comment sanitize                      < review > safe-review
    publish-comment render --harness H --version V --model M --head-sha SHA --run-url U --review FILE
    publish-comment post --repo R --pr N --harness H --body-file FILE

Runs in the ``publish`` job: a fresh VM with a write-capable token, no model
and no PR code. The review is hostile text (agent_review.sanitize).
``sanitize`` and ``render`` read only stdin and files and write stdout; the
workflow runs ``render`` in a step without a token. Only ``post`` runs
``gh`` (agent_review.gh), with the token of its step.

Exit codes: 0 on success; 3 if the review is not valid UTF-8, is empty, or
its file is not a regular file (a symlink, a FIFO, ...) within the size cap
(the workflow then posts nothing and warns); 1 on any other error,
including a missing review file (the workflow checks for it first).

The comment is found by a marker on its first line, among this workflow's
own comments only (``BOT_LOGIN``), never with ``gh pr comment --edit-last``:
other tools (PR Agent) post as the same bot. The comment listing is a
``gh --jq`` projection (id, login, and the first 256 characters of the
body's first line, as TSV), so this
host never parses a comment body (invariant 2); only the first line is
compared, as bytes, with the marker.
"""

import json
import os
import sys
from collections.abc import Sequence

from agent_review import files, gh, runner, sanitize, validate, workflow

# GitHub rejects comment bodies over 65536 characters.
BODY_MAX_BYTES = 65536
# The review read by 'sanitize' and 'render'. The review job caps review.md
# at 64 KiB; the bash version had no cap at all.
INPUT_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_BOT_LOGIN = "github-actions[bot]"
EXIT_INVALID = 3

# bash: ^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$ (harness, version, model)
LABEL = validate.pattern(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}")
# bash: ^https://github\.com/[A-Za-z0-9_./-]+$
RUN_URL = validate.pattern(r"https://github\.com/[A-Za-z0-9_./-]+")
COMMENT_ID = validate.pattern(r"[1-9][0-9]{0,19}")
# The projection of the comment listing; a constant, never data. Only the
# first FIRST_LINE_CHARS characters of each first line: other users'
# comments must not be able to push the listing past runner's output cap
# (300 comments with 60 KB first lines would). Longer than any marker
# (at most 150 characters), so the comparison stays exact.
FIRST_LINE_CHARS = 256
LISTING_JQ = f'.[] | [.id, .user.login, (.body | split("\\n") | .[0] | .[0:{FIRST_LINE_CHARS}])] | @tsv'
ASCII_WHITESPACE = " \t\n\v\f\r"

RENDER_OPTIONS = ("--harness", "--version", "--model", "--head-sha", "--run-url", "--review")
POST_OPTIONS = ("--repo", "--pr", "--harness", "--body-file")


class Failure(Exception):
    """An error that ends the command with a message and an exit code."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _stderr_line(message: str) -> None:
    # One line that cannot start or contain a workflow command: it starts
    # with 'publish-comment: ', has no line break, and no '##['.
    text = message.replace("\r", "\\r").replace("\n", "\\n")
    sys.stderr.buffer.write(workflow.neutralise(f"publish-comment: {text}\n").encode("utf-8", errors="replace"))
    sys.stderr.buffer.flush()


def marker(harness: str) -> str:
    return f"<!-- agent-review:{harness} -->"


def parse_options(args: Sequence[str], known: Sequence[str]) -> dict[str, str]:
    """``--name value`` pairs, as the bash version read them; the last one wins."""
    values = dict.fromkeys(known, "")
    rest = list(args)
    while rest:
        name = rest.pop(0)
        if name not in known:
            raise Failure(f"Unknown option '{name}'.")
        if not rest:
            raise Failure(f"Option '{name}' needs a value.")
        values[name] = rest.pop(0)
    return values


def check_label(what: str, value: str) -> str:
    if not validate.matches(LABEL, value):
        raise Failure(f"Invalid {what} '{value}'.")
    return value


def sanitize_review(data: bytes) -> tuple[bytes, bool]:
    """(the sanitised review, whether the cap cut it), or Failure(code 3) if it is not valid UTF-8."""
    try:
        return sanitize.sanitize_and_report(data)
    except sanitize.InvalidReviewError as e:
        raise Failure(str(e), EXIT_INVALID) from e


def cmd_sanitize() -> bytes:
    data = sys.stdin.buffer.read(INPUT_MAX_BYTES + 1)
    if len(data) > INPUT_MAX_BYTES:
        raise Failure(f"The review exceeds {INPUT_MAX_BYTES} bytes.", EXIT_INVALID)
    return sanitize_review(data)[0]


def render(*, harness: str, version: str, model: str, head_sha: str, run_url: str, review: bytes) -> bytes:
    """The comment body for ``review`` (raw bytes), as UTF-8."""
    # The bash version took the sanitised text through '$(...)', which drops
    # trailing newlines.
    safe_bytes, cut = sanitize_review(review)
    safe = safe_bytes.rstrip(b"\n").decode("utf-8", errors="strict")
    if not safe.strip(ASCII_WHITESPACE):
        raise Failure("the review is empty.", EXIT_INVALID)
    lines = [
        marker(harness),
        "> [!NOTE]",
        f"> Advisory review by **{harness}** {version} (`{model}`) of commit {head_sha}, produced in a sandbox. "
        "Later pushes are not reviewed. Not a required check: it may be wrong, and the content of the pull request "
        f"can steer it. [Workflow run]({run_url}).",
        "",
        safe,
    ]
    body = "\n".join(lines) + "\n"
    # Noted if and only if the cap cut the sanitised text (also when escaping
    # made it grow past the cap; not when only dropped characters made the
    # input larger than the cap).
    if cut:
        body += f"\n_[Truncated to {sanitize.REVIEW_MAX_BYTES} bytes.]_\n"
    return body.encode("utf-8", errors="strict")


def cmd_render(args: Sequence[str]) -> bytes:
    o = parse_options(args, RENDER_OPTIONS)
    # These come from the workflow's trusted job outputs, never from the
    # sandbox; validated because they are interpolated into Markdown.
    harness = check_label("harness", o["--harness"])
    version = check_label("version", o["--version"])
    model = check_label("model", o["--model"])
    if not validate.matches(validate.SHA, o["--head-sha"]):
        raise Failure(f"Invalid head sha '{o['--head-sha']}'.")
    if not validate.matches(RUN_URL, o["--run-url"]):
        raise Failure(f"Invalid run URL '{o['--run-url']}'.")
    path = o["--review"]
    try:
        review = files.read_bytes(path, INPUT_MAX_BYTES) if path else None
    except FileNotFoundError:
        review = None
    except OSError as e:
        # A symlink, a FIFO, a directory or a file over the cap: an invalid
        # result (a warning), not an error (a red run); the check is advisory.
        raise Failure(f"Unusable review file '{path}': {e}", EXIT_INVALID) from e
    if review is None:
        raise Failure(f"No review file '{path}'.")
    return render(
        harness=harness, version=version, model=model, head_sha=o["--head-sha"], run_url=o["--run-url"], review=review
    )


def find_comment(listing: bytes, login: str, want: str) -> str | None:
    """The id of the first comment by ``login`` whose first line is ``want``.

    ``listing``: lines of 'id TAB login TAB first line' (jq's @tsv, which
    escapes tabs and line breaks inside fields), compared as bytes.
    """
    login_b, want_b = login.encode("utf-8"), want.encode("utf-8")
    for line in listing.split(b"\n"):
        fields = line.split(b"\t")
        if len(fields) == 3 and fields[1] == login_b and fields[2] == want_b:
            cid = fields[0].decode("ascii", errors="replace")
            if not validate.matches(COMMENT_ID, cid):
                raise Failure(f"Invalid comment id '{cid}' in the listing.")
            return cid
    return None


def _gh(
    what: str, endpoint: str, *, method: str = "GET", jq: str, paginate: bool = False, body: bytes | None = None
) -> bytes:
    """``gh.api``; a failure becomes Failure(``what``), with gh's message in the log."""
    try:
        return gh.api(endpoint, method=method, jq=jq, paginate=paginate, body=body)
    except runner.CommandError as e:
        if e.stderr:
            # gh's own messages, which may quote API responses: prefixed and
            # with '##[' broken up, like any untrusted log text.
            workflow.log_lines(e.stderr.decode("utf-8", errors="replace"), "gh| ", stream=sys.stderr.buffer)
        raise Failure(f"{what} ({e}).") from e
    except ValueError as e:
        raise Failure(f"{what} ({e}).") from e


def cmd_post(args: Sequence[str]) -> bytes:
    o = parse_options(args, POST_OPTIONS)
    repo, pr = o["--repo"], o["--pr"]
    if not validate.matches(validate.REPO, repo):
        raise Failure(f"Invalid repo '{repo}'.")
    if not validate.matches(validate.POSITIVE_INT, pr):
        raise Failure(f"Invalid PR number '{pr}'.")
    harness = check_label("harness", o["--harness"])
    path = o["--body-file"]
    try:
        body_bytes = files.read_bytes(path, BODY_MAX_BYTES) if path else None
    except FileNotFoundError:
        body_bytes = None
    except files.UnsafeFileError as e:
        raise Failure(f"The comment body exceeds {BODY_MAX_BYTES} bytes or is not a regular file ({e}).") from e
    except OSError as e:
        raise Failure(f"Cannot read the body file '{path}': {e}") from e
    if body_bytes is None:
        raise Failure(f"No body file '{path}'.")
    want = marker(harness)
    if len(want) >= FIRST_LINE_CHARS:
        raise Failure(f"The marker is not shorter than the {FIRST_LINE_CHARS} characters the listing keeps.")
    if body_bytes.split(b"\n", 1)[0] != want.encode("utf-8"):
        raise Failure("The body does not start with the marker.")
    try:
        body = body_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as e:
        raise Failure("The comment body is not valid UTF-8.") from e

    # A failed listing stops here, so it can never post a second comment.
    listing = _gh(
        f"Could not list the comments of #{pr}",
        f"repos/{repo}/issues/{pr}/comments?per_page=100",
        paginate=True,
        jq=LISTING_JQ,
    )
    cid = find_comment(listing, os.environ.get("BOT_LOGIN") or DEFAULT_BOT_LOGIN, want)
    # Our own body, encoded as JSON (never parsing anything); compact, as 'jq -c'.
    payload = (json.dumps({"body": body}, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if cid is not None:
        url = _gh(
            f"Could not update comment {cid}",
            f"repos/{repo}/issues/comments/{cid}",
            method="PATCH",
            body=payload,
            jq=".html_url",
        )
    else:
        url = _gh(
            "Could not post the comment",
            f"repos/{repo}/issues/{pr}/comments",
            method="POST",
            body=payload,
            jq=".html_url",
        )
    # The workflow writes it to GITHUB_OUTPUT: exactly one line.
    text = url.decode("utf-8", errors="replace").removesuffix("\n")
    if not text or "\n" in text or "\r" in text:
        raise Failure("gh returned no single-line comment URL.")
    return (text + "\n").encode("utf-8")


def run(argv: Sequence[str]) -> bytes:
    if not argv:
        raise Failure("Usage: publish-comment sanitize|render|post [options]")
    command, args = argv[0], argv[1:]
    if command == "sanitize":
        # The bash version ignored extra arguments here too.
        return cmd_sanitize()
    if command == "render":
        return cmd_render(args)
    if command == "post":
        return cmd_post(args)
    raise Failure(f"Unknown command '{command}'.")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        out = run(sys.argv[1:] if argv is None else argv)
    except Failure as e:
        _stderr_line(str(e))
        return e.code
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return 0
