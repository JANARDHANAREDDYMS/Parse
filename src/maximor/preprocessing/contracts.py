"""Define trusted request/source inputs for future materialization.

The service will turn a validated storage key into an internal PDF path. These
types output safe component inputs and do not read, parse, log, or serialize PDFs.
"""

import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


def validate_storage_key(storage_key: str) -> str:
    """Return a relative path-safe object key or reject it without exposing local paths."""
    logical_key = PurePosixPath(storage_key)
    if (not storage_key or logical_key.is_absolute() or "." in logical_key.parts
            or ".." in logical_key.parts or "\\" in storage_key):
        raise ValueError("storage_key must be a safe relative object key")
    return storage_key


@dataclass(frozen=True)
class DocumentPreprocessingRequest:
    """Identify one tenant document and its validated relative object-storage key."""
    organization_id: uuid.UUID
    document_id: uuid.UUID
    processing_job_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    storage_key: str

    def __post_init__(self) -> None:
        """Reject malformed identifiers and unsafe storage references."""
        identifiers = (self.organization_id, self.document_id, self.processing_job_id, self.preprocessing_run_id)
        if not all(isinstance(identifier, uuid.UUID) for identifier in identifiers):
            raise ValueError("preprocessing request identifiers must be UUID values")
        validate_storage_key(self.storage_key)


@dataclass(frozen=True, repr=False)
class TrustedPdfSource:
    """Hold an internal temporary PDF path created only from a validated object key."""
    local_path: Path
    storage_key: str

    def __post_init__(self) -> None:
        """Require an internal absolute path and a safe external storage reference."""
        if not self.local_path.is_absolute():
            raise ValueError("trusted PDF source requires an internal absolute path")
        validate_storage_key(self.storage_key)

    def __repr__(self) -> str:
        """Hide the sensitive temporary path from logs and diagnostics."""
        return f"TrustedPdfSource(storage_key={self.storage_key!r}, local_path=<hidden>)"
