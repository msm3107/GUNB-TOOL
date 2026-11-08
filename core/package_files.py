"""Bounded verification on a single securely opened private artifact handle."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

MAX_ARTIFACT_SIZE = 64 * 1024 * 1024
CHUNK_SIZE = 64 * 1024


class ArtifactUnavailable(OSError):
    """Safe public boundary; never expose a path or underlying OS message."""


@dataclass(frozen=True)
class Artifact:
    format: str
    relative_name: str
    size: int
    sha256: str


class VerifiedFile:
    def __init__(self, stream: BinaryIO, guards: ExitStack):
        self.stream, self._guards = stream, guards

    def close(self):
        self._guards.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def valid_name(name: str, fmt: str) -> bool:
    return (
        fmt in {"xlsx", "csv"}
        and isinstance(name, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\." + fmt, name) is not None
        and ".." not in name
    )


def _windows_open(path: Path, directory: bool, guards: ExitStack):
    # CreateFileW OPEN_REPARSE_POINT opens the link itself; inspect its handle.
    # Directory handles deny deletion/rename while descending and streaming.
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = (wintypes.HANDLE,), wintypes.BOOL
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    info.restype = wintypes.BOOL
    handle = create(
        str(path),
        0x80 if directory else 0x80000000,
        1,
        None,
        3,
        0x00200000 | (0x02000000 if directory else 0),
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ArtifactUnavailable()
    # Ownership is transferred to msvcrt only after attributes are checked.
    try:
        attributes = (wintypes.DWORD * 2)()
        if not info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            raise ArtifactUnavailable()
        if attributes[0] & 0x400 or bool(attributes[0] & 0x10) != directory:
            raise ArtifactUnavailable()
        if directory:
            guards.callback(close, handle)
            return None
        import msvcrt

        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    return fd


def _private_open(root: Path, name: str, fmt: str) -> VerifiedFile:
    if not valid_name(name, fmt) or not root.is_absolute() or ".." in root.parts:
        raise ArtifactUnavailable()
    guards = ExitStack()
    try:
        if os.name == "nt":
            current = Path(root.anchor)
            _windows_open(current, True, guards)
            for part in root.parts[1:]:
                current /= part
                _windows_open(current, True, guards)
            fd = _windows_open(current / name, False, guards)
        else:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent = os.open(root.anchor, flags)
            guards.callback(os.close, parent)
            for part in root.parts[1:]:
                parent = os.open(part, flags, dir_fd=parent)
                guards.callback(os.close, parent)
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
        try:
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        guards.callback(stream.close)
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ArtifactUnavailable()
        return VerifiedFile(stream, guards)
    except BaseException:
        guards.close()
        raise


def _inspect(opened: VerifiedFile, name: str, fmt: str) -> Artifact:
    stream = opened.stream
    before = os.fstat(stream.fileno())
    if not 0 < before.st_size <= MAX_ARTIFACT_SIZE:
        raise ArtifactUnavailable()
    deadline, size, digest = time.monotonic() + 5, 0, hashlib.sha256()
    while chunk := stream.read(CHUNK_SIZE):
        size += len(chunk)
        if size > MAX_ARTIFACT_SIZE or time.monotonic() > deadline:
            raise ArtifactUnavailable()
        digest.update(chunk)
    after = os.fstat(stream.fileno())
    if size != before.st_size or (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ArtifactUnavailable()
    stream.seek(0)
    return Artifact(fmt, name, size, digest.hexdigest())


def inspect_artifact(root: Path, name: str, fmt: str) -> Artifact:
    try:
        with _private_open(root, name, fmt) as opened:
            return _inspect(opened, name, fmt)
    except (OSError, ValueError):
        raise ArtifactUnavailable() from None


def open_verified(root: Path, artifact: Artifact) -> VerifiedFile:
    opened = None
    try:
        if type(artifact.size) is not int or not 0 < artifact.size <= MAX_ARTIFACT_SIZE:
            raise ArtifactUnavailable()
        opened = _private_open(root, artifact.relative_name, artifact.format)
        if _inspect(opened, artifact.relative_name, artifact.format) != artifact:
            raise ArtifactUnavailable()
        return opened
    except (OSError, ValueError):
        if opened is not None:
            opened.close()
        raise ArtifactUnavailable() from None
