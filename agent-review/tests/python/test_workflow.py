"""agent_review.workflow: workflow commands, step outputs and log lines."""

import io
import os
import pathlib

import pytest

from agent_review import workflow


def test_escape_data() -> None:
    assert workflow.escape_data("100%\r\n::error::x") == "100%25%0D%0A::error::x"


def test_command_cannot_be_split() -> None:
    out = io.BytesIO()
    workflow.notice("a\n::error::injected", stream=out)
    assert out.getvalue() == b"::notice::a%0A::error::injected\n"
    assert out.getvalue().count(b"\n") == 1


def test_command_properties_are_escaped() -> None:
    out = io.BytesIO()
    workflow.warning("msg", title="a:b,c\nd", stream=out)
    assert out.getvalue() == b"::warning title=a%3Ab%2Cc%0Ad::msg\n"


def test_only_known_commands() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        workflow.command("stop-commands", "token")
    with pytest.raises(ValueError, match="unsupported"):
        workflow.command("add-mask", "x")


def test_output_is_utf8_bytes() -> None:
    out = io.BytesIO()
    workflow.error("é", stream=out)
    assert out.getvalue() == "::error::é\n".encode()
    with pytest.raises(UnicodeEncodeError):
        workflow.error("\udcff", stream=io.BytesIO())


def test_log_lines_prefix_every_line() -> None:
    out = io.BytesIO()
    workflow.log_lines("one\r\n::error::two\n\nthree", "awf| ", stream=out)
    assert out.getvalue() == b"awf| one\nawf| ::error::two\nawf| \nawf| three\n"


def test_log_lines_break_legacy_commands() -> None:
    out = io.BytesIO()
    workflow.log_lines("x ##[warning]fake\n##[add-mask]v ##[set-output name=a;]b", "awf| ", stream=out)
    text = out.getvalue().decode()
    assert "##[" not in text
    assert text == "awf| x #\u200b#[warning]fake\nawf| #\u200b#[add-mask]v #\u200b#[set-output name=a;]b\n"


@pytest.mark.parametrize("prefix", ["", "::", " ::x", "a\nb", " x", "\u00a0x", "\tx", ":x", "a\x00", "##[x", "a::b"])
def test_log_lines_rejects_unsafe_prefixes(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        workflow.log_lines("x", prefix, stream=io.BytesIO())


@pytest.mark.parametrize("value", ["a\nb=c", "a\rb", "\n"])
def test_output_rejects_line_breaks(value: str) -> None:
    with pytest.raises(ValueError, match="line break"):
        workflow.output_line("name", value)


@pytest.mark.parametrize("name", ["", "a=b", "a b", "-a", "a\n", "é"])
def test_output_rejects_bad_names(name: str) -> None:
    with pytest.raises(ValueError, match="Invalid output name"):
        workflow.output_line(name, "v")


def test_set_output_appends(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "output"
    target.write_bytes(b"first=1\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(target))
    workflow.set_output("allowed", "true")
    workflow.set_output("model", "zai-org/GLM-5.3-Flash")
    assert target.read_bytes() == b"first=1\nallowed=true\nmodel=zai-org/GLM-5.3-Flash\n"


def test_set_output_refuses_a_symlink(tmp_path: pathlib.Path) -> None:
    real = tmp_path / "real"
    real.write_bytes(b"")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(OSError):
        workflow.set_output("a", "b", path=str(link))
    assert real.read_bytes() == b""


def test_set_output_refuses_a_fifo_without_blocking(tmp_path: pathlib.Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(OSError):
        workflow.set_output("a", "b", path=str(fifo))


def test_set_output_needs_an_absolute_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    with pytest.raises(ValueError, match="absolute"):
        workflow.set_output("a", "b")
    with pytest.raises(ValueError, match="absolute"):
        workflow.set_output("a", "b", path="relative/out")
