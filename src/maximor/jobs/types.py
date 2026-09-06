"""Define typed job identifiers received from PostgreSQL for worker dispatch."""

import uuid
from dataclasses import dataclass
from enum import StrEnum


class JobType(StrEnum):
    """Supported and reserved processing-job type values stored as strings."""

    PIPELINE_SMOKE_TEST = "pipeline_smoke_test"
    DOCUMENT_PREPROCESSING = "document_preprocessing"
    DOCUMENT_ANALYSIS = "document_analysis"
    SKU_MAPPING = "sku_mapping"


@dataclass(frozen=True)
class JobContext:
    """Carry immutable identifiers from one claimed job into one handler call."""

    organization_id: uuid.UUID
    document_id: uuid.UUID
    processing_job_id: uuid.UUID
    job_type: str
    attempt_number: int

    def __post_init__(self) -> None:
        """Reject malformed contexts before a handler can execute."""

        identifiers = (
            self.organization_id,
            self.document_id,
            self.processing_job_id,
        )
        if not all(isinstance(identifier, uuid.UUID) for identifier in identifiers):
            raise ValueError("Job context identifiers must be UUID values.")
        if not self.job_type or self.attempt_number < 1:
            raise ValueError("Job context type and attempt number are invalid.")
