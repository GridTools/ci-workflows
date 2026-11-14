"""agent_review.gh: the 'gh api' argument order the suites' fakes rely on."""

import os
import pathlib

import pytest

from agent_review import gh, runner


@pytest.fixture
def fake_gh(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A fake 'gh' first on PATH that logs its arguments (one per line) and stdin."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    script = bindir / "gh"
    script.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" >"${FAKE_DIR}/args"\n'
        'cat >"${FAKE_DIR}/stdin"\n'
        '[ -z "${FAKE_FAIL:-}" ] || { echo "HTTP 502" >&2; exit 1; }\n'
        "echo answer\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(tmp_path))
    return tmp_path


def args(directory: pathlib.Path) -> list[str]:
    return (directory / "args").read_text().splitlines()


def test_get_with_jq(fake_gh: pathlib.Path) -> None:
    out = gh.api("repos/o/r/pulls/7", jq="[.head.sha, .base.sha] | @tsv")
    assert out == b"answer\n"
    # test-authorize.sh's fake matches the endpoint as "$2".
    assert args(fake_gh) == ["api", "repos/o/r/pulls/7", "--jq", "[.head.sha, .base.sha] | @tsv"]
    assert (fake_gh / "stdin").read_bytes() == b""


def test_paginate(fake_gh: pathlib.Path) -> None:
    gh.api("repos/o/r/issues/9/comments?per_page=100", jq=".[]", paginate=True)
    assert args(fake_gh) == ["api", "--paginate", "repos/o/r/issues/9/comments?per_page=100", "--jq", ".[]"]


def test_write_with_body(fake_gh: pathlib.Path) -> None:
    gh.api("repos/o/r/issues/comments/11", method="PATCH", body=b'{"body": "x"}', jq=".html_url")
    # test-publish-comment.sh's fake matches "api -X " and logs "$3 $4".
    assert args(fake_gh) == ["api", "-X", "PATCH", "repos/o/r/issues/comments/11", "--input", "-", "--jq", ".html_url"]
    assert (fake_gh / "stdin").read_bytes() == b'{"body": "x"}'


def test_failure(fake_gh: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FAIL", "1")
    with pytest.raises(runner.CommandError) as e:
        gh.api("repos/o/r/pulls/7")
    assert e.value.stderr == b"HTTP 502\n"


@pytest.mark.parametrize(
    "endpoint",
    [
        "-X",
        "--hostname=evil",
        "https://evil.example/x",
        "/repos/o/r",
        "repos/o/r/../../user",
        "repos//o",
        "repos/o/r pulls",
        "repos/o/r\n",
        "repos/é",
        "",
    ],
)
def test_endpoint_validation(fake_gh: pathlib.Path, endpoint: str) -> None:
    with pytest.raises(ValueError, match="API endpoint"):
        gh.api(endpoint)
    assert not (fake_gh / "args").exists()


def test_method_validation(fake_gh: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="unsupported method"):
        gh.api("repos/o/r", method="get")
