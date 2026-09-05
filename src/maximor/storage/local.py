import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from uuid import uuid4

from maximor.storage.base import ObjectMetadata, StorageError


class LocalObjectStorage:
    """Filesystem-backed object storage addressed only by safe relative keys."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path_for(self, key: str) -> Path:
        logical_path = PurePosixPath(key)
        if (
            not key
            or logical_path.is_absolute()
            or ".." in logical_path.parts
            or "." in logical_path.parts
            or "\\" in key
        ):
            raise StorageError("invalid_storage_key", "The storage key is invalid.")
        candidate = self._root.joinpath(*logical_path.parts).resolve()
        if candidate == self._root or self._root not in candidate.parents:
            raise StorageError("invalid_storage_key", "The storage key is invalid.")
        return candidate

    async def put(
        self, key: str, chunks: AsyncIterator[bytes], *, maximum_bytes: int
    ) -> ObjectMetadata:
        destination = self._path_for(key)
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            handle = await asyncio.to_thread(temporary.open, "xb")
            try:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > maximum_bytes:
                        raise StorageError("upload_too_large", "The PDF exceeds the upload limit.")
                    digest.update(chunk)
                    await asyncio.to_thread(handle.write, chunk)
                await asyncio.to_thread(handle.flush)
                await asyncio.to_thread(os.fsync, handle.fileno())
            finally:
                await asyncio.to_thread(handle.close)
            if size == 0:
                raise StorageError("empty_upload", "The uploaded PDF is empty.")
            await asyncio.to_thread(temporary.replace, destination)
        except BaseException:
            await asyncio.to_thread(temporary.unlink, missing_ok=True)
            raise
        return ObjectMetadata(sha256_checksum=digest.hexdigest(), size=size)

    async def inspect(self, key: str) -> ObjectMetadata:
        path = self._path_for(key)
        try:
            return await asyncio.to_thread(self._inspect_sync, path)
        except FileNotFoundError:
            raise StorageError(
                "stored_object_missing", "The stored document is unavailable."
            ) from None

    async def read(self, key: str, *, maximum_bytes: int) -> bytes:
        """Read one safe-key object after enforcing its bounded byte size."""
        if maximum_bytes < 1:
            raise StorageError("invalid_read_limit", "The object read limit is invalid.")
        path = self._path_for(key)
        try:
            return await asyncio.to_thread(self._read_sync, path, maximum_bytes)
        except FileNotFoundError:
            raise StorageError("stored_object_missing", "The stored document is unavailable.") from None
        except OSError:
            raise StorageError("stored_object_unreadable", "The stored document is unavailable.") from None

    @staticmethod
    def _read_sync(path: Path, maximum_bytes: int) -> bytes:
        if path.stat().st_size > maximum_bytes:
            raise StorageError("stored_object_too_large", "The stored document exceeds the read limit.")
        with path.open("rb") as handle:
            data = handle.read(maximum_bytes + 1)
        if len(data) > maximum_bytes:
            raise StorageError("stored_object_too_large", "The stored document exceeds the read limit.")
        return data

    @staticmethod
    def _inspect_sync(path: Path) -> ObjectMetadata:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return ObjectMetadata(digest.hexdigest(), size)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path_for(key).unlink, missing_ok=True)
