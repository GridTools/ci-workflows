"""Workflow commands, step outputs and log lines.

- Workflow commands (``::notice::`` and the like) escape ``%``, CR and LF in
  their data, so a value cannot end the command and start another one.
- ``set_output`` writes ``name=value`` lines to ``$GITHUB_OUTPUT`` and
  rejects CR and LF in values: a newline would let a value set other
  outputs. The multi-line (heredoc) form is not offered.
- ``log_lines`` prefixes every line of text that may be untrusted, so none
  can start a workflow command; it never uses ``::stop-commands::``, whose
  token would be printed to the live log first.

Everything is written as strict UTF-8 bytes, never in the locale's
encoding.
"""

import os
import sys
from typing import BinaryIO

from agent_review import files, validate

COMMANDS = ("notice", "warning", "error")
OUTPUT_NAME = validate.pattern(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
PROPERTY_NAME = validate.pattern(r"[a-zA-Z]{1,16}")


def escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    return escape_data(text).replace(":", "%3A").replace(",", "%2C")


def _stdout() -> BinaryIO:
    return sys.stdout.buffer


def _write(stream: BinaryIO, line: str) -> None:
    stream.write(line.encode("utf-8", errors="strict"))
    stream.flush()


def command(name: str, message: str, properties: dict[str, str] | None = None) -> str:
    """One workflow command line, with a trailing newline."""
    if name not in COMMANDS:
        raise ValueError(f"unsupported workflow command {name!r}")
    props = ""
    if properties:
        for key in properties:
            validate.require(PROPERTY_NAME, key, "command property")
        props = " " + ",".join(f"{k}={escape_property(v)}" for k, v in properties.items())
    return f"::{name}{props}::{escape_data(message)}\n"


def notice(message: str, *, title: str | None = None, stream: BinaryIO | None = None) -> None:
    _write(stream or _stdout(), command("notice", message, {"title": title} if title else None))


def warning(message: str, *, title: str | None = None, stream: BinaryIO | None = None) -> None:
    _write(stream or _stdout(), command("warning", message, {"title": title} if title else None))


def error(message: str, *, title: str | None = None, stream: BinaryIO | None = None) -> None:
    _write(stream or _stdout(), command("error", message, {"title": title} if title else None))


def log_lines(text: str, prefix: str, *, stream: BinaryIO | None = None) -> None:
    """Writes ``text`` with ``prefix`` before every line; CRs are dropped."""
    if not prefix or prefix.lstrip().startswith(":") or "\n" in prefix or "\r" in prefix:
        raise ValueError(f"invalid log prefix {prefix!r}")
    lines = text.replace("\r", "").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    _write(stream or _stdout(), "".join(f"{prefix}{line}\n" for line in lines))


def output_line(name: str, value: str) -> str:
    """The ``name=value`` line for ``$GITHUB_OUTPUT``."""
    validate.require(OUTPUT_NAME, name, "output name")
    if "\n" in value or "\r" in value:
        raise ValueError(f"the value of output {name!r} contains a line break")
    return f"{name}={value}\n"


def set_output(name: str, value: str, *, path: str | None = None) -> None:
    """Appends ``name=value`` to ``$GITHUB_OUTPUT`` (or ``path``)."""
    target = path if path is not None else os.environ.get("GITHUB_OUTPUT", "")
    if not target.startswith("/"):
        raise ValueError("GITHUB_OUTPUT is not an absolute path")
    files.append_bytes(target, output_line(name, value).encode("utf-8", errors="strict"))
