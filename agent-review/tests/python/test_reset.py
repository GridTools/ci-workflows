"""agent_review.reset: configuration, coverage by git pathspecs, the reset and its post-condition (S4)."""

import os
import pathlib
import re
import subprocess
from collections.abc import Mapping

import pytest

from agent_review import reset
from agent_review.review_errors import Fatal
from agent_review.sandbox_git import Git

COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}


def git(repo: pathlib.Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env={**os.environ, **COMMIT_ENV},
        input=stdin,
        capture_output=True,
        check=True,
    ).stdout


def write_tree(repo: pathlib.Path, tree: Mapping[str, str | None]) -> None:
    """Applies ``tree`` to the work tree: text, '-> target' for a symlink, None to delete."""
    for rel, content in tree.items():
        path = repo / rel
        if content is None:
            git(repo, "rm", "-r", "-q", "--", rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.is_file():
            path.unlink()
        if content.startswith("-> "):
            path.symlink_to(content[3:])
        else:
            path.write_text(content)
        git(repo, "add", "--", rel)


def commit(repo: pathlib.Path, tree: Mapping[str, str | None]) -> str:
    write_tree(repo, tree)
    git(repo, "commit", "-q", "--allow-empty", "-m", "c")
    return git(repo, "rev-parse", "HEAD").decode().strip()


@pytest.fixture
def pr(tmp_path: pathlib.Path) -> "PR":
    return PR(tmp_path)


class PR:
    """A checkout of a crafted pull request: base and head commits, the head checked out."""

    def __init__(self, tmp: pathlib.Path) -> None:
        self.ws = tmp / "ws"
        self.repo = self.ws / "repo"
        self.repo.mkdir(parents=True)
        git(self.repo, "init", "-q")
        self.context = self.repo / reset.CONTEXT_NAME
        self.git = Git(str(self.repo), {})

    def make(self, base: Mapping[str, str | None], head: Mapping[str, str | None]) -> None:
        self.base = commit(self.repo, base)
        self.head = commit(self.repo, head)
        git(self.repo, "checkout", "-q", "--detach", self.head)
        (self.context / "pr-instruction-files").mkdir(parents=True)

    def reset(self, patterns: list[str]) -> None:
        reset.reset_instruction_files(
            self.git,
            str(self.ws),
            head_sha=self.head,
            base_sha=self.base,
            patterns=patterns,
            context_dir=str(self.context),
        )

    def verify(self, rule_files: list[str], named: list[str] | None = None) -> str:
        return reset.verify_followed_files(self.git, str(self.repo), self.base, rule_files, named or [])

    def kept(self) -> dict[str, str]:
        root = self.context / "pr-instruction-files"
        return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


# --- Configuration ---------------------------------------------------------------


def test_read_list_trims_and_skips_comments(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "list"
    path.write_bytes(b"# comment\n  AGENTS.md \r\n\n\t.claude/\n   # indented comment\nlast")
    assert reset.read_list(str(path)) == ["AGENTS.md", ".claude/", "last"]
    assert reset.read_list(str(tmp_path / "missing")) == []


@pytest.mark.parametrize("data", [b"a\0b\n", b"\xff\n"])
def test_read_list_rejects_nul_and_invalid_utf8(tmp_path: pathlib.Path, data: bytes) -> None:
    path = tmp_path / "list"
    path.write_bytes(data)
    with pytest.raises(Fatal):
        reset.read_list(str(path))


@pytest.mark.parametrize("entry", ["/etc/passwd", ":(top)x", "../x", "a/../b", "a..b"])
def test_check_entry_rejects(entry: str) -> None:
    with pytest.raises(Fatal, match="Invalid configured path or pattern"):
        reset.check_entry(entry)


def test_pathspecs() -> None:
    assert reset.pathspecs(["AGENTS.md", "", ".claude/"]) == [
        ":(glob,icase)**/AGENTS.md",
        ":(glob,icase)**/.claude/**",
    ]


@pytest.mark.parametrize(
    ("path", "tree", "plain"),
    [
        ("a/b.md", True, True),
        ("a/./b.md", False, False),
        ("a/../b.md", False, False),
        ("a//b.md", False, False),
        (".", False, False),
        ("", False, False),
        ("docs/*.md", True, False),
        ("a?.md", True, False),
        ("[a].md", True, False),
        ("...md", True, True),
    ],
)
def test_path_checks(path: str, tree: bool, plain: bool) -> None:
    assert reset.is_tree_path(path) is tree
    assert reset.is_plain_path(path) is plain


def make_root(
    tmp: pathlib.Path, rules: str = "", extra: str = "", addendum: bytes = b"", central: str = "AGENTS.md\n"
) -> str:
    root = tmp / "root"
    (root / "config").mkdir(parents=True)
    (root / "instruction-files.txt").write_text(central)
    (root / "config" / "rule-files.txt").write_text(rules)
    (root / "config" / "extra-instruction-files.txt").write_text(extra)
    (root / "config" / "prompt-addendum.md").write_bytes(addendum)
    return str(root)


def test_load_config(tmp_path: pathlib.Path) -> None:
    addendum = (
        b"Follow `docs/style.md`, `b.yml`, `c.yaml`, `CODING.md` and `docs/style.md`;\n"
        b"not `two words.md`, `x.py`, ``, `a.md\nb.md` or `.md`. Also `x.jsonc`, `y.mdc`.\n"
    )
    config = reset.load_config(make_root(tmp_path, "CODING.md\n", "docs/\n", addendum))
    assert config.rule_files == ("CODING.md",)
    assert config.reset_patterns == ("AGENTS.md", "docs/", "CODING.md")
    assert config.addendum == addendum
    # Sorted bytewise and unique, as 'sort -u' in the C locale.
    assert config.addendum_named == ("CODING.md", "b.yml", "c.yaml", "docs/style.md", "x.jsonc", "y.mdc")


def test_load_config_without_addendum(tmp_path: pathlib.Path) -> None:
    config = reset.load_config(make_root(tmp_path))
    assert (config.addendum, config.addendum_named, config.rule_files) == (b"", (), ())


def test_load_config_rejects_a_placeholder_in_the_addendum(tmp_path: pathlib.Path) -> None:
    with pytest.raises(Fatal, match=r"The prompt addendum contains '\{\{'; placeholders are not allowed there\."):
        reset.load_config(make_root(tmp_path, addendum=b"x {{PR_BODY}}"))


def test_load_config_checks_rule_files_first(tmp_path: pathlib.Path) -> None:
    with pytest.raises(Fatal, match=re.escape("'../etc/passwd'")):
        reset.load_config(make_root(tmp_path, "../etc/passwd\n", "/abs\n"))
    with pytest.raises(Fatal, match="'/abs'"):
        reset.load_config(make_root(tmp_path / "2", "ok.md\n", "/abs\n"))


# --- Coverage (git pathspecs on a scratch index) ------------------------------------


@pytest.mark.parametrize(
    ("patterns", "path", "covered"),
    [
        (["AGENTS.md"], "AGENTS.md", True),
        (["AGENTS.md"], "src/pkg/AGENTS.md", True),
        (["AGENTS.md"], "src/pkg/agents.md", True),  # icase, as the reset
        (["AGENTS.md"], "NOT_AGENTS.md", False),
        (["docs/style/"], "docs/style/naming.md", True),
        (["docs/style/"], "a/docs/style/b/c.md", True),
        (["docs/style/"], "docs/style.md", False),
        (["next/*.md"], "src/gt4py/next/AGENTS.md", True),
        # '*' does not cross '/' (bash's matching said it did: finding S4).
        (["gt4py/*.md"], "src/gt4py/next/AGENTS.md", False),
        (["**/*.md"], "a/b/c.md", True),
        ([".github/copilot-instructions.md"], ".github/copilot-instructions.md", True),
        ([], "AGENTS.md", False),
        ([""], "AGENTS.md", False),
    ],
)
def test_coverage(tmp_path: pathlib.Path, patterns: list[str], path: str, covered: bool) -> None:
    with reset.Coverage(str(tmp_path), {}, patterns) as coverage:
        assert coverage.covers(path) is covered
    assert list(tmp_path.iterdir()) == []  # the scratch directory is gone


def test_coverage_checks_each_path_on_its_own(tmp_path: pathlib.Path) -> None:
    # 'a.md' as a file and as a directory could not share one index.
    with reset.Coverage(str(tmp_path), {}, ["*.md"]) as coverage:
        assert coverage.covers("a.md")
        assert coverage.covers("a.md/b.md")
        assert not coverage.covers("a.txt")


def test_check_coverage_messages(tmp_path: pathlib.Path) -> None:
    def config(named: tuple[str, ...]) -> reset.Config:
        return reset.Config((), ("AGENTS.md", "docs/"), b"x", named)

    reset.check_coverage(config(("AGENTS.md", "docs/a/b.md")), str(tmp_path), {})
    with pytest.raises(Fatal, match=r"names 'style\.md', which no reset pattern covers; add it to rule_files\."):
        reset.check_coverage(config(("AGENTS.md", "style.md")), str(tmp_path), {})
    with pytest.raises(Fatal, match=r"names 'docs/\*\.md', which is not a plain repository path\."):
        reset.check_coverage(config(("docs/*.md",)), str(tmp_path), {})
    # In order: the first problem is reported, as the bash loop did.
    with pytest.raises(Fatal, match=re.escape("'a.md', which no reset pattern covers")):
        reset.check_coverage(config(("a.md", "b/*.md")), str(tmp_path), {})
    # A path git refuses in an index ('Ignoring path') is not covered: fails closed.
    with pytest.raises(Fatal, match=r"names '\.git/AGENTS\.md', which no reset pattern covers"):
        reset.check_coverage(config((".git/AGENTS.md",)), str(tmp_path), {})
    assert list(tmp_path.iterdir()) == []


# --- The reset ---------------------------------------------------------------------

BASE = {
    "AGENTS.md": "base rules\n",
    "src/pkg/AGENTS.md": "base nested\n",
    "src/pkg/code.py": "print(1)\n",
    ".claude/settings.json": "{}\n",
    "docs/RULES.md": "base doc rules\n",
}


def test_reset_restores_base_and_keeps_pr_versions_as_data(pr: PR) -> None:
    pr.make(
        BASE,
        {
            "AGENTS.md": "PR rules\n",  # modified
            "src/pkg/AGENTS.md": None,  # deleted
            "src/new/CLAUDE.md": "PR claude\n",  # added, nested
            ".claude/settings.json": '{"hooks": 1}\n',  # under a directory pattern
            ".claude/agents/x.md": "PR agent\n",
            "src/pkg/code.py": "print(2)\n",  # not an instruction file
        },
    )
    pr.reset(["AGENTS.md", "CLAUDE.md", ".claude/"])
    assert (pr.repo / "AGENTS.md").read_text() == "base rules\n"
    assert (pr.repo / "src/pkg/AGENTS.md").read_text() == "base nested\n"
    assert not (pr.repo / "src/new/CLAUDE.md").exists()
    assert (pr.repo / ".claude/settings.json").read_text() == "{}\n"
    assert not (pr.repo / ".claude/agents/x.md").exists()
    assert (pr.repo / "src/pkg/code.py").read_text() == "print(2)\n"
    assert pr.kept() == {
        "AGENTS.md.txt": "PR rules\n",
        "src/new/CLAUDE.md.txt": "PR claude\n",
        ".claude/settings.json.txt": '{"hooks": 1}\n',
        ".claude/agents/x.md.txt": "PR agent\n",
    }
    assert not (pr.ws / "base.index").exists()
    assert "are their base versions" in pr.verify(["AGENTS.md", "src/pkg/AGENTS.md", "src/new/CLAUDE.md"])


def test_reset_is_case_insensitive(pr: PR) -> None:
    pr.make({"README.md": "r\n"}, {"agents.md": "PR\n"})
    pr.reset(["AGENTS.md"])
    assert not (pr.repo / "agents.md").exists()
    assert pr.kept() == {"agents.md.txt": "PR\n"}


def test_reset_needs_patterns(pr: PR) -> None:
    pr.make(BASE, {"AGENTS.md": "PR\n"})
    with pytest.raises(Fatal, match=r"^No reset patterns\.$"):
        pr.reset(["", ""])


def test_reset_restores_a_base_file_under_a_pr_symlink(pr: PR) -> None:
    # The PR replaces the directory 'docs' by a symlink to its own rules.
    pr.make(BASE, {"docs/RULES.md": None, "evil/RULES.md": "PR rules\n", "docs": "-> evil"})
    pr.reset(["docs/RULES.md"])
    assert not (pr.repo / "docs").is_symlink()
    assert (pr.repo / "docs/RULES.md").read_text() == "base doc rules\n"
    assert "1 rule file(s)" in pr.verify(["docs/RULES.md"])


# --- The post-condition (S4 regressions: the reset bypassed or subverted) -----------


def test_post_condition_file_left_at_the_pr_version(pr: PR) -> None:
    pr.make(BASE, {"src/pkg/AGENTS.md": "PR nested\n"})
    with pytest.raises(Fatal, match=r"^Followed file 'src/pkg/AGENTS\.md' is not its base version in the checkout"):
        pr.verify([], ["src/pkg/AGENTS.md"])
    pr.reset(["AGENTS.md"])
    pr.verify([], ["src/pkg/AGENTS.md"])


def test_post_condition_pattern_that_misses_the_file(pr: PR) -> None:
    # The S4 scenario: a pattern that does not match at that depth leaves
    # the PR's added file in place.
    pr.make(BASE, {"src/gt4py/next/AGENTS.md": "PR\n"})
    pr.reset(["CLAUDE.md", "gt4py/*.md"])
    with pytest.raises(Fatal, match="is not in the base commit but is in the checkout: the reset missed it"):
        pr.verify(["CODING_GUIDELINES.md"], ["src/gt4py/next/AGENTS.md"])


def test_post_condition_symlinked_parent(pr: PR) -> None:
    pr.make(BASE, {"evil/RULES.md": "base doc rules\n"})
    # Same content, reached through a symlinked parent the reset did not write.
    (pr.repo / "docs/RULES.md").unlink()
    (pr.repo / "docs").rmdir()
    (pr.repo / "docs").symlink_to("evil")
    with pytest.raises(Fatal, match=r"^Followed file 'docs/RULES\.md': 'docs' is a symlink in the checkout\.$"):
        pr.verify(["docs/RULES.md"])


def test_post_condition_deepest_symlink_first(pr: PR) -> None:
    pr.make({"a/b/c/R.md": "x\n"}, {"t/c/R.md": "x\n", "t2/b/c/R.md": "x\n"})
    for link, target in (("a/b", "../t"), ("a", "t2")):
        path = pr.repo / link
        os.rename(path, pr.repo / f"{link}.old")
        path.symlink_to(target)
    with pytest.raises(Fatal, match="'a' is a symlink"):
        pr.verify(["a/b/c/R.md"])


def test_post_condition_rule_file_added_by_the_pr(pr: PR) -> None:
    pr.make(BASE, {"CODING_GUIDELINES.md": "PR guidelines\n"})
    with pytest.raises(
        Fatal, match=re.escape("'CODING_GUIDELINES.md' is not in the base commit but is in the checkout")
    ):
        pr.verify(["CODING_GUIDELINES.md"])
    pr.reset(["CODING_GUIDELINES.md"])
    assert pr.verify(["CODING_GUIDELINES.md"]).startswith("run-review: 1 rule file(s) and 0 file(s)")
    # A dangling symlink counts as present.
    (pr.repo / "CODING_GUIDELINES.md").symlink_to("nowhere")
    with pytest.raises(Fatal, match="is not in the base commit but is in the checkout"):
        pr.verify(["CODING_GUIDELINES.md"])


def test_post_condition_rule_file_deleted_by_the_pr(pr: PR) -> None:
    pr.make(BASE, {"docs/RULES.md": None})
    with pytest.raises(Fatal, match=r"'docs/RULES\.md' is not its base version in the checkout: the reset missed it"):
        pr.verify(["docs/RULES.md"])
    pr.reset(["docs/RULES.md"])
    pr.verify(["docs/RULES.md"])


def test_post_condition_symlink_in_base(pr: PR) -> None:
    pr.make({"AGENTS.md": "-> docs/agents.md", "docs/agents.md": "x\n"}, {"AGENTS.md": "PR\n"})
    with pytest.raises(Fatal, match=re.escape("'AGENTS.md' is not its base version")):
        pr.verify(["AGENTS.md"])
    pr.reset(["AGENTS.md"])
    assert (pr.repo / "AGENTS.md").is_symlink()
    pr.verify(["AGENTS.md"])
    (pr.repo / "AGENTS.md").unlink()
    (pr.repo / "AGENTS.md").symlink_to("docs/other.md")
    with pytest.raises(Fatal, match=re.escape("'AGENTS.md' is not its base version")):
        pr.verify(["AGENTS.md"])


def test_post_condition_executable_bit_and_directories(pr: PR) -> None:
    pr.make(BASE, {})
    with pytest.raises(Fatal, match=r"^Followed file 'docs' is not a file in the base commit \(mode 040000\)\.$"):
        pr.verify(["docs"])
    # Absent in both.
    assert "2 rule file(s)" in pr.verify(["MISSING.md", "x/y.md"])
    with pytest.raises(Fatal, match=r"^Invalid followed file 'docs/\*\.md': not a plain path\.$"):
        pr.verify(["docs/*.md"])


def test_post_condition_unusual_path(pr: PR) -> None:
    # 'ls-tree' quotes such names without '-z'; the bash version compared the quoted form.
    name = 'r\u00e9gles "x".md'
    pr.make({name: "base\n"}, {name: "PR\n"})
    with pytest.raises(Fatal, match="is not its base version"):
        pr.verify([name])
    pr.reset([name])
    pr.verify([name])
