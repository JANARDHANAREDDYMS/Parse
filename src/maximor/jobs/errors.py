"""Provide safe typed worker failures without exposing SQL, paths, or document data."""


class JobExecutionError(Exception):
    """Represent a known worker failure with persistable safe client details."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class UnsupportedJobTypeError(JobExecutionError):
    """Indicate that no runnable handler is registered for a claimed job type."""

    def __init__(self, job_type: str) -> None:
        super().__init__(
            "unsupported_job_type", f"Job type {job_type!r} is not supported."
        )


class DocumentNotFoundError(JobExecutionError):
    """Indicate that a claimed job has no matching tenant-scoped document."""

    def __init__(self) -> None:
        super().__init__("document_not_found", "The document record is unavailable.")


class StoredObjectMissingError(JobExecutionError):
    """Indicate that the document's object cannot be found in configured storage."""

    def __init__(self) -> None:
        super().__init__(
            "stored_object_missing", "The stored document is unavailable."
        )


class ChecksumMismatchError(JobExecutionError):
    """Indicate that stored bytes differ from the authoritative document checksum."""

    def __init__(self) -> None:
        super().__init__(
            "checksum_mismatch", "The stored document checksum does not match."
        )


class SizeMismatchError(JobExecutionError):
    """Indicate that stored bytes differ from the authoritative document size."""

    def __init__(self) -> None:
        super().__init__("size_mismatch", "The stored document size does not match.")


class InvalidJobContextError(JobExecutionError):
    """Indicate that a claimed row cannot form a safe typed handler context."""

    def __init__(self) -> None:
        super().__init__("invalid_job_context", "The processing job context is invalid.")
