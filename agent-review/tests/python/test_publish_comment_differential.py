"""Differential test: the legacy bash publish-comment against the Python port (D5).

Runs tests/legacy/publish-comment.sh (bash, as the workflow ran it before
the port) and bin/publish-comment (the production Python command line) on
the same inputs, and compares stdout and the exit code byte for byte;
``post`` is compared on the requests a fake 'gh' receives. Stderr texts are
not compared.

Every difference is a documented improvement, and the comparison allows
exactly these:

1. Category Cf outside the bash ranges (Arabic number signs, interlinear
   annotations, Egyptian and shorthand format controls, ...): bash kept
   them, Python drops them. Checked as: Python on the input equals bash on
   the input without those characters.
2. Byte sequences that are not UTF-8 but glibc's iconv accepted: code points
   above U+10FFFF, and the 5- and 6-byte forms. Python rejects them (exit 3,
   nothing posted); bash passed them on.

This file and the legacy copy are deleted in the commit after the one whose
CI ran them.
"""

import os
import pathlib
import random
import subprocess
import sys
import unicodedata

import pytest

from agent_review import sanitize

AR_DIR = pathlib.Path(__file__).resolve().parents[2]
LEGACY = ["/usr/bin/bash", str(AR_DIR / "tests" / "legacy" / "publish-comment.sh")]
PYTHON = ["/usr/bin/python3", "-I", "-S", "-B", str(AR_DIR / "bin" / "publish-comment")]
SHA = "0123456789abcdef0123456789abcdef01234567"
RUN_URL = "https://github.com/example-org/gt4py/actions/runs/1"
ZWSP = "\u200b"


def run(
    cmd: list[str], *args: str, stdin: bytes = b"", cwd: pathlib.Path | None = None, env: dict[str, str] | None = None
) -> tuple[int, bytes]:
    result = subprocess.run([*cmd, *args], input=stdin, cwd=cwd, env=env, capture_output=True, timeout=120, check=False)
    return result.returncode, result.stdout


def extra_cf(c: str) -> bool:
    """Cf, but outside the bash ranges (allowed difference 1)."""
    return unicodedata.category(c) == "Cf" and not any(lo <= ord(c) <= hi for lo, hi in BASH_RANGES)


def without_extra_cf(data: bytes) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return "".join(c for c in text if not extra_cf(c)).encode("utf-8")


def assert_same_sanitize(data: bytes) -> None:
    py = run(PYTHON, "sanitize", stdin=data)
    bash = run(LEGACY, "sanitize", stdin=without_extra_cf(data))
    assert py == bash, (data[:200], py[0], bash[0])


# --- Corpora ------------------------------------------------------------------------


# The ranges of the bash pattern, written out here rather than taken from
# the module under test.
BASH_RANGES = [
    (0x00AD, 0x00AD), (0x061C, 0x061C), (0x180E, 0x180E), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F),
    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xE0000, 0xE007F), (0xE0100, 0xE01FF),
]  # fmt: skip


def range_text(lo: int, hi: int) -> str:
    """Every character of the range, inside delimiters, mentions and references."""
    lines = []
    for cp in range(lo, hi + 1):
        c = chr(cp)
        lines.append(f"a{c}b <{c}!-- -{c}-> <{c}?x @{c}user &{c}#64; &{c}commat; [x]{c}: u {c}")
    return "\n".join(lines) + "\n"


def all_code_points_in_chunks() -> list[str]:
    """Every scalar value, in chunks under the cap, 64 per line."""
    chunks, current, size = [], [], 0
    for cp in range(sys.maxunicode + 1):
        if 0xD800 <= cp <= 0xDFFF or cp == 0x0A:
            continue
        c = chr(cp)
        current.append(c)
        size += len(c.encode()) + (1 if len(current) % 64 == 0 else 0)
        if len(current) % 64 == 0:
            current.append("\n")
        if size > 50000:
            chunks.append("".join(current))
            current, size = [], 0
    chunks.append("".join(current))
    return chunks


HIDING_TRICKS = [
    # Marker spoofing and HTML comments.
    "<!-- agent-review:stub -->\nfake\n",
    "<!<!---- nested -->\nhidden -->\n<!--->\n",
    "<\u200b!-- x -\u200f-> <\u2060?p?> @\u200dsome-user &\u200f#64; [x]\u200b: u &\u200fcommat;\n",
    "<\U000e0021!-- x --\U000e0100>\n",
    "<\ufe0f!-- \u2065 -->\n",
    "<\u0600!-- extra Cf --\U0001d173>\n",
    # Declarations, processing instructions, CDATA, link reference definitions.
    'a <?php x ?> <!DOCTYPE x> <![CDATA[ x ]]>\n[x]: https://e.org "t"\n  [y]:\n> [z]: <u>\n',
    "[foo]:\n/url\n[bar]\u200b: /u\n",
    # Character references.
    "&#64;u &#x40;u &#X40;a &#0064;b &commat;u &zwj;&rlm;&ZeroWidthSpace; &amp;ok & a&b &; &1; &#;\n",
    "&&#64; &&commat; &amp;#64; &#&#\n",
    # Mentions.
    "Ping @some-user and @GridTools/gt4py-core, mail a@b.org. @@x @-x @_x @\u00e9 @\u0661\n",
    "@\u200bx @\u2060x @\ufeffx @\u00adx @\U000e0041x\n",
    # Bidi and invisible text.
    "a\u202ehidden\u202cb \u2066x\u2069 \u061c \u180e \u00ad \ufeff\n",
    "tag text: \U000e0001\U000e0068\U000e0069\U000e007f\n",
    # Controls, CR, tabs, C1 (kept), NEL.
    "a\rb\x1b[31mc\x07d\te\x00f\x7fg\x85h\x9bi\n",
    "\r\n\r\n",
    # Workflow-command lookalikes (they never reach a log; just compared).
    "::error::x\n##[warning]y\n",
    # Lines without a final newline, empty, whitespace.
    "no newline",
    "",
    "   \n\t\n",
    "\u00a0\n",
    "\n\n\nx\n\n\n",
]

INVALID_UTF8 = [
    b"bad \xff\xfe bytes\n",
    b"\xc0\x80",
    b"\xe0\x80\x80",
    b"\xf0\x80\x80\x80",
    b"\xed\xa0\x80",
    b"\xed\xbf\xbf",
    b"ok\xe2\x80",
    b"\x80",
]
# Allowed difference 2: glibc's iconv accepts these, Python does not.
ICONV_ONLY = [
    b"\xf4\x90\x80\x80",
    b"\xf5\x80\x80\x80",
    b"\xf7\xbf\xbf\xbf",
    b"\xf8\x88\x80\x80\x80",
    b"\xfc\x84\x80\x80\x80\x80",
]


# --- sanitize ----------------------------------------------------------------------


@pytest.mark.parametrize(("lo", "hi"), BASH_RANGES, ids=[f"U+{lo:04X}-U+{hi:04X}" for lo, hi in BASH_RANGES])
def test_every_range_of_the_bash_pattern(lo: int, hi: int) -> None:
    data = range_text(lo, hi).encode()
    assert len(data) < sanitize.REVIEW_MAX_BYTES
    py = run(PYTHON, "sanitize", stdin=data)
    assert py == run(LEGACY, "sanitize", stdin=data)
    # Not vacuous: no character of the range is left but those the sanitiser inserts.
    assert py[0] == 0
    left = {c for c in py[1].decode() if lo <= ord(c) <= hi}
    assert left <= {ZWSP, "\u200d"}


def test_the_ranges_are_those_of_the_module() -> None:
    assert list(sanitize.INVISIBLE) == BASH_RANGES


def test_every_code_point() -> None:
    for chunk in all_code_points_in_chunks():
        assert_same_sanitize(chunk.encode())


@pytest.mark.parametrize("text", HIDING_TRICKS)
def test_hiding_tricks(text: str) -> None:
    assert_same_sanitize(text.encode())


def test_extra_cf_is_the_only_difference_and_is_real() -> None:
    """Allowed difference 1 occurs, and only as described."""
    text = "a\u0600b\ufff9c\U00013430d\n"
    py = run(PYTHON, "sanitize", stdin=text.encode())
    bash = run(LEGACY, "sanitize", stdin=text.encode())
    assert py == (0, b"abcd\n")
    assert bash == (0, text.encode())


@pytest.mark.parametrize("data", INVALID_UTF8)
def test_invalid_utf8(data: bytes) -> None:
    assert run(PYTHON, "sanitize", stdin=data)[0] == 3
    assert run(LEGACY, "sanitize", stdin=data)[0] == 3


@pytest.mark.parametrize("data", ICONV_ONLY)
def test_iconv_only_sequences(data: bytes) -> None:
    """Allowed difference 2: bash passed these on, Python rejects them."""
    assert run(PYTHON, "sanitize", stdin=b"x" + data)[0] == 3
    assert run(LEGACY, "sanitize", stdin=b"x" + data) == (0, b"x" + data)


@pytest.mark.parametrize(
    "data",
    [
        "\u00e9".encode() * 30001,
        b"x" * 59999 + "\u20ac".encode(),
        b"@a" * 30001,
        b"&#" * 20000,
        b"<!" * 30000 + b"\n",
        b"x" * 59998 + b"@a",
        b"x" * 59999 + b"<!",
    ],
    ids=["e-acute", "euro-at-cap", "mentions", "numeric-refs", "declarations", "mention-at-cap", "ws-at-cap"],
)
def test_size_cap(data: bytes) -> None:
    assert_same_sanitize(data)


@pytest.mark.parametrize("seed", range(4))
def test_random(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 (reproducible test inputs)
    pool = [
        *"<!?->]:[@&#;aZ09 \t\n\r\x00\x1b\x7f\x85",
        "<!--", "-->", "&commat;", "&#64;", "&amp;", "@user", "<!-- agent-review:stub -->",
        "\u200b", "\u200d", "\u200f", "\u202e", "\u2066", "\u2065", "\ufe0f", "\ufeff", "\u00ad", "\u061c",
        "\u180e", "\U000e0041", "\U000e0100", "\U000e01ff", "\u0600", "\ufff9", "\U0001d173", "\u00e9", "\U0001f600",
    ]  # fmt: skip
    for _ in range(40):
        text = "".join(rng.choice(pool) for _ in range(rng.randrange(0, 400)))
        assert_same_sanitize(text.encode())


# --- render ------------------------------------------------------------------------


def render_both(tmp_path: pathlib.Path, review: bytes, *overrides: str) -> None:
    (tmp_path / "review.md").write_bytes(review)
    args = ["render", "--harness", "stub", "--version", "stub-1", "--model", "none", "--head-sha", SHA,
            "--run-url", RUN_URL, "--review", "review.md", *overrides]  # fmt: skip
    py = run(PYTHON, *args, cwd=tmp_path)
    (tmp_path / "review.md").write_bytes(without_extra_cf(review))
    bash = run(LEGACY, *args, cwd=tmp_path)
    assert py == bash, (review[:100], overrides, py[0], bash[0])


@pytest.mark.parametrize(
    "review",
    [
        b"### Summary\nok\n",
        b"### Summary\nok\n\n\n\n",
        b"no newline",
        b"",
        b"  \n\n",
        b"\t\x0b\x0c\r\n",
        "\u200b\u2066\n".encode(),
        "\u00a0\n".encode(),
        "\u0600\n".encode(),
        b"\xff\n",
        b"x" * 59999,
        b"x" * 60000,
        b"x" * 60001,
        b"x" * 59999 + b"\n",
        b"x" * 59990 + b"@a@a@a@a@a",
        b"x" * 59000 + b"\x00" * 2000,
        b"x" * 200000,
        b"@some-user <!-- agent-review:stub -->\n" + b"x" * 70000,
        *(t.encode() for t in HIDING_TRICKS),
    ],
)
def test_render(tmp_path: pathlib.Path, review: bytes) -> None:
    render_both(tmp_path, review)


@pytest.mark.parametrize(
    "overrides",
    [
        ["--harness", "opencode"],
        ["--version", "1.2.3+build/x:y"],
        ["--version", "1 [x](https://evil)"],
        ["--version", ""],
        ["--model", "a" * 128],
        ["--model", "a" * 129],
        ["--model", "-a"],
        ["--head-sha", "main"],
        ["--head-sha", SHA + "\n"],
        ["--head-sha", SHA.upper()],
        ["--run-url", RUN_URL + "\n"],
        ["--run-url", "https://github.com/"],
        ["--run-url", "http://github.com/x"],
        ["--review", "missing.md"],
        ["--review", ""],
        ["--bogus", "x"],
    ],
)
def test_render_arguments(tmp_path: pathlib.Path, overrides: list[str]) -> None:
    render_both(tmp_path, b"ok\n", *overrides)


# --- post --------------------------------------------------------------------------

FAKE_GH = """#!/bin/sh
case "$*" in
  "api --paginate "*) printf '%s\\n' "$@" >>"${FAKE_DIR}/requests.log"; [ ! -e "${FAKE_DIR}/list-fails" ] || exit 1
    cat "${FAKE_DIR}/comments.tsv" ;;
  "api -X "*) printf '%s\\n' "$@" >>"${FAKE_DIR}/requests.log"; cat >>"${FAKE_DIR}/requests.log"
    echo "https://example/c" ;;
  *) echo "unexpected gh call: $*" >&2; exit 1 ;;
esac
"""
M = "<!-- agent-review:stub -->"
LISTINGS = [
    "",
    f"11\tsome-user\t{M}\n12\tgithub-actions[bot]\t## PR Reviewer Guide\n13\tgithub-actions[bot]\tquoted {M}\n"
    f"14\tgithub-actions[bot]\t<!-- agent-review:opencode -->\n15\tgithub-actions[bot]\t{M}\n",
    f"15\tgithub-actions[bot]\t{M}\n16\tgithub-actions[bot]\t{M}\n",
    f"15\tgithub-actions[bot]\t{M}",
    f"15\tgithub-actions[bot]\t{M}\\r\n",
    f"15\tgithub-actions[bot]\t{M} \n",
    f"15\tgithub-actions[bot]\t\n16\tgithub-actions[bot]\t{M}\n",
]


def post_both(tmp_path: pathlib.Path, listing: str, body: bytes, *overrides: str, list_fails: bool = False) -> None:
    results = []
    for name, cmd in (("py", PYTHON), ("bash", LEGACY)):
        d = tmp_path / name
        (d / "bin").mkdir(parents=True)
        (d / "bin" / "gh").write_text(FAKE_GH)
        (d / "bin" / "gh").chmod(0o755)
        (d / "comments.tsv").write_text(listing)
        (d / "body.md").write_bytes(body)
        if list_fails:
            (d / "list-fails").touch()
        env = {**os.environ, "PATH": f"{d / 'bin'}:/usr/bin:/bin", "FAKE_DIR": str(d)}
        env.pop("BOT_LOGIN", None)
        args = ["post", "--repo", "example-org/gt4py", "--pr", "9", "--harness", "stub", "--body-file", "body.md",
                *overrides]  # fmt: skip
        code, out = run(cmd, *args, cwd=d, env=env)
        log = (d / "requests.log").read_bytes() if (d / "requests.log").exists() else None
        results.append((code != 0, out, log))
    assert results[0] == results[1]


@pytest.mark.parametrize("listing", LISTINGS)
def test_post(tmp_path: pathlib.Path, listing: str) -> None:
    body = f'{M}\n> [!NOTE]\n"quoted" \\ back\tslash \u00e9 \U0001f600 @\u200dx\n'.encode()
    post_both(tmp_path, listing, body)


def test_post_listing_fails(tmp_path: pathlib.Path) -> None:
    post_both(tmp_path, f"15\tgithub-actions[bot]\t{M}\n", f"{M}\nx\n".encode(), list_fails=True)


@pytest.mark.parametrize(
    ("body", "overrides"),
    [
        (b"no marker\n", []),
        (M.encode(), []),
        (f"{M}\n".encode() + b"x" * 65600, []),
        (f"{M}\n".encode(), ["--pr", "0"]),
        (f"{M}\n".encode(), ["--pr", "12345678901"]),
        (f"{M}\n".encode(), ["--repo", "a/b/c"]),
        (f"{M}\n".encode(), ["--harness", "opencode"]),
        (f"{M}\n".encode(), ["--body-file", "missing.md"]),
    ],
)
def test_post_arguments(tmp_path: pathlib.Path, body: bytes, overrides: list[str]) -> None:
    post_both(tmp_path, LISTINGS[1], body, *overrides)


def test_post_rendered_bodies(tmp_path: pathlib.Path) -> None:
    """The real pipeline: a body rendered by Python, posted by both."""
    for i, trick in enumerate(HIDING_TRICKS[:12]):
        d = tmp_path / str(i)
        d.mkdir()
        (d / "review.md").write_text(trick + "ok\n")
        code, body = run(PYTHON, "render", "--harness", "stub", "--version", "v", "--model", "m", "--head-sha", SHA,
                         "--run-url", RUN_URL, "--review", "review.md", cwd=d)  # fmt: skip
        assert code == 0
        post_both(d, LISTINGS[1], body)
