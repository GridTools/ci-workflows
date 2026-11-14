"""agent_review.validate: bash-like matching, without Python's looser defaults."""

import re

import pytest

from agent_review import validate


def test_fullmatch_rejects_a_trailing_newline() -> None:
    # re.match(r'^[0-9a-f]{40}$', sha + '\n') would match.
    sha = "a" * 40
    assert validate.matches(validate.SHA, sha)
    assert re.match(r"^[0-9a-f]{40}$", sha + "\n")
    assert not validate.matches(validate.SHA, sha + "\n")


@pytest.mark.parametrize("value", ["\uff11\uff12", "\u0661\u0662", "1_0", " 7", "7\n", "+7", "-7", "", "0x10", "1e3"])
def test_parse_int_rejects_what_int_accepts(value: str) -> None:
    with pytest.raises(validate.ValidationError):
        validate.parse_int(value, "number")


def test_int_traps_exist() -> None:
    # The reason parse_int validates first.
    assert int("\uff11\uff12") == 12
    assert int("1_0") == 10
    assert int(" 7\n") == 7


def test_parse_int() -> None:
    assert validate.parse_int("0042", "number") == 42
    assert validate.parse_int("7", "number", digits=validate.POSITIVE_INT) == 7
    with pytest.raises(validate.ValidationError):
        validate.parse_int("07", "number", digits=validate.POSITIVE_INT)
    with pytest.raises(validate.ValidationError):
        validate.parse_int("101", "cap", maximum=100)
    with pytest.raises(validate.ValidationError):
        validate.parse_int("1" * 11, "number")


def test_classes_are_ascii_only() -> None:
    word = validate.pattern(r"\w+")
    assert validate.matches(word, "abc_1")
    assert not validate.matches(word, "é")
    assert not validate.matches(validate.pattern(r"\d+"), "\u0661")


def test_pattern_refuses_anchors_and_unicode_patterns() -> None:
    with pytest.raises(ValueError, match="anchors"):
        validate.pattern(r"^[a-z]+$")
    with pytest.raises(ValueError, match=r"validate\.pattern"):
        validate.matches(re.compile(r"[a-z]+"), "abc")


def test_require() -> None:
    assert validate.require(validate.REPO, "example-org/example", "repository") == "example-org/example"
    with pytest.raises(validate.ValidationError, match="Invalid repository"):
        validate.require(validate.REPO, "example-org/example/extra", "repository")


@pytest.mark.parametrize(
    ("compiled", "good", "bad"),
    [
        (validate.POSITIVE_INT, ["1", "2983", "9999999999"], ["0", "01", "10000000000"]),
        (validate.REPO, ["a/b", "Example-Org/repo.x"], ["a", "a/b/c", "a /b", "a/b\n"]),
        (validate.HARNESS, ["stub", "open-code"], ["Stub", "1x", "-x", "a" * 33]),
        (validate.MODEL, ["zai-org/GLM-5.3-Flash", "m:1"], ["-m", "m m", "/m"]),
        (validate.PROJECT, ["ExampleProj", "proj 4x"], [" x", "x\ty", "a" * 65]),
    ],
)
def test_shared_patterns(compiled: re.Pattern[str], good: list[str], bad: list[str]) -> None:
    assert all(validate.matches(compiled, v) for v in good)
    assert not any(validate.matches(compiled, v) for v in bad)
