"""The review sanitiser of ``publish-comment`` (invariant 5 in docs/agent-review.md).

The review is model output that the pull request can steer, so it is
treated as hostile text that GitHub will render as Markdown in a comment
posted with a write token:

- it must be valid UTF-8 (strict: no surrogates, nothing above U+10FFFF);
- C0 control characters except tab and newline, and DEL, are dropped;
- invisible characters are dropped: the explicit ranges of ``INVISIBLE``
  (zero-width, bidirectional controls, variation selectors, tag
  characters, ...) and every character of Unicode category ``Cf``. The
  ranges are kept although most of them are ``Cf``: variation selectors are
  ``Mn``, U+2065 is unassigned (``Cn``) in Python's Unicode 15.0, and the
  category of a character GitHub knows but Python does not is ``Cn``;
- markup that GitHub renders as nothing is broken up with a zero-width
  space (ZWSP): HTML comments (so the review cannot imitate the marker of
  this workflow's comment), processing instructions (``<?``), declarations
  and CDATA (``<!``), and link reference definitions (``[label]: url``);
- character references are escaped so they render literally: named ones
  (``&commat;``, ``&zwj;``, ``&rlm;``), which GitHub decodes into mentions or
  invisible characters, and numeric ones (``&#64;``), with a ZWSP after the
  ``#`` so that the escaped form is no issue reference like ``#64``;
- ``@mentions`` get a zero-width joiner (ZWJ) after the ``@``;
- the result is cut to ``REVIEW_MAX_BYTES`` on a character boundary.

The invisible characters go first, so removing them cannot join a delimiter
that a later replacement would miss (``<`` U+200B ``!--``). The later
replacements cannot create new matches: none inserts ``<``, ``>``, ``&``,
``]`` or ``@``, and the named-reference rule runs before the numeric one.
The order and patterns are those of the bash version this replaces.

Pure functions on bytes and text: this module runs no program and has no
token (its import closure excludes ``agent_review.gh``).
"""

import re
import unicodedata

REVIEW_MAX_BYTES = 60000
ZWSP = "\u200b"  # zero-width space
ZWJ = "\u200d"  # zero-width joiner

# Inclusive code point ranges, as the bash version's UTF-8 byte patterns
# matched them (U+E0100-U+E01FF: the byte pattern covers the whole block,
# not only the selectors up to U+E01EF).
INVISIBLE: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),  # soft hyphen
    (0x061C, 0x061C),  # Arabic letter mark
    (0x180E, 0x180E),  # Mongolian vowel separator
    (0x200B, 0x200F),  # zero-width space, (non-)joiner, LRM, RLM
    (0x202A, 0x202E),  # bidirectional embeddings and overrides
    (0x2060, 0x206F),  # word joiner, invisible operators, isolates, ...
    (0xFE00, 0xFE0F),  # variation selectors
    (0xFEFF, 0xFEFF),  # byte order mark
    (0xE0000, 0xE007F),  # tag characters
    (0xE0100, 0xE01FF),  # variation selectors supplement
)
# Kept: tab and newline. Dropped: the rest of C0, and DEL.
CONTROLS: tuple[tuple[int, int], ...] = ((0x00, 0x08), (0x0B, 0x1F), (0x7F, 0x7F))

_DROPPED_BY_RANGE = re.compile(
    "[" + "".join(f"\\U{lo:08x}-\\U{hi:08x}" for lo, hi in (*CONTROLS, *INVISIBLE)) + "]", re.ASCII
)

# The replacements, in the order of the bash version's 'sed' chain.
_PI_OR_DECLARATION = re.compile(r"<([!?])", re.ASCII)
_MENTION = re.compile(r"@([A-Za-z0-9])", re.ASCII)
_NAMED_REFERENCE = re.compile(r"&([A-Za-z][A-Za-z0-9]*;)", re.ASCII)


class InvalidReviewError(ValueError):
    """The review is not valid UTF-8."""


def is_dropped(char: str) -> bool:
    """True for a character that ``clean`` removes."""
    return _DROPPED_BY_RANGE.fullmatch(char) is not None or unicodedata.category(char) == "Cf"


def drop_invisible(text: str) -> str:
    """``text`` without control characters (but tab and newline) and invisible ones."""
    text = _DROPPED_BY_RANGE.sub("", text)
    # Cf outside the ranges; only non-ASCII characters can be Cf.
    if not text.isascii():
        text = "".join(c for c in text if c < "\x80" or unicodedata.category(c) != "Cf")
    return text


def neutralise(text: str) -> str:
    """Breaks up hidden markup, escapes character references, neutralises mentions."""
    text = _PI_OR_DECLARATION.sub(f"<{ZWSP}\\1", text)
    text = text.replace("-->", f"--{ZWSP}>")
    text = text.replace("]:", f"]{ZWSP}:")
    text = _MENTION.sub(f"@{ZWJ}\\1", text)
    text = _NAMED_REFERENCE.sub("&amp;\\1", text)
    return text.replace("&#", f"&amp;#{ZWSP}")


def clean(text: str) -> str:
    """The sanitised ``text``, before the size cap."""
    return neutralise(drop_invisible(text))


def cut(data: bytes, max_bytes: int) -> bytes:
    """``data`` (valid UTF-8) cut to at most ``max_bytes``, never inside a character."""
    if len(data) <= max_bytes:
        return data
    end = max_bytes
    # Back off over continuation bytes to the start of the cut character.
    while end > 0 and data[end] & 0xC0 == 0x80:
        end -= 1
    return data[:end]


def decode(data: bytes) -> str:
    """``data`` as strict UTF-8, or InvalidReviewError."""
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as e:
        raise InvalidReviewError("the review is not valid UTF-8.") from e


def sanitize(data: bytes, max_bytes: int = REVIEW_MAX_BYTES) -> bytes:
    """The sanitised review, as UTF-8 bytes of at most ``max_bytes``.

    Raises InvalidReviewError if ``data`` is not valid UTF-8.
    """
    return cut(clean(decode(data)).encode("utf-8", errors="strict"), max_bytes)
