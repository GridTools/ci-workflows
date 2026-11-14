"""agent_review.publish_comment: render and post (tests/test-publish-comment.sh is the black-box spec).

Unit tests of the parts the bash suite does not reach, and the entry point
under the production command line in a hostile environment, with a fake
'gh' first on PATH.
"""

import json
import os
import pathlib
import subprocess

import pytest

from agent_review import publish_comment as pc
from agent_review import runner, sanitize

AR_DIR = pathlib.Path(__file__).resolve().parents[2]
ENTRY = [*("/usr/bin/python3", "-I", "-S", "-B"), str(AR_DIR / "bin" / "publish-comment")]
SHA = "0123456789abcdef0123456789abcdef01234567"
RUN_URL = "https://github.com/example-org/gt4py/actions/runs/1"
MARKER = "<!-- agent-review:stub -->"


def render(review: bytes, **overrides: str) -> bytes:
    args = {"harness": "stub", "version": "stub-1", "model": "none", "head_sha": SHA, "run_url": RUN_URL}
    return pc.render(**{**args, **overrides}, review=review)


# --- render ------------------------------------------------------------------------


def test_render_layout() -> None:
    body = render(b"### Summary\nok\n\n\n").decode()
    assert body == (
        f"{MARKER}\n> [!NOTE]\n> Advisory review by **stub** stub-1 (`none`) of commit {SHA}, produced in a sandbox. "
        "Later pushes are not reviewed. Not a required check: it may be wrong, and the content of the pull request "
        f"can steer it. [Workflow run]({RUN_URL}).\n\n### Summary\nok\n"
    )


@pytest.mark.parametrize("review", [b"", b"\n\n", b" \t\n", b"\x0b\x0c\r\n", "\u200b\u2066\n".encode()])
def test_render_empty(review: bytes) -> None:
    with pytest.raises(pc.Failure) as e:
        render(review)
    assert e.value.code == pc.EXIT_INVALID


def test_render_non_ascii_whitespace_is_not_empty() -> None:
    # As in bash under LC_ALL=C: only ASCII whitespace counts.
    assert render("\u00a0\n".encode()).endswith("\n\n\u00a0\n".encode())


def test_render_invalid_utf8() -> None:
    with pytest.raises(pc.Failure) as e:
        render(b"\xff")
    assert e.value.code == pc.EXIT_INVALID


@pytest.mark.parametrize(
    ("review", "noted"),
    [
        (b"x" * 59999, False),
        # Exactly the cap and not cut: no note (bash noted it).
        (b"x" * 60000, False),
        (b"x" * 60000 + b"\n", False),
        (b"x" * 60001, True),
        # Grows past the cap through escaping, cut: noted (bash did not).
        (b"xx" + b"@a" * 12000, True),
        # Larger than the cap only because of dropped characters: not cut.
        (b"ok\n" + "\u200b".encode() * 30000, False),
    ],
    ids=["below", "at-cap", "at-cap-newline", "above", "grows-past-cap", "dropped-only"],
)
def test_render_truncation_note(review: bytes, noted: bool) -> None:
    """The note appears if and only if the sanitised review was cut."""
    body = render(review)
    assert (b"_[Truncated to 60000 bytes.]_" in body) is noted
    assert len(body) <= pc.BODY_MAX_BYTES
    assert sanitize.sanitize_and_report(review)[1] is noted


def test_render_cut_keeps_every_mention_neutralised() -> None:
    body = render(b"xx" + b"@a" * 12000).decode()
    text = body.split("\n\n", 1)[1].split("\n\n_[Truncated")[0]
    # Cut after a whole '@' ZWJ unit at most: never a bare '@'.
    assert text.endswith(("a", "@\u200d"))
    assert text.count("@") == text.count("@\u200d")


def test_render_body_fits_github_for_any_review() -> None:
    body = render("\U0001f600@a<!--".encode() * 30000)
    assert len(body) <= pc.BODY_MAX_BYTES
    assert body.count(b"<!--") == 1


@pytest.mark.parametrize(
    "args",
    [
        ["--harness", "stub", "--version", "1 [x](https://evil)"],
        ["--harness", "stub\n"],
        ["--harness", "-x"],
        ["--model", "a" * 129],
        ["--head-sha", SHA + "\n"],
        ["--head-sha", SHA.upper()],
        ["--run-url", RUN_URL + "\n"],
        ["--run-url", "https://github.com.evil.org/x"],
        ["--run-url", RUN_URL + ")"],
        ["--bogus", "x"],
        ["--review"],
    ],
)
def test_render_rejects(args: list[str], tmp_path: pathlib.Path) -> None:
    review = tmp_path / "review.md"
    review.write_text("ok\n")
    base = {
        "--harness": "stub",
        "--version": "stub-1",
        "--model": "none",
        "--head-sha": SHA,
        "--run-url": RUN_URL,
        "--review": str(review),
    }
    argv = [x for kv in base.items() for x in kv] + args
    with pytest.raises(pc.Failure) as e:
        pc.cmd_render(argv)
    assert e.value.code == 1


def test_render_review_file_must_be_regular(tmp_path: pathlib.Path) -> None:
    real = tmp_path / "real.md"
    real.write_text("ok\n")
    link = tmp_path / "review.md"
    link.symlink_to(real)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    base = ["--harness", "stub", "--version", "v", "--model", "m", "--head-sha", SHA, "--run-url", RUN_URL]
    for path in (link, fifo, tmp_path, tmp_path / "missing"):
        with pytest.raises(pc.Failure) as e:
            pc.cmd_render([*base, "--review", str(path)])
        assert e.value.code == 1


# --- post ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("listing", "want"),
    [
        (b"", None),
        (b"15\tgithub-actions[bot]\t" + MARKER.encode(), "15"),  # no final newline
        (b"15\tgithub-actions[bot]\t" + MARKER.encode() + b"\n16\tgithub-actions[bot]\t" + MARKER.encode(), "15"),
        (b"15\tsome-user\t" + MARKER.encode() + b"\n", None),
        (b"15\tgithub-actions[bot]\t" + MARKER.encode() + b"\\r\n", None),  # jq's @tsv escape of a CR
        (b"15\tgithub-actions[bot]\t" + MARKER.encode() + b" \n", None),
        (b"15\tgithub-actions[bot]\tx" + MARKER.encode() + b"\n", None),
        (b"15\tgithub-actions[bot]\t" + MARKER.encode() + b"\tx\n", None),
        (b"\xff\xfe\tgithub-actions[bot]\tx\n15\tgithub-actions[bot]\t" + MARKER.encode() + b"\n", "15"),
    ],
)
def test_find_comment(listing: bytes, want: str | None) -> None:
    assert pc.find_comment(listing, "github-actions[bot]", MARKER) == want


@pytest.mark.parametrize("cid", [b"0", b"-1", b"1/../2", b"\xd9\xa1", b"12 ", b""])
def test_find_comment_rejects_a_bad_id(cid: bytes) -> None:
    with pytest.raises(pc.Failure):
        pc.find_comment(cid + b"\tgithub-actions[bot]\t" + MARKER.encode() + b"\n", "github-actions[bot]", MARKER)


@pytest.fixture
def fake_gh(tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict[str, str]]:
    """A fake 'gh' that serves FAKE_DIR/comments.tsv and logs write requests; (dir, env)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  "api --paginate "*) [ ! -e "${FAKE_DIR}/list-fails" ] || { echo "HTTP 502 ##[error]x" >&2; exit 1; }\n'
        '    printf "%s\\n" "$@" >"${FAKE_DIR}/list-args"; cat "${FAKE_DIR}/comments.tsv" ;;\n'
        '  "api -X "*) [ ! -e "${FAKE_DIR}/write-fails" ] || exit 1\n'
        '    echo "$3 $4" >>"${FAKE_DIR}/requests.log"; cat >>"${FAKE_DIR}/requests.log"\n'
        '    cat "${FAKE_DIR}/url" 2>/dev/null || echo "https://example/c" ;;\n'
        '  *) echo "unexpected gh call: $*" >&2; exit 1 ;;\n'
        "esac\n"
    )
    (bindir / "gh").chmod(0o755)
    (tmp_path / "comments.tsv").write_bytes(b"")
    env = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin", "FAKE_DIR": str(tmp_path)}
    return tmp_path, env


def post(directory: pathlib.Path, env: dict[str, str], body: bytes, *extra: str) -> subprocess.CompletedProcess[bytes]:
    (directory / "body.md").write_bytes(body)
    argv = ["post", "--repo", "example-org/gt4py", "--pr", "9", "--harness", "stub", "--body-file", "body.md", *extra]
    return subprocess.run([*ENTRY, *argv], cwd=directory, env=env, capture_output=True, timeout=60, check=False)


def test_post_json_body_and_listing(fake_gh: tuple[pathlib.Path, dict[str, str]]) -> None:
    directory, env = fake_gh
    body = f'{MARKER}\n"quoted" \\ tab\t\u00e9 \U0001f600\n'.encode()
    result = post(directory, env, body)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"https://example/c\n"
    assert (directory / "list-args").read_text().splitlines() == [
        "api",
        "--paginate",
        "repos/example-org/gt4py/issues/9/comments?per_page=100",
        "--jq",
        pc.LISTING_JQ,
    ]
    log = (directory / "requests.log").read_bytes().split(b"\n", 1)
    assert log[0] == b"POST repos/example-org/gt4py/issues/9/comments"
    # Compact, as 'jq -Rsc', and the body exactly.
    assert log[1].startswith(b'{"body":"')
    assert json.loads(log[1]) == {"body": body.decode()}


def test_post_bot_login_override(fake_gh: tuple[pathlib.Path, dict[str, str]]) -> None:
    directory, env = fake_gh
    (directory / "comments.tsv").write_text(f"15\tgithub-actions[bot]\t{MARKER}\n16\tother-bot\t{MARKER}\n")
    result = post(directory, {**env, "BOT_LOGIN": "other-bot"}, f"{MARKER}\n".encode())
    assert result.returncode == 0, result.stderr
    assert (directory / "requests.log").read_bytes().startswith(b"PATCH repos/example-org/gt4py/issues/comments/16\n")


def test_post_listing_failure_is_logged_safely(fake_gh: tuple[pathlib.Path, dict[str, str]]) -> None:
    directory, env = fake_gh
    (directory / "list-fails").touch()
    result = post(directory, env, f"{MARKER}\n".encode())
    assert result.returncode == 1
    assert not (directory / "requests.log").exists()
    assert b"##[" not in result.stderr
    assert all(not line.startswith(b"::") for line in result.stderr.split(b"\n"))
    assert b"gh| HTTP 502" in result.stderr


def test_post_write_failure(fake_gh: tuple[pathlib.Path, dict[str, str]]) -> None:
    directory, env = fake_gh
    (directory / "write-fails").touch()
    result = post(directory, env, f"{MARKER}\n".encode())
    assert result.returncode == 1
    assert result.stdout == b""


@pytest.mark.parametrize("url", [b"", b"\n", b"https://a\nhttps://b\n", b"https://a\r\n"])
def test_post_rejects_a_bad_url(fake_gh: tuple[pathlib.Path, dict[str, str]], url: bytes) -> None:
    directory, env = fake_gh
    (directory / "url").write_bytes(url)
    result = post(directory, env, f"{MARKER}\n".encode())
    assert result.returncode == 1
    assert result.stdout == b""


@pytest.mark.parametrize(
    ("body", "extra"),
    [
        (b"no marker\n", []),
        (f"{MARKER} \n".encode(), []),
        (f"{MARKER}\n".encode() + b"\xff", []),
        (f"{MARKER}\n".encode() + b"x" * pc.BODY_MAX_BYTES, []),
        (f"{MARKER}\n".encode(), ["--pr", "09"]),
        (f"{MARKER}\n".encode(), ["--pr", "\uff19"]),
        (f"{MARKER}\n".encode(), ["--repo", "a/b/c"]),
        (f"{MARKER}\n".encode(), ["--repo", "../x"]),
        (f"{MARKER}\n".encode(), ["--harness", "opencode"]),
        (f"{MARKER}\n".encode(), ["--edit-last", "x"]),
    ],
)
def test_post_refuses(fake_gh: tuple[pathlib.Path, dict[str, str]], body: bytes, extra: list[str]) -> None:
    directory, env = fake_gh
    result = post(directory, env, body, *extra)
    assert result.returncode == 1
    assert not (directory / "requests.log").exists()


def test_post_without_gh_on_path(tmp_path: pathlib.Path) -> None:
    (tmp_path / "body.md").write_text(f"{MARKER}\n")
    result = subprocess.run(
        [*ENTRY, "post", "--repo", "o/r", "--pr", "9", "--harness", "stub", "--body-file", "body.md"],
        cwd=tmp_path,
        env={"PATH": str(tmp_path)},
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert b"not found on PATH" in result.stderr


# --- The entry point --------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "code"),
    [([], 1), (["bogus"], 1), (["render", "--harness"], 1), (["post"], 1)],
)
def test_usage_errors(argv: list[str], code: int, tmp_path: pathlib.Path) -> None:
    result = subprocess.run([*ENTRY, *argv], cwd=tmp_path, capture_output=True, timeout=60, check=False)
    assert result.returncode == code
    assert result.stdout == b""
    assert result.stderr.startswith(b"publish-comment: ")


def test_stderr_cannot_carry_workflow_commands(tmp_path: pathlib.Path) -> None:
    result = subprocess.run(
        [*ENTRY, "render", "--harness", "x\n::error::pwned\n##[warning]w"],
        cwd=tmp_path,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr.count(b"\n") == 1
    assert b"##[" not in result.stderr


HOSTILE_PLANTS = ["json.py", "re.py", "unicodedata.py", "sitecustomize.py", "agent_review/__init__.py"]


def test_hostile_environment(fake_gh: tuple[pathlib.Path, dict[str, str]]) -> None:
    """sanitize, render and post with planted modules in the working directory and on PYTHONPATH."""
    directory, env = fake_gh
    markers = directory / "markers"
    markers.mkdir()
    for root in (directory, directory / "pythonpath"):
        for rel in HOSTILE_PLANTS:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"open({str(markers / (root.name + '-' + rel.replace('/', '-')))!r}, 'w').close()\n")
    env = {**env, "PYTHONPATH": f"{directory / 'pythonpath'}:{directory}", "PYTHONSTARTUP": str(directory / "re.py")}

    def run(*argv: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [*ENTRY, *argv], cwd=directory, env=env, input=stdin, capture_output=True, timeout=60, check=False
        )

    out = run("sanitize", stdin="@user \u2066<!--".encode())
    assert out.returncode == 0, out.stderr
    assert out.stdout == sanitize.sanitize("@user \u2066<!--".encode())
    (directory / "review.md").write_text("ok @user\n")
    rendered = run(
        "render", "--harness", "stub", "--version", "v", "--model", "m", "--head-sha", SHA, "--run-url", RUN_URL,
        "--review", "review.md",
    )  # fmt: skip
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout.startswith(MARKER.encode() + b"\n")
    (directory / "body.md").write_bytes(rendered.stdout)
    posted = run("post", "--repo", "o/r", "--pr", "9", "--harness", "stub", "--body-file", "body.md")
    assert posted.returncode == 0, posted.stderr
    assert sorted(p.name for p in markers.iterdir()) == []


# --- The listing projection (availability) -------------------------------------------


def test_every_marker_fits_the_projection() -> None:
    """A longer marker would be cut by the listing and never match again."""
    longest = pc.marker("a" * 128)
    assert pc.check_label("harness", "a" * 128)
    with pytest.raises(pc.Failure):
        pc.check_label("harness", "a" * 129)
    assert len(longest) == 150
    assert len(longest) < pc.FIRST_LINE_CHARS
    assert f".[0:{pc.FIRST_LINE_CHARS}]" in pc.LISTING_JQ


JQ_FAKE_GH = """#!/bin/sh
# Applies the real --jq filter to FAKE_DIR/comments.json, as gh does per page.
case "$1 $2" in
  "api --paginate") [ "$4" = --jq ] || exit 9; exec /usr/bin/jq -r "$5" "${FAKE_DIR}/comments.json" ;;
  "api -X") echo "$3 $4" >>"${FAKE_DIR}/requests.log"; cat >/dev/null; echo "https://example/c" ;;
  *) echo "unexpected gh call: $*" >&2; exit 1 ;;
esac
"""


@pytest.fixture
def jq_gh(tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict[str, str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(JQ_FAKE_GH)
    (bindir / "gh").chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin", "FAKE_DIR": str(tmp_path)}
    return tmp_path, env


def comment(cid: int, login: str, body: str) -> dict[str, object]:
    return {"id": cid, "user": {"login": login}, "body": body}


def test_huge_foreign_first_lines_do_not_block_the_upsert(jq_gh: tuple[pathlib.Path, dict[str, str]]) -> None:
    """300 comments with 60 KB first lines: the unbounded projection passed runner's 16 MiB cap."""
    directory, env = jq_gh
    huge = [comment(1000 + i, "some-user", "x" * 60000 + "\nrest") for i in range(300)]
    comments = [*huge, comment(42, "github-actions[bot]", f"{MARKER}\nold review")]
    path = directory / "comments.json"
    path.write_text(json.dumps(comments))
    # Negative control: the old projection's output is over the cap.
    old_jq = '.[] | [.id, .user.login, (.body | split("\\n") | .[0])] | @tsv'
    old = subprocess.run(["/usr/bin/jq", "-r", old_jq, str(path)], capture_output=True, check=True).stdout
    assert len(old) > runner.MAX_STDOUT_BYTES
    result = post(directory, env, f"{MARKER}\nnew review\n".encode())
    assert result.returncode == 0, result.stderr
    assert (directory / "requests.log").read_text() == "PATCH repos/example-org/gt4py/issues/comments/42\n"


@pytest.mark.parametrize(
    ("first_line", "matches"),
    [
        (MARKER, True),
        (MARKER + " ", False),
        (MARKER + "x" * 300, False),  # longer than the projection: cut, still no match
        (" " + MARKER, False),
        (MARKER + "\r", False),
        (MARKER + "\t", False),
    ],
)
def test_projection_keeps_matching_exact(
    jq_gh: tuple[pathlib.Path, dict[str, str]], first_line: str, matches: bool
) -> None:
    directory, env = jq_gh
    (directory / "comments.json").write_text(json.dumps([comment(42, "github-actions[bot]", f"{first_line}\nx")]))
    result = post(directory, env, f"{MARKER}\nnew\n".encode())
    assert result.returncode == 0, result.stderr
    want = (
        "PATCH repos/example-org/gt4py/issues/comments/42"
        if matches
        else "POST repos/example-org/gt4py/issues/9/comments"
    )
    assert (directory / "requests.log").read_text() == want + "\n"
