"""agent_review.files: no symlinks followed, no blocking, bounded reads."""

import errno
import os
import pathlib

import pytest

from agent_review import files


def test_reads_a_regular_file(tmp_path: pathlib.Path) -> None:
    (tmp_path / "f").write_bytes(b"data")
    assert files.read_bytes(str(tmp_path / "f"), 4) == b"data"


def test_refuses_a_symlink(tmp_path: pathlib.Path) -> None:
    (tmp_path / "real").write_bytes(b"secret")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(OSError) as e:
        files.read_bytes(str(tmp_path / "link"), 100)
    assert e.value.errno == errno.ELOOP


def test_refuses_a_fifo_without_blocking(tmp_path: pathlib.Path) -> None:
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(files.UnsafeFileError, match="not a regular file"):
        files.read_bytes(str(tmp_path / "fifo"), 100)


def test_refuses_a_directory(tmp_path: pathlib.Path) -> None:
    with pytest.raises(files.UnsafeFileError, match="not a regular file"):
        files.read_bytes(str(tmp_path), 100)


def test_refuses_a_device() -> None:
    with pytest.raises(files.UnsafeFileError, match="not a regular file"):
        files.read_bytes("/dev/zero", 100)


def test_size_cap(tmp_path: pathlib.Path) -> None:
    (tmp_path / "f").write_bytes(b"x" * 11)
    assert files.read_bytes(str(tmp_path / "f"), 11) == b"x" * 11
    with pytest.raises(files.UnsafeFileError, match="11 bytes, more than 10"):
        files.read_bytes(str(tmp_path / "f"), 10)


def test_hard_links(tmp_path: pathlib.Path) -> None:
    (tmp_path / "f").write_bytes(b"x")
    os.link(tmp_path / "f", tmp_path / "g")
    assert files.read_bytes(str(tmp_path / "f"), 1) == b"x"
    with pytest.raises(files.UnsafeFileError, match="2 hard links"):
        files.read_bytes(str(tmp_path / "f"), 1, single_link=True)


def test_dir_fd(tmp_path: pathlib.Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_bytes(b"in d")
    (tmp_path / "link").symlink_to(tmp_path / "d")
    with pytest.raises(OSError):
        files.open_directory(str(tmp_path / "link"))
    fd = files.open_directory(str(tmp_path / "d"))
    try:
        assert files.read_bytes("f", 10, dir_fd=fd) == b"in d"
    finally:
        os.close(fd)


def test_read_text_is_strict_utf8(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ok").write_bytes("é\n".encode())
    assert files.read_text(str(tmp_path / "ok"), 10) == "é\n"
    (tmp_path / "bad").write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        files.read_text(str(tmp_path / "bad"), 10)


def test_write_new(tmp_path: pathlib.Path) -> None:
    files.write_new(str(tmp_path / "new"), b"x", mode=0o600)
    assert (tmp_path / "new").read_bytes() == b"x"
    assert (tmp_path / "new").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        files.write_new(str(tmp_path / "new"), b"y")
    (tmp_path / "dangling").symlink_to(tmp_path / "target")
    with pytest.raises(FileExistsError):
        files.write_new(str(tmp_path / "dangling"), b"y")
    assert not (tmp_path / "target").exists()


def test_append_refuses_a_symlink(tmp_path: pathlib.Path) -> None:
    (tmp_path / "real").write_bytes(b"")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(OSError):
        files.append_bytes(str(tmp_path / "link"), b"x")
    files.append_bytes(str(tmp_path / "real"), b"x")
    files.append_bytes(str(tmp_path / "real"), b"y")
    assert (tmp_path / "real").read_bytes() == b"xy"


def _reason(path: str, max_bytes: int, **kwargs: bool) -> str:
    with pytest.raises(files.UnsafeFileError) as e:
        files.read_bytes(path, max_bytes, **kwargs)
    assert isinstance(e.value.st, os.stat_result)
    return e.value.reason


def test_reasons(tmp_path: pathlib.Path) -> None:
    os.mkfifo(tmp_path / "fifo")
    (tmp_path / "f").write_bytes(b"x" * 11)
    os.link(tmp_path / "f", tmp_path / "g")
    assert _reason(str(tmp_path / "fifo"), 100) == "type"
    assert _reason(str(tmp_path), 100) == "type"
    assert _reason(str(tmp_path / "f"), 100, single_link=True) == "links"
    assert _reason(str(tmp_path / "f"), 10) == "size"


def test_reason_grew(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file that grows past the cap after the fstat check is refused."""
    (tmp_path / "f").write_bytes(b"x" * 10)
    real_read = os.read

    def growing_read(fd: int, n: int) -> bytes:
        with open(tmp_path / "f", "ab") as f:
            f.write(b"y" * 10)
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", growing_read)
    with pytest.raises(files.UnsafeFileError, match="more than 10 bytes") as e:
        files.read_bytes(str(tmp_path / "f"), 10)
    assert e.value.reason == "grew"
    assert e.value.st is not None
    assert e.value.st.st_size == 10


def test_unsafe_file_error_old_form() -> None:
    """Raising it with a message only still works (callers written before 'reason')."""
    e = files.UnsafeFileError("x: refused")
    assert (str(e), e.reason, e.st) == ("x: refused", "other", None)
    assert isinstance(e, OSError)
