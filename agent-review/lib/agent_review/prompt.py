"""The review prompt (``run-review``, fetch phase, inside the sandbox).

The template is trusted; so is the caller's addendum, substituted first so
that placeholders in trusted text are expanded like the template's own
(``load_config`` already rejected ``{{`` in it). The pull request's title
and body are untrusted data: they come from ``pr.json``, the raw API
response, parsed only here inside the sandbox (invariant 2 is about the
host), and are substituted last, so placeholders inside them are not
expanded, between delimiters that carry a random nonce the PR cannot know
(and which is removed from them anyway).

The output is byte for byte what the bash version wrote, so the rendering
mirrors its shell steps on bytes:

- ``$(...)`` drops NUL bytes and every trailing newline (``substituted``);
- ``clip`` is ``head -c N | iconv -f UTF-8 -t UTF-8 -c``: a byte cap, then
  invalid or cut multi-byte sequences dropped;
- ``jq -r '.x // "" | tostring'``: null and false become empty, strings
  are themselves, other values their compact JSON; jq prints a newline
  after the value, which the cap counts;
- the substitutions are sequential, global and literal, in the bash order.
"""

import json
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent_review import files
from agent_review.reset import CONTEXT_NAME
from agent_review.review_errors import Fatal

PR_TITLE_MAX_BYTES = 512
PR_BODY_MAX_BYTES = 16384
ADDENDUM_PROMPT_MAX_BYTES = 8192
# pr.json is the API response for one pull request; GitHub caps the body
# at 65536 characters, so this cap is far above any real response.
PR_JSON_MAX_BYTES = 8 << 20
_SURROGATE = re.compile("[\ud800-\udfff]")


def substituted(data: bytes) -> bytes:
    """What bash's ``$(...)`` keeps of a command's output."""
    return data.replace(b"\0", b"").rstrip(b"\n")


def clip(data: bytes, max_bytes: int) -> bytes:
    """The first ``max_bytes`` bytes, without invalid or cut UTF-8 sequences."""
    return data[:max_bytes].decode("utf-8", errors="ignore").encode("utf-8")


def jq_text(value: Any) -> str:
    """``jq -r '. // "" | tostring'`` of a parsed JSON value."""
    if value is None or value is False:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=True)


def _utf8(text: str) -> bytes:
    # A lone surrogate escape ('\ud800') parses in Python but cannot be
    # encoded; jq rejects the whole document. Replace it, as decoders do.
    return _SURROGATE.sub("\ufffd", text).encode("utf-8")


def title_bytes(title: str) -> bytes:
    """jq -r | tr -d '\\r' | head -n 1 | clip 512, then ``$(...)``."""
    data = (_utf8(title) + b"\n").replace(b"\r", b"")
    first_line = data[: data.index(b"\n") + 1]
    return substituted(clip(first_line, PR_TITLE_MAX_BYTES))


def body_bytes(body: str) -> bytes:
    """jq -r | tr -d '\\r' | clip 16384, then ``$(...)``."""
    data = (_utf8(body) + b"\n").replace(b"\r", b"")
    return substituted(clip(data, PR_BODY_MAX_BYTES))


def read_pr(path: str) -> tuple[bytes, bytes]:
    """The PR's title and body from the raw API response, as prompt bytes."""
    try:
        data = files.read_bytes(path, PR_JSON_MAX_BYTES)
        # Invalid UTF-8 becomes U+FFFD, as jq did: the title and body are
        # data for the prompt, and an odd byte must not stop the review.
        pr = json.loads(data.decode("utf-8", errors="replace"))
    except (OSError, ValueError, RecursionError) as e:
        raise Fatal(f"Cannot read the pull request from '{path}': {e}") from e
    if not isinstance(pr, dict):
        raise Fatal(f"'{path}' does not hold a JSON object.")
    return title_bytes(jq_text(pr.get("title"))), body_bytes(jq_text(pr.get("body")))


def rules_bytes(rule_files: Sequence[str]) -> bytes:
    if not rule_files:
        return b"No project rule files are configured."
    lines = (f"{i}. `{f}`, if it exists." for i, f in enumerate(rule_files, start=1))
    return substituted("\n".join(lines).encode("utf-8"))


def addendum_bytes(addendum: bytes) -> bytes:
    """``$(printf '### Project notes\\n\\n%s\\n' "$(clip 8192 <file)")``, or empty without an addendum."""
    if not addendum:
        return b""
    inner = substituted(clip(addendum, ADDENDUM_PROMPT_MAX_BYTES))
    return substituted(b"### Project notes\n\n" + inner + b"\n")


def new_nonce() -> str:
    return secrets.token_hex(12)


@dataclass(frozen=True)
class PromptValues:
    project: str
    repo: str
    pr_number: str
    head_sha: str
    base_sha: str
    merge_base: str
    rules: bytes
    addendum: bytes
    title: bytes
    body: bytes


def render(template: bytes, values: PromptValues, nonce: str) -> bytes:
    """The prompt file's contents (with its final newline)."""
    marker = nonce.encode("ascii")
    # The PR cannot know the nonce, but strip it anyway so the delimiters stay unique.
    title = values.title.replace(marker, b"")
    body = values.body.replace(marker, b"")
    prompt = substituted(template)
    # Trusted caller text first, so that placeholders inside it are expanded
    # like the template's own; untrusted values last, so placeholders inside
    # them are not expanded.
    for name, value in (
        ("ADDENDUM", values.addendum),
        ("RULES", values.rules),
        ("PROJECT", values.project.encode("utf-8")),
        ("REPO", values.repo.encode("utf-8")),
        ("PR_NUMBER", values.pr_number.encode("utf-8")),
        ("HEAD_SHA", values.head_sha.encode("utf-8")),
        ("BASE_SHA", values.base_sha.encode("utf-8")),
        ("MERGE_BASE", values.merge_base.encode("utf-8")),
        ("CONTEXT_DIR", CONTEXT_NAME.encode("utf-8")),
        ("NONCE", marker),
        ("PR_TITLE", title),
        ("PR_BODY", body),
    ):
        prompt = prompt.replace(b"{{" + name.encode("ascii") + b"}}", value)
    return prompt + b"\n"
