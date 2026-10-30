"""Check filesystem attachment security at the local boundary."""

import os

import pytest
from fastmcp.exceptions import ToolError

from jira_mcp.attachment_files import AttachmentStorage


def test_upload_snapshot_and_limit(tmp_path):
    """Snapshot exact-limit files and reject oversized files."""
    storage = AttachmentStorage((str(tmp_path),), 3)
    path = tmp_path / "file"
    path.write_bytes(b"abc")
    name, snapshot, size = storage.open_upload(str(path))
    path.write_bytes(b"abcd")
    with snapshot:
        assert (name, size, snapshot.read()) == ("file", 3, b"abc")
    with pytest.raises(ToolError):
        storage.open_upload(str(path))


def test_unsafe_paths_and_fifo(tmp_path):
    """Reject traversal, prefix collisions, symlinks, and nonregular files."""
    storage = AttachmentStorage((str(tmp_path),), 3)
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "file").write_bytes(b"a")
    (tmp_path / "link").symlink_to(tmp_path / "dir")
    (tmp_path / "leaf").symlink_to(tmp_path / "dir" / "file")
    os.mkfifo(tmp_path / "fifo")
    for path in (tmp_path / "link" / "file", tmp_path / "leaf", tmp_path / "fifo", tmp_path / ".." / "file"):
        with pytest.raises(ToolError):
            storage.open_upload(str(path))
    with pytest.raises(ToolError), storage.open_directory(str(tmp_path) + "-other"):
        pass


def test_publication_race_and_cleanup(tmp_path, monkeypatch):
    """A no-clobber race preserves the winner and removes the temporary file."""
    storage = AttachmentStorage((str(tmp_path),), 3)
    original_link = os.link

    def race(src, dst, **kwargs):
        """Create a competing destination immediately before publication."""
        (tmp_path / dst).write_bytes(b"old")
        return original_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", race)
    with storage.open_directory(str(tmp_path)) as directory, pytest.raises(ToolError):
        directory.write("file", b"new", overwrite=False)
    assert (tmp_path / "file").read_bytes() == b"old"
    assert [path.name for path in tmp_path.iterdir()] == ["file"]


def test_overwrite_rejects_symlink(tmp_path):
    """Never follow an existing destination symlink when overwriting."""
    storage = AttachmentStorage((str(tmp_path),), 3)
    target = tmp_path / "target"
    target.write_bytes(b"old")
    (tmp_path / "link").symlink_to(target)
    with storage.open_directory(str(tmp_path)) as directory, pytest.raises(ToolError):
        directory.write("link", b"new", overwrite=True)
    assert target.read_bytes() == b"old"


def test_missing_root_is_not_created(tmp_path):
    """Create download subdirectories only inside an already existing trusted root."""
    # Arrange
    root = tmp_path / "missing"
    storage = AttachmentStorage((str(root),), 3)

    # Act and assert
    with pytest.raises(ToolError), storage.open_directory(str(root / "downloads")):
        pass
    assert not root.exists()
