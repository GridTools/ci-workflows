"""Copies the review out of the sandbox workspace: ``bin/collect-result``.

Usage: ``collect-result <out-dir> <dest-dir>``

Runs on the TAINTED host, after the sandbox. Everything the sandbox could
write is attacker-controlled from here on: ``<out-dir>``, the workspace
around it, and the home and ``/tmp`` trees. So this module starts no
process and reads nothing from the environment; it only opens files, and
every open is relative to a directory descriptor, with ``O_NOFOLLOW``:

- ``<out-dir>`` must be an absolute path without ``.``, ``..``, empty
  components or a trailing slash (the bash version compared it with its
  ``realpath``). It is opened one component at a time from ``/``, each a
  directory and none a symlink, and is then used only through that
  descriptor: swapping it or a parent for a symlink afterwards changes
  nothing. The walk uses ``O_PATH`` descriptors, which need only search
  permission, as bash's path lookup did (a search-only ``0311`` workspace
  still works).
- Only ``review.md`` and ``meta.json`` are copied, each only if it is a
  regular file with a single link, at most ``MAX_BYTES``. The type is
  checked with ``lstat`` first, so a FIFO or a device found there is not
  opened. Whatever is opened (also something swapped in after that check)
  is opened non-blocking and checked again with ``fstat`` on the descriptor
  (``files.read_bytes``), which also caps the bytes read: a file that grows
  while it is read is refused.
- What this does not close: a process that is still running could
  hard-link a host file (of at most ``MAX_BYTES``) in as ``review.md`` and
  remove the extra link between the open and the ``fstat``, so that
  ``st_nlink`` reads 1. No link-count check can prevent that; the
  single-link check relies on no sandbox process being alive, which the
  workflow's ``Stop the sandbox`` step guarantees (``Collect the result``
  runs only if it succeeded). The path and type races are closed
  regardless.
- The content is copied as bytes; the ``publish`` job validates it again
  and never trusts this copy.
- ``<dest-dir>`` (under ``$RUNNER_TEMP``, outside the sandbox's reach) is
  created if needed and must not be a symlink itself. Each file is read
  completely before anything is written, then written to a new
  ``<name>.tmp`` (``O_EXCL``, no symlink followed) and renamed into place;
  the temporary file is removed if the rename fails.

Everything else the agent wrote is ignored. A missing or refused file is a
``::warning`` and exit 0; a wrong argument count, or a destination that
cannot be used, exits 1.
"""

import contextlib
import errno
import os
import stat
import sys

from agent_review import files, workflow

MAX_BYTES = 65536
EXPECTED = ("review.md", "meta.json")
TITLE = "Agent review result"
USAGE = "Usage: collect-result <out-dir> <dest-dir>\n"

# A directory used only as ``dir_fd`` and for path lookup: needs search
# permission only. With O_DIRECTORY | O_NOFOLLOW a symlink fails (ENOTDIR).
# Not files.open_directory: an O_PATH descriptor cannot list the directory.
_PATH_DIR_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

# Open errors that mean "not a regular file we may read": absent, a symlink
# (O_NOFOLLOW), a socket.
_NOT_REGULAR = frozenset({errno.ENOENT, errno.ELOOP, errno.ENXIO, errno.ENOTDIR})


def _display(path: str) -> str:
    """``path`` as printable UTF-8 (arguments may hold undecodable bytes)."""
    return os.fsencode(path).decode("utf-8", errors="backslashreplace")


def _warn(message: str) -> None:
    workflow.warning(message, title=TITLE)


def _say(line: str) -> None:
    sys.stdout.buffer.write((line + "\n").encode("utf-8", errors="strict"))
    sys.stdout.buffer.flush()


def open_path_directory(name: str, *, dir_fd: int | None = None) -> int:
    """An ``O_PATH`` descriptor of the directory ``name``, which must not be a symlink."""
    return os.open(name, _PATH_DIR_FLAGS, dir_fd=dir_fd)


def open_source(src: str) -> int | None:
    """An ``O_PATH`` descriptor of the directory ``src``, or None.

    None if ``src`` is not absolute and canonical, or if it or any parent
    is missing, not a directory, or a symlink.
    """
    if not src.startswith("/"):
        return None
    parts = [] if src == "/" else src[1:].split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    try:
        fd = open_path_directory("/")
    except OSError:
        return None
    try:
        for part in parts:
            child = open_path_directory(part, dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError:
        os.close(fd)
        return None
    return fd


def open_dest(dest: str) -> int:
    """Creates ``dest`` if needed; a descriptor of it (not through a symlink)."""
    os.makedirs(dest, exist_ok=True)
    return files.open_directory(dest)


def read_expected(name: str, src_fd: int) -> bytes | None:
    """The content of ``name`` in the directory ``src_fd``, or None after a warning."""
    try:
        st = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode):
            _warn(f"'{name}' is missing or not a regular file; skipped.")
            return None
        return files.read_bytes(name, MAX_BYTES, dir_fd=src_fd, single_link=True)
    except files.UnsafeFileError as e:
        if e.reason == "links" and e.st is not None:
            # A hard link could expose a host file the agent cannot read itself.
            _warn(f"'{name}' has {e.st.st_nlink} hard links; skipped.")
        elif e.reason == "size" and e.st is not None:
            _warn(f"'{name}' is {e.st.st_size} bytes, over the {MAX_BYTES}-byte limit; skipped.")
        elif e.reason == "grew":
            _warn(f"'{name}' grew over the {MAX_BYTES}-byte limit while reading; skipped.")
        else:
            _warn(f"'{name}' is missing or not a regular file; skipped.")
    except OSError as e:
        if e.errno in _NOT_REGULAR:
            _warn(f"'{name}' is missing or not a regular file; skipped.")
        else:
            _warn(f"'{name}' could not be read ({e.strerror}); skipped.")
    return None


def write_result(name: str, data: bytes, dest_fd: int) -> None:
    """Writes ``data`` to ``name`` in ``dest_fd``, through a new temporary file."""
    tmp = f"{name}.tmp"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp, dir_fd=dest_fd)
    files.write_new(tmp, data, dir_fd=dest_fd)
    try:
        os.rename(tmp, name, src_dir_fd=dest_fd, dst_dir_fd=dest_fd)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dest_fd)
        raise


def collect(src: str, dest: str) -> int:
    try:
        dest_fd = open_dest(dest)
    except OSError as e:
        sys.stderr.write(f"collect-result: cannot use '{_display(dest)}' as the destination: {e.strerror}\n")
        return 1
    try:
        src_fd = open_source(src)
        if src_fd is None:
            _warn(f"'{_display(src)}' is missing, not a directory, or reached through a symlink; nothing collected.")
            return 0
        try:
            for name in EXPECTED:
                data = read_expected(name, src_fd)
                if data is None:
                    continue
                try:
                    write_result(name, data, dest_fd)
                except OSError as e:
                    sys.stderr.write(f"collect-result: cannot write '{name}' to '{_display(dest)}': {e.strerror}\n")
                    return 1
                _say(f"Collected '{name}' ({len(data)} bytes).")
        finally:
            os.close(src_fd)
    finally:
        os.close(dest_fd)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        sys.stderr.write(USAGE)
        return 1
    return collect(args[0], args[1])
