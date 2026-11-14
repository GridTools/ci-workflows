"""Caller configuration, the instruction-file reset and its post-condition.

Part of ``run-review``, inside the sandbox. A pull request must not rewrite
the rules it is reviewed by, so before the harness runs every file in the
PR tree that matches a reset pattern is removed, and the ones that exist at
the base commit are restored (the PR's own versions stay as data).

**One glob engine.** Whether a pattern matches a path is always decided by
git, through the pathspec ``:(glob,icase)**/<pattern>`` (``**`` appended
for a directory pattern ending in ``/``): in the reset, and in the
pre-fetch check that a reset pattern covers every file the prompt addendum
names. Nothing here matches globs in Python. The bash version checked
coverage with bash's own matching, where ``*`` crosses ``/``, so a pattern
could look like it covered a file that the reset then missed (finding S4).

**Scratch index.** ``git ls-files`` matches pathspecs against an index, and
the coverage check runs before anything is fetched. ``Coverage`` therefore
creates a scratch bare repository in a temporary directory in the workspace
(the only place the sandbox may write) and, for each named path, an index
(``GIT_INDEX_FILE``) holding only that path, pointing at the empty blob,
which need not exist; one index per path, so that two named paths cannot
conflict as file and directory. The directory is removed afterwards.

**Post-condition (S4).** Whatever the matching does, after the reset every
file the prompt tells the agent to follow (each rule file, and each file
the addendum names, as a path from the repository root) must be its base
version, or absent in both the base and the checkout, and no leading
directory may be a symlink (``verify_followed_files``).
"""

import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from agent_review import files, runner
from agent_review.review_errors import Fatal
from agent_review.sandbox_git import Git

# Caps on the trusted configuration files (the bash version read them whole).
LIST_MAX_BYTES = 1 << 20
ADDENDUM_MAX_BYTES = 1 << 20
# bash's [[:space:]] in the C locale.
ASCII_SPACE = b" \t\n\v\f\r"
# grep -oE '`[^`[:space:]]+\.(md|mdc|txt|toml|ya?ml|json|jsonc)`', on bytes.
NAMED_FILE = re.compile(rb"`[^`\s]+\.(?:md|mdc|txt|toml|ya?ml|json|jsonc)`", re.ASCII)
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
CONTEXT_NAME = ".agent-review-context"


def _decode(data: bytes, where: str) -> str:
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as e:
        raise Fatal(f"'{where}' is not valid UTF-8.") from e


def read_config_file(path: str, max_bytes: int) -> bytes:
    try:
        data = files.read_bytes(path, max_bytes)
    except OSError as e:
        raise Fatal(f"Cannot read '{path}': {e}") from e
    if b"\0" in data:
        raise Fatal(f"'{path}' contains a NUL byte.")
    return data


def read_list(path: str) -> list[str]:
    """Non-comment, trimmed lines of ``path``; none if it is not a file."""
    if not os.path.isfile(path):
        return []
    entries = []
    for raw in read_config_file(path, LIST_MAX_BYTES).split(b"\n"):
        line = raw.strip(ASCII_SPACE)
        if line and not line.startswith(b"#"):
            entries.append(_decode(line, path))
    return entries


def check_entry(entry: str) -> None:
    if entry.startswith(("/", ":")) or ".." in entry:
        raise Fatal(f"Invalid configured path or pattern: '{entry}'.")


def is_tree_path(path: str) -> bool:
    """bash: case "/${path}/" in */../* | */./* | *//*) (not a path git writes)."""
    wrapped = f"/{path}/"
    return not ("/../" in wrapped or "/./" in wrapped or "//" in wrapped)


def is_plain_path(path: str) -> bool:
    """A path from the repository root without glob characters."""
    return is_tree_path(path) and not any(c in path for c in "*?[")


def pathspecs(patterns: Iterable[str]) -> list[str]:
    """The git pathspec of each pattern; every pattern applies at any depth, case-insensitively."""
    return [f":(glob,icase)**/{p}**" if p.endswith("/") else f":(glob,icase)**/{p}" for p in patterns if p]


@dataclass(frozen=True)
class Config:
    """The caller configuration, from ``<root>/config`` and ``<root>/instruction-files.txt``."""

    rule_files: tuple[str, ...]
    reset_patterns: tuple[str, ...]
    addendum: bytes  # empty when not configured
    addendum_named: tuple[str, ...]  # sorted bytewise, unique


def load_config(root: str) -> Config:
    """Reads and checks the caller configuration (coverage is checked by ``check_coverage``)."""
    config_dir = os.path.join(root, "config")
    rule_files = read_list(os.path.join(config_dir, "rule-files.txt"))
    for entry in rule_files:
        check_entry(entry)
    # The central list (every harness's files), the caller's extra patterns,
    # and every rule file the prompt tells the agent to follow.
    patterns = [
        *read_list(os.path.join(root, "instruction-files.txt")),
        *read_list(os.path.join(config_dir, "extra-instruction-files.txt")),
        *rule_files,
    ]
    for entry in patterns:
        check_entry(entry)
    addendum = b""
    named: list[str] = []
    addendum_path = os.path.join(config_dir, "prompt-addendum.md")
    if os.path.isfile(addendum_path) and os.path.getsize(addendum_path) > 0:
        addendum = read_config_file(addendum_path, ADDENDUM_MAX_BYTES)
        # The addendum is substituted into the prompt before the untrusted PR
        # title and body; a placeholder in it would be expanded with them,
        # outside their delimiters.
        if b"{{" in addendum:
            raise Fatal("The prompt addendum contains '{{'; placeholders are not allowed there.")
        found = sorted({m.group()[1:-1] for m in NAMED_FILE.finditer(addendum)})
        named = [_decode(n, addendum_path) for n in found]
    return Config(tuple(rule_files), tuple(patterns), addendum, tuple(named))


class Coverage:
    """Whether some reset pattern matches a path, decided by git (see the module docstring)."""

    def __init__(self, parent_dir: str, env: Mapping[str, str], patterns: Sequence[str]) -> None:
        self.specs = pathspecs(patterns)
        self.dir = tempfile.mkdtemp(prefix=".agent-review-coverage.", dir=parent_dir)
        self.index = os.path.join(self.dir, "index")
        repo = os.path.join(self.dir, "repo")
        os.mkdir(repo)
        self.git = Git(repo, env)
        self.git.run("init", "-q", "--bare", "--template=")

    def __enter__(self) -> "Coverage":
        return self

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def covers(self, path: str) -> bool:
        if not self.specs:
            return False
        if os.path.lexists(self.index):
            os.unlink(self.index)
        index = {"GIT_INDEX_FILE": self.index}
        entry = f"100644 {EMPTY_BLOB}\t{path}\0".encode()
        self.git.run("update-index", "-z", "--add", "--index-info", env=index, stdin=entry)
        listed = self.git.output("ls-files", "-z", "--", *self.specs, env=index)
        return path.encode() in listed.split(b"\0")


def check_coverage(config: Config, scratch_parent: str, env: Mapping[str, str]) -> None:
    """Fails closed unless each file the addendum names is a plain path that a reset pattern covers.

    Otherwise a PR could rewrite such a file and have the agent follow its
    version. The post-condition after the reset checks the result again.
    """
    if not config.addendum_named:
        return
    with Coverage(scratch_parent, env, config.reset_patterns) as coverage:
        for named in config.addendum_named:
            # A path from the repository root, which the post-condition can check.
            if not is_plain_path(named):
                raise Fatal(f"The prompt addendum names '{named}', which is not a plain repository path.")
            try:
                covered = coverage.covers(named)
            except runner.CommandError as e:
                raise Fatal(f"Could not check whether a reset pattern covers '{named}': {e}.") from e
            if not covered:
                raise Fatal(
                    f"The prompt addendum names '{named}', which no reset pattern covers; add it to rule_files."
                )


def _paths(listing: bytes) -> list[str]:
    """NUL-separated git paths; non-UTF-8 bytes survive as surrogate escapes."""
    return [os.fsdecode(p) for p in listing.split(b"\0") if p]


def _write_from_git(git: Git, dest: str, *args: str) -> None:
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        git.run(*args, stdout_fd=fd)
    finally:
        os.close(fd)


def reset_instruction_files(
    git: Git, ws: str, *, head_sha: str, base_sha: str, patterns: Sequence[str], context_dir: str
) -> None:
    """Resets every file matching a pattern to its base version; keeps the PR's changed versions as data.

    Runs after the checkout of the head, so the index holds the head tree
    (without the context directory); the base tree goes into a scratch
    index. ``ls-files``, not ``ls-tree``, which rejects the ``:(glob)``
    magic.
    """
    specs = pathspecs(patterns)
    if not specs:
        raise Fatal("No reset patterns.")
    pr_paths = _paths(git.output("ls-files", "-z", "--", *specs))
    base_index = os.path.join(ws, "base.index")
    index = {"GIT_INDEX_FILE": base_index}
    try:
        git.run("read-tree", base_sha, env=index)
        base_paths = _paths(git.output("ls-files", "-z", "--", *specs, env=index))
    finally:
        if os.path.lexists(base_index):
            os.unlink(base_index)

    for path in pr_paths:
        # Git refuses such paths in trees, but they become filesystem paths here.
        if not is_tree_path(path):
            raise Fatal(f"Unexpected path in PR tree: '{path}'.")
        # Only what the PR adds or changes is kept, so the review can comment on it.
        head_id = git.line("rev-parse", f"{head_sha}:{path}")
        if head_id is None:
            raise Fatal(f"Cannot resolve '{path}' in the head commit.")
        base_id = git.line("rev-parse", "-q", "--verify", f"{base_sha}:{path}") or ""
        if head_id == base_id:
            continue
        dest = os.path.join(context_dir, "pr-instruction-files", f"{path}.txt")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _write_from_git(git, dest, "cat-file", "-p", f"{head_sha}:{path}")
    if pr_paths:
        # Git removes paths without following symlinks in leading components.
        git.run("--literal-pathspecs", "rm", "-r", "-q", "-f", "--ignore-unmatch", "--", *pr_paths)
    if base_paths:
        git.run("--literal-pathspecs", "checkout", "-q", base_sha, "--", *base_paths)


def _parse_ls_tree(listing: bytes) -> tuple[str, str, str, str] | None:
    """(mode, type, id, path) of the first 'ls-tree -z' record, or None if there is none."""
    record = listing.split(b"\0", 1)[0]
    if not record:
        return None
    meta, _, name = record.partition(b"\t")
    fields = meta.decode("ascii", errors="replace").split(" ")
    if len(fields) != 3:
        return ("", "", "", os.fsdecode(record))
    return (fields[0], fields[1], fields[2], os.fsdecode(name))


def verify_followed_files(
    git: Git, repo_dir: str, base_sha: str, rule_files: Sequence[str], named: Sequence[str]
) -> str:
    """Checks the post-condition of the reset; returns the summary line, or raises Fatal.

    Checked on the result rather than on the patterns, so a pattern that
    does not match what it seems to cannot let the PR's version through.
    No leading directory may be a symlink, so the path in the checkout is
    the path git wrote.
    """
    missed = "is not its base version in the checkout: the reset missed it."
    for path in (*rule_files, *named):
        if not path:
            continue
        if not is_plain_path(path):
            raise Fatal(f"Invalid followed file '{path}': not a plain path.")
        parent = path
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if os.path.islink(os.path.join(repo_dir, parent)):
                raise Fatal(f"Followed file '{path}': '{parent}' is a symlink in the checkout.")
        checkout_path = os.path.join(repo_dir, path)
        # '-z': no quoting of unusual paths, which the bash version compared quoted.
        entry = _parse_ls_tree(git.output("ls-tree", "-z", "--full-tree", base_sha, "--", path))
        if entry is None:
            if os.path.lexists(checkout_path):
                raise Fatal(
                    f"Followed file '{path}' is not in the base commit but is in the checkout: the reset missed it."
                )
            continue
        mode, kind, object_id, name = entry
        if name != path:
            raise Fatal(f"Followed file '{path}': unexpected base entry '{mode} {kind} {object_id}\t{name}'.")
        if (mode, kind) in (("100644", "blob"), ("100755", "blob")):
            same = (
                os.path.isfile(checkout_path)
                and not os.path.islink(checkout_path)
                and git.line("hash-object", "--no-filters", "--", path) == object_id
            )
            if not same:
                raise Fatal(f"Followed file '{path}' {missed}")
        elif (mode, kind) == ("120000", "blob"):
            same = os.path.islink(checkout_path) and os.fsencode(os.readlink(checkout_path)) == git.output(
                "cat-file", "blob", object_id
            )
            if not same:
                raise Fatal(f"Followed file '{path}' {missed}")
        else:
            raise Fatal(f"Followed file '{path}' is not a file in the base commit (mode {mode}).")
    return (
        f"run-review: {len(rule_files)} rule file(s) and {len(named)} file(s) named by the addendum"
        " are their base versions."
    )
