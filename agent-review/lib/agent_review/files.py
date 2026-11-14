"""File access that does not follow symlinks, block on FIFOs or read unbounded.

Every open uses ``O_NOFOLLOW | O_NONBLOCK | O_NOCTTY | O_CLOEXEC``; the type
and size are then checked with ``fstat`` on the open descriptor (never on
the path, which could be swapped in between), and reads stop at a cap.
``O_NOFOLLOW`` covers the last path component only: where a parent
directory may be hostile, open it with ``open_directory`` and pass its
descriptor as ``dir_fd`` with a single-component name.

Contents are bytes; ``read_text`` decodes strict UTF-8, never in the
locale's encoding.
"""

import os
import stat

_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC


class UnsafeFileError(OSError):
    """A file is not a regular file, is too large, or has extra links.

    ``reason`` says which check failed, so that callers need not parse the
    message: ``"type"`` (not a regular file), ``"links"`` (more than one
    hard link), ``"size"`` (``fstat`` size over the cap) or ``"grew"`` (the
    read went past the cap). ``st`` is the ``fstat`` result of the open file.
    """

    def __init__(self, message: str, reason: str, st: os.stat_result) -> None:
        super().__init__(message)
        self.reason = reason
        self.st = st


def open_directory(path: str, *, dir_fd: int | None = None) -> int:
    """A descriptor of the directory ``path``, which must not be a symlink."""
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | _FLAGS, dir_fd=dir_fd)


def _check_regular(fd: int, path: str, max_bytes: int, single_link: bool) -> os.stat_result:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeFileError(f"{path}: not a regular file", "type", st)
    if single_link and st.st_nlink != 1:
        raise UnsafeFileError(f"{path}: has {st.st_nlink} hard links", "links", st)
    if st.st_size > max_bytes:
        raise UnsafeFileError(f"{path}: {st.st_size} bytes, more than {max_bytes}", "size", st)
    return st


def read_bytes(path: str, max_bytes: int, *, dir_fd: int | None = None, single_link: bool = False) -> bytes:
    """The contents of the regular file ``path``, at most ``max_bytes``.

    Fails on a symlink, a FIFO, a device or a directory, on a file larger
    than the cap (also one that grows while it is read), and with
    ``single_link`` on a file with more than one hard link.
    """
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    fd = os.open(path, os.O_RDONLY | _FLAGS, dir_fd=dir_fd)
    try:
        st = _check_regular(fd, path, max_bytes, single_link)
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 16))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise UnsafeFileError(f"{path}: more than {max_bytes} bytes", "grew", st)
    return data


def read_text(path: str, max_bytes: int, *, dir_fd: int | None = None, single_link: bool = False) -> str:
    """``read_bytes`` decoded as strict UTF-8 (UnicodeDecodeError otherwise)."""
    return read_bytes(path, max_bytes, dir_fd=dir_fd, single_link=single_link).decode("utf-8", errors="strict")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def append_bytes(path: str, data: bytes, *, dir_fd: int | None = None) -> None:
    """Appends to the regular file ``path``, creating it (mode 0644) if needed."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _FLAGS, 0o644, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeFileError(f"{path}: not a regular file", "type", st)
        _write_all(fd, data)
    finally:
        os.close(fd)


def write_new(path: str, data: bytes, *, mode: int = 0o644, dir_fd: int | None = None) -> None:
    """Creates ``path``, which must not exist yet (not even as a symlink)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FLAGS, mode, dir_fd=dir_fd)
    try:
        _write_all(fd, data)
    finally:
        os.close(fd)
