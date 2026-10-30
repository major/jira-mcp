"""Safe local attachment staging under trusted POSIX roots.

Roots must be trusted against hostile directory moves and hard links by local users.
"""

# ruff: noqa: TRY003, TRY301, SIM115, PLR2004, PTH118

from __future__ import annotations

import os
import secrets
import stat
import tempfile
import unicodedata
from contextlib import contextmanager, suppress
from typing import BinaryIO, cast
lazy from collections.abc import Generator

from fastmcp.exceptions import ToolError


def validate_filename(name: str) -> None:
    """Reject names that cannot safely identify one attachment file."""
    if (
        not name
        or len(name) > 255
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or any(unicodedata.category(char) == "Cc" for char in name)
    ):
        raise ToolError("Invalid attachment filename.")


class AttachmentStorage:
    """Access bounded attachment files below configured staging roots."""

    def __init__(self, roots: tuple[str, ...], max_bytes: int) -> None:
        """Store the permitted roots and per-file size limit."""
        self.roots = tuple(os.path.normpath(root) for root in roots)
        self.max_bytes = max_bytes

    def _parts(self, path: str) -> tuple[list[str], int]:
        """Check lexical containment before accessing or creating any path."""
        if not path.startswith("/") or "\x00" in path or ".." in path.split("/"):
            raise ToolError("Invalid attachment path.")
        normalized = os.path.normpath(path)
        parts = [part for part in normalized.split("/") if part]
        for root in self.roots:
            root_parts = [part for part in root.split("/") if part]
            if parts[: len(root_parts)] == root_parts:
                return parts, len(root_parts)
        raise ToolError("Attachment path is outside configured directories.")

    @contextmanager
    def _directory(self, parts: list[str], root_length: int, *, create: bool) -> Generator[int]:
        """Walk directories without following symlinks, creating only below the root."""
        fd = -1
        try:
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for index, part in enumerate(parts):
                try:
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not create or index < root_length:
                        raise
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o700, dir_fd=fd)
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd
        except OSError:
            raise ToolError("Cannot access attachment directory safely.") from None
        finally:
            if fd != -1:
                os.close(fd)

    def open_upload(self, path: str) -> tuple[str, BinaryIO, int]:
        """Snapshot a regular file without following symlinks or exceeding the limit."""
        parts, root_length = self._parts(path)
        if len(parts) <= root_length:
            raise ToolError("Attachment upload must be a file below a configured directory.")
        filename = parts[-1]
        validate_filename(filename)
        snapshot = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        try:
            with self._directory(parts[:-1], root_length, create=False) as directory:
                fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ToolError("Attachment upload must be a regular file.")
                    size = 0
                    while chunk := source.read(min(65536, self.max_bytes - size + 1)):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise ToolError("Attachment exceeds the configured size limit.")
                        snapshot.write(chunk)
            snapshot.seek(0)
            return filename, cast("BinaryIO", snapshot), size
        except OSError:
            snapshot.close()
            raise ToolError("Cannot read attachment file safely.") from None
        except BaseException:
            snapshot.close()
            raise

    @contextmanager
    def open_directory(self, path: str) -> Generator[AttachmentDirectory]:
        """Open or create an allowed destination directory without following symlinks."""
        parts, root_length = self._parts(path)
        with self._directory(parts, root_length, create=True) as fd:
            yield AttachmentDirectory(fd, os.path.normpath(path), self.max_bytes)


class AttachmentDirectory:
    """Publish files relative to a held directory descriptor."""

    def __init__(self, fd: int, path: str, max_bytes: int) -> None:
        """Keep the directory descriptor, display path, and file size limit."""
        self._fd = fd
        self._path = path
        self._max_bytes = max_bytes

    def check_destination(self, filename: str, *, overwrite: bool) -> None:
        """Reject existing destinations unless they are regular files to overwrite."""
        validate_filename(filename)
        try:
            info = os.stat(filename, dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            raise ToolError("Cannot inspect attachment destination safely.") from None
        if not overwrite or not stat.S_ISREG(info.st_mode):
            raise ToolError("Attachment destination already exists or is not a regular file.")

    def write(self, filename: str, content: bytes, *, overwrite: bool) -> str:
        """Atomically publish bounded content without following destination symlinks."""
        self.check_destination(filename, overwrite=overwrite)
        if len(content) > self._max_bytes:
            raise ToolError("Attachment exceeds the configured size limit.")
        temporary = f".jira-attachment-{secrets.token_hex(16)}"
        created = False
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
            created = True
            with os.fdopen(fd, "wb") as target:
                target.write(content)
            self.check_destination(filename, overwrite=overwrite)
            if overwrite:
                os.replace(temporary, filename, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            else:
                os.link(temporary, filename, src_dir_fd=self._fd, dst_dir_fd=self._fd, follow_symlinks=False)
            return os.path.join(self._path, filename)
        except OSError:
            raise ToolError("Cannot write attachment destination safely.") from None
        finally:
            if created:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=self._fd)
