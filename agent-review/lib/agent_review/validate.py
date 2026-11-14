"""Validation helpers that behave like bash's ``[[ =~ ^...$ ]]`` checks.

Python's defaults accept more than bash does:

- ``re.match(r'^[0-9a-f]{4}$', 'abcd\\n')`` matches (``$`` before a final
  newline), so patterns are only ever applied with ``re.fullmatch``;
- ``\\d``, ``\\w`` and ``\\s`` match non-ASCII characters unless the pattern
  is compiled with ``re.ASCII``, which ``pattern()`` always does;
- ``int()`` accepts full-width digits, underscores and surrounding
  whitespace (``int(' 1_0\\n')`` is 10), so numbers are matched against a
  pattern first (``parse_int``).

Patterns shared by several entry points are defined here, with the bash
patterns they mirror.
"""

import re


class ValidationError(ValueError):
    """A value does not match its pattern."""


def pattern(regex: str) -> re.Pattern[str]:
    """Compiles a pattern for ``fullmatch`` with ASCII-only classes."""
    if regex.startswith("^") or regex.endswith("$"):
        raise ValueError("patterns are applied with fullmatch; drop the anchors")
    return re.compile(regex, re.ASCII)


def matches(compiled: re.Pattern[str], value: str) -> bool:
    """True if all of ``value`` matches ``compiled``."""
    if not compiled.flags & re.ASCII:
        raise ValueError("compile patterns with validate.pattern()")
    return isinstance(value, str) and compiled.fullmatch(value) is not None


def require(compiled: re.Pattern[str], value: str, what: str) -> str:
    """Returns ``value`` if it matches, else raises ValidationError."""
    if not matches(compiled, value):
        raise ValidationError(f"Invalid {what} {value!r}.")
    return value


def parse_int(value: str, what: str, *, digits: re.Pattern[str] | None = None, maximum: int | None = None) -> int:
    """Parses a decimal integer, validated by ``digits`` before ``int()``.

    The default pattern is ``[0-9]{1,10}``; pass a stricter one to reject a
    leading zero, as ``POSITIVE_INT`` does.
    """
    require(digits or DIGITS, value, what)
    number = int(value, 10)
    if maximum is not None and number > maximum:
        raise ValidationError(f"Invalid {what} {value!r}: more than {maximum}.")
    return number


DIGITS = pattern(r"[0-9]{1,10}")
# bash: ^[1-9][0-9]{0,9}$ (pull request numbers)
POSITIVE_INT = pattern(r"[1-9][0-9]{0,9}")
# bash: ^[0-9a-f]{40}$
SHA = pattern(r"[0-9a-f]{40}")
# bash: ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$
REPO = pattern(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# bash: ^[a-z][a-z0-9-]{0,31}$
HARNESS = pattern(r"[a-z][a-z0-9-]{0,31}")
# bash: ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$
MODEL = pattern(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
# bash: ^[A-Za-z0-9][A-Za-z0-9._\ -]{0,63}$
PROJECT = pattern(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}")
