from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


class StorageError(Exception):
    """Safe object-storage failure with a client-independent error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ObjectMetadata:
    sha256_checksum: str
    size: int


class ObjectStorage(Protocol):
    async def put(
        self, key: str, chunks: AsyncIterator[bytes], *, maximum_bytes: int
    ) -> ObjectMetadata: ...

    async def inspect(self, key: str) -> ObjectMetadata: ...

    async def read(self, key: str, *, maximum_bytes: int) -> bytes:
        """Return one bounded object without revealing its storage path."""
        ...

    async def delete(self, key: str) -> None: ...
