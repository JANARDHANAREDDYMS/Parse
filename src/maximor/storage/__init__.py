"""Object-storage interfaces and local development implementation."""

from maximor.storage.base import ObjectMetadata, ObjectStorage, StorageError
from maximor.storage.local import LocalObjectStorage

__all__ = ["LocalObjectStorage", "ObjectMetadata", "ObjectStorage", "StorageError"]
