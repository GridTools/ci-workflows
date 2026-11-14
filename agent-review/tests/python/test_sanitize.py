"""agent_review.sanitize: the review sanitiser of publish-comment (invariant 5).

The oracles here are independent of the module's tables: the invisible
characters are the bash version's UTF-8 byte pattern, copied verbatim and
matched on each character's encoding, plus Unicode category Cf from
``unicodedata``. The property tests use seeded ``random`` (no extra
dependency) over random and adversarial inputs.
"""

import random
import re
import sys
import unicodedata

import pytest

from agent_review import sanitize
from workflow_structure import import_closure

ZWSP, ZWJ = "\u200b", "\u200d"
MARKER = "<!-- agent-review:"
# The bash version's INVISIBLE pattern, byte for byte (matched under LC_ALL=C).
BASH_INVISIBLE = re.compile(
    rb"\xc2\xad|\xd8\x9c|\xe1\xa0\x8e|\xe2\x80[\x8b-\x8f\xaa-\xae]|\xe2\x81[\xa0-\xaf]|\xef\xb8[\x80-\x8f]"
    rb"|\xef\xbb\xbf|\xf3\xa0[\x80\x81][\x80-\xbf]|\xf3\xa0[\x84-\x87][\x80-\xbf]"
)
# The bash version's 'tr -d' set.
BASH_CONTROLS = re.compile(rb"[\x00-\x08\x0b-\x1f\x7f]")
LOGIN_CHAR = re.compile(r"[A-Za-z0-9]", re.ASCII)


def all_chars() -> list[str]:
    return [chr(cp) for cp in range(sys.maxunicode + 1) if not 0xD800 <= cp <= 0xDFFF]


def oracle_dropped(c: str) -> bool:
    b = c.encode("utf-8")
    return bool(BASH_INVISIBLE.fullmatch(b) or BASH_CONTROLS.fullmatch(b)) or unicodedata.category(c) == "Cf"


def in_bash_ranges(c: str) -> bool:
    return BASH_INVISIBLE.fullmatch(c.encode("utf-8")) is not None


# --- Which characters are dropped ----------------------------------------------


def test_unicode_version_is_known() -> None:
    # Python 3.12 (the runner's): Unicode 15.0. A change of version changes
    # the Cf set; the tests below still hold, but say so in review.
    assert unicodedata.unidata_version.split(".")[0] in ("15", "16", "17")


def test_every_code_point_against_the_oracle() -> None:
    chars = all_chars()
    wrong = [f"U+{ord(c):04X}" for c in chars if sanitize.is_dropped(c) != oracle_dropped(c)]
    assert wrong == []
    text = "".join(chars)
    assert sanitize.drop_invisible(text) == "".join(c for c in chars if not oracle_dropped(c))


def test_ranges_beyond_cf_are_dropped() -> None:
    # Advisor finding 5: Cf alone would keep these.
    for c in ("\ufe0f", "\U000e0100", "\U000e01ef", "\U000e01ff", "\u2065", "\ufe00"):
        assert unicodedata.category(c) != "Cf"
        assert sanitize.drop_invisible(f"a{c}b") == "ab"


def test_cf_beyond_the_ranges_is_dropped() -> None:
    # Arabic number sign, interlinear annotation, Egyptian format controls,
    # shorthand format controls, musical symbol beams.
    for c in ("\u0600", "\u06dd", "\u070f", "\u08e2", "\ufff9", "\ufffb", "\U00013430", "\U0001bca0", "\U0001d173"):
        assert unicodedata.category(c) == "Cf"
        assert not in_bash_ranges(c)
        assert sanitize.drop_invisible(f"a{c}b") == "ab"


@pytest.mark.parametrize("c", ["\t", "\n", "\u2044", "\u00e9", "\u00a0", "\u3000", "\u0085", "\U0001f600", "\ufffe"])
def test_visible_and_kept(c: str) -> None:
    assert sanitize.drop_invisible(f"a{c}b") == f"a{c}b"


def test_explicit_ranges_match_the_bash_bytes() -> None:
    from_table = {cp for lo, hi in sanitize.INVISIBLE for cp in range(lo, hi + 1)}
    from_bash = {ord(c) for c in all_chars() if in_bash_ranges(c)}
    assert from_table == from_bash


# --- Neutralising -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("<!-- x -->", f"<{ZWSP}!-- x --{ZWSP}>"),
        ("<?php ?>", f"<{ZWSP}?php ?>"),
        ("<![CDATA[x]]>", f"<{ZWSP}![CDATA[x]]>"),
        ("--->", f"---{ZWSP}>"),
        ("[x]: https://e.org", f"[x]{ZWSP}: https://e.org"),
        ("@user @@a a@b.org @-x @ x", f"@{ZWJ}user @@{ZWJ}a a@{ZWJ}b.org @-x @ x"),
        ("&commat;x &amp;ok &zwj;", "&amp;commat;x &amp;amp;ok &amp;zwj;"),
        ("&#64; &#x40; &#X40;", f"&amp;#{ZWSP}64; &amp;#{ZWSP}x40; &amp;#{ZWSP}X40;"),
        ("& a&b &; &1x; &\u00e9;", "& a&b &; &1x; &\u00e9;"),
        (f"<{ZWSP}!-- @{ZWSP}x &\u200f#64; [x]{ZWSP}:", f"<{ZWSP}!-- @{ZWJ}x &amp;#{ZWSP}64; [x]{ZWSP}:"),
    ],
)
def test_clean(text: str, want: str) -> None:
    assert sanitize.clean(text) == want


def test_unicode_digits_and_letters_are_not_login_characters() -> None:
    # bash's [A-Za-z0-9] under LC_ALL=C is ASCII only; so is this one.
    assert sanitize.clean("@\u0661 @\u00e9") == "@\u0661 @\u00e9"


# --- Encoding and the size cap ------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"\xff",
        b"\xc0\x80",  # overlong
        b"\xed\xa0\x80",  # surrogate
        b"\xf4\x90\x80\x80",  # above U+10FFFF (glibc iconv accepted it)
        b"\xf8\x88\x80\x80\x80",  # 5-byte form (glibc iconv accepted it)
        b"a\xe2\x80",  # truncated
    ],
)
def test_invalid_utf8(data: bytes) -> None:
    with pytest.raises(sanitize.InvalidReviewError):
        sanitize.sanitize(data)


@pytest.mark.parametrize("char", ["a", "\u00e9", "\u20ac", "\U0001f600"])
@pytest.mark.parametrize("cap", [0, 1, 2, 3, 4, 5, 10, 11])
def test_cut_on_character_boundary(char: str, cap: int) -> None:
    data = (char * 10).encode()
    out = sanitize.cut(data, cap)
    assert len(out) <= cap
    assert data.startswith(out)
    assert out.decode() == char * (len(out) // len(char.encode()))
    # Nothing more could have fitted.
    assert len(out) + len(char.encode()) > cap or out == data


def test_sanitize_caps_after_escaping() -> None:
    out, cut = sanitize.sanitize_and_report(b"@a" * 40000)
    assert cut
    assert len(out) <= sanitize.REVIEW_MAX_BYTES
    out.decode("utf-8")


@pytest.mark.parametrize(
    ("text", "units"),
    [
        ("x@a", ["@\u200d"]),
        ("x<!", ["<\u200b"]),
        ("x<?", ["<\u200b"]),
        ("x-->", ["--\u200b"]),
        ("x--->", ["--\u200b"]),
        ("x]:", ["]\u200b"]),
        ("x&#1", ["&amp;#\u200b"]),
        ("x&lt;", ["&amp;"]),
    ],
)
def test_cut_never_splits_a_neutralisation(text: str, units: list[str]) -> None:
    """Every cap: the cut keeps a neutralisation whole or drops all of it."""
    full = sanitize.clean(text)
    data = full.encode()
    for cap in range(len(data) + 1):
        out = sanitize.cut(data, cap).decode()
        assert full.startswith(out)
        for unit in units:
            start = full.index(unit)
            assert len(out) <= start or len(out) >= start + len(unit), (cap, out)


def test_reported_cut_is_exact() -> None:
    assert sanitize.sanitize_and_report(b"x" * 10, 10) == (b"x" * 10, False)
    assert sanitize.sanitize_and_report(b"x" * 11, 10) == (b"x" * 10, True)
    assert sanitize.sanitize_and_report(b"@a", 3) == (b"", True)
    assert sanitize.sanitize_and_report(b"@a", 4) == ("@\u200d".encode(), True)
    # Only trailing newlines lost: no content was cut.
    assert sanitize.sanitize_and_report(b"xx\n\n", 2) == (b"xx", False)
    assert sanitize.sanitize_and_report(b"xx\n\ny", 2) == (b"xx", True)
    assert sanitize.sanitize_and_report("\u200b".encode() * 10 + b"x", 2) == (b"x", False)


# --- Property tests ------------------------------------------------------------------

NAMED_REF = re.compile(r"&[A-Za-z][A-Za-z0-9]*;", re.ASCII)
# Characters that build the constructs, and hiding tricks around them.
ADVERSARIAL = [
    *"<!?->]:[@&#;aZ09 \t\n",
    "<!--",
    "-->",
    MARKER,
    "agent-review:stub -->",
    "&commat;",
    "&#64;",
    "&#x40;",
    "&amp;",
    "]:",
    "@user",
    "\r",
    "\x00",
    "\x1b",
    "\x7f",
    "\u0085",
]
INVISIBLE_SAMPLES = [c for c in map(chr, range(0x110000)) if not 0xD800 <= ord(c) <= 0xDFFF and oracle_dropped(c)]


def random_text(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randrange(1, 60)):
        roll = rng.random()
        if roll < 0.45:
            parts.append(rng.choice(ADVERSARIAL))
        elif roll < 0.75:
            parts.append(rng.choice(INVISIBLE_SAMPLES))
        else:
            cp = rng.randrange(0x110000)
            if not 0xD800 <= cp <= 0xDFFF:
                parts.append(chr(cp))
    return "".join(parts)


def inserted_at(out: str, i: int) -> bool:
    """True if ``out[i]`` is a character the sanitiser inserts, in its place."""
    c = out[i]
    before, after = out[:i], out[i + 1 : i + 2]
    if c == ZWJ:
        return before.endswith("@") and (LOGIN_CHAR.fullmatch(after) is not None or after == "")
    if c == ZWSP:
        return (
            (before.endswith("<") and after in ("!", "?", ""))
            or (before.endswith("--") and after in (">", ""))
            or (before.endswith("]") and after in (":", ""))
            or before.endswith("&amp;#")
        )
    return False


def check_output(out: str, *, at_end_only_if_cut: bool) -> None:
    assert MARKER not in out
    assert "<!--" not in out and "-->" not in out
    assert "<!" not in out and "<?" not in out and "]:" not in out
    assert re.search(r"@[A-Za-z0-9]", out, re.ASCII) is None
    assert "&#" not in out
    assert all(m.group(0) == "&amp;" for m in NAMED_REF.finditer(out))
    for i, c in enumerate(out):
        if oracle_dropped(c):
            assert inserted_at(out, i), (f"U+{ord(c):04X}", i, out[max(0, i - 8) : i + 8])
            # An inserted character with nothing after it only where the cap cut.
            if not at_end_only_if_cut:
                assert i + 1 < len(out)


@pytest.mark.parametrize("seed", range(20))
def test_property_random_adversarial(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 (reproducible test inputs)
    for _ in range(300):
        text = random_text(rng)
        check_output(sanitize.clean(text), at_end_only_if_cut=False)


# Independent of the module: what each neutralisation inserts, with the
# character(s) it protects.
UNIT = re.compile("&amp;#\u200b|&amp;|<\u200b|--\u200b|\\]\u200b|@\u200d")


def legal_cuts(full: str) -> list[int]:
    """Byte offsets where ``full`` may be cut: character boundaries outside every unit."""
    inside = {i for m in UNIT.finditer(full) for i in range(m.start() + 1, m.end())}
    offsets, pos = [], 0
    for i, c in enumerate(full):
        if i not in inside:
            offsets.append(pos)
        pos += len(c.encode())
    offsets.append(pos)
    return offsets


@pytest.mark.parametrize("seed", range(5))
def test_property_with_the_cap(seed: int) -> None:
    """At every cap: the longest legal prefix, reported as cut exactly when it is."""
    rng = random.Random(1000 + seed)  # noqa: S311 (reproducible test inputs)
    for _ in range(40):
        text = random_text(rng)
        full = sanitize.clean(text)
        data = full.encode()
        legal = legal_cuts(full)
        for cap in range(len(data) + 1):
            out = sanitize.cut(data, cap)
            assert len(out) == max(o for o in legal if o <= cap)
            check_output(out.decode("utf-8"), at_end_only_if_cut=True)
        for cap in (0, len(data) // 2, len(data) - 1, len(data)):
            if cap >= 0:
                out, cut = sanitize.sanitize_and_report(text.encode(), cap)
                assert cut is (len(data.rstrip(b"\n")) > len(out))


@pytest.mark.parametrize("seed", range(5))
def test_property_dropping_never_joins(seed: int) -> None:
    """Invisible characters inside every delimiter, mention and reference."""
    rng = random.Random(2000 + seed)  # noqa: S311 (reproducible test inputs)
    words = ["<!--", "-->", "<?", "<!", "]:", "@user", "&#64;", "&commat;", MARKER, "&amp;#"]
    for _ in range(300):
        word = rng.choice(words)
        pos = rng.randrange(1, len(word))
        hidden = "".join(rng.choice(INVISIBLE_SAMPLES) for _ in range(rng.randrange(1, 4)))
        check_output(sanitize.clean(word[:pos] + hidden + word[pos:]), at_end_only_if_cut=False)


def test_negative_control_the_property_can_fail() -> None:
    with pytest.raises(AssertionError):
        check_output("a\u200bb", at_end_only_if_cut=False)
    with pytest.raises(AssertionError):
        check_output("@\u200d-", at_end_only_if_cut=False)
    with pytest.raises(AssertionError):
        check_output("x \u2066y", at_end_only_if_cut=False)


# --- Token reach -------------------------------------------------------------------


def test_sanitizer_cannot_reach_gh() -> None:
    import pathlib

    lib = pathlib.Path(sanitize.__file__).resolve().parent
    modules = {
        f"agent_review.{p.stem}": p.read_text(encoding="utf-8") for p in lib.glob("*.py") if p.stem != "__init__"
    }
    modules["agent_review"] = (lib / "__init__.py").read_text(encoding="utf-8")
    closure = import_closure((lib / "sanitize.py").read_text(encoding="utf-8"), modules)
    assert "agent_review.gh" not in closure
    assert "agent_review.runner" not in closure
