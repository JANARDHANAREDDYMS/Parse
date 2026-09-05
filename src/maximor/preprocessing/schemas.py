"""Define structured outputs received from deterministic preprocessing components.

Pydantic validates component and final-document results. This module performs no
PDF extraction, persistence, semantic interpretation, or agent work.
"""

import math
import re
import uuid
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from maximor.preprocessing.contracts import validate_storage_key

PageNumber = Annotated[int, Field(ge=1)]
ReadingOrder = Annotated[int, Field(ge=0)]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


def _contains_sensitive_diagnostic(value: str) -> bool:
    return bool(
        "Traceback (most recent call last)" in value
        or "file://" in value.lower()
        or re.search(r"(?:^|\s)/(?:Users|home|tmp|private|var|etc)/", value)
        or re.search(r"(?:^|\s)[A-Za-z]:\\", value)
    )


class PreprocessingModel(BaseModel):
    """Make serialized preprocessing contracts immutable and reject undeclared fields."""
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtractionSource(StrEnum):
    """Distinguish embedded PDF text from OCR-derived text."""
    NATIVE = "native"
    OCR = "ocr"


class BlockType(StrEnum):
    """Describe physical page elements without assigning business meaning."""
    TEXT = "text"
    IMAGE_REGION = "image_region"
    DRAWING_REGION = "drawing_region"
    UNKNOWN = "unknown"


class OcrStatus(StrEnum):
    """Represent whether OCR was unnecessary, requested, completed, or failed."""
    NOT_REQUIRED = "not_required"
    REQUIRED = "required"
    COMPLETED = "completed"
    FAILED = "failed"


class WarningSeverity(StrEnum):
    """Classify safe deterministic preprocessing warnings by operational severity."""
    INFORMATION = "information"
    WARNING = "warning"
    ERROR = "error"


class OcrDecisionReason(StrEnum):
    """Provide stable machine-readable reasons for a future OCR decision."""
    EMPTY_NATIVE_TEXT = "empty_native_text"
    LOW_READABLE_CHARACTER_RATIO = "low_readable_character_ratio"
    SUSPICIOUS_REPLACEMENT_CHARACTERS = "suspicious_replacement_characters"
    SUFFICIENT_NATIVE_TEXT = "sufficient_native_text"


class BoundingBox(PreprocessingModel):
    """Locate content in normalized PDF points using a top-left coordinate origin."""
    x0: float
    y0: float
    x1: float
    y1: float

    @field_validator("x0", "y0", "x1", "y1")
    @classmethod
    def coordinates_are_finite(cls, value: float) -> float:
        """Reject NaN and infinite coordinate values."""
        if not math.isfinite(value):
            raise ValueError("bounding-box coordinates must be finite")
        return value

    @model_validator(mode="after")
    def coordinates_are_ordered(self) -> "BoundingBox":
        """Require lower coordinates not to exceed upper coordinates."""
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("bounding-box coordinates are out of order")
        return self


class PreprocessingWarning(PreprocessingModel):
    """Carry a stable safe warning without paths, stack traces, or document content."""
    code: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")]
    severity: WarningSeverity
    message: Annotated[str, Field(min_length=1, max_length=500)]
    page_number: PageNumber | None = None
    component_name: Annotated[str, Field(min_length=1, max_length=100)] | None = None

    @field_validator("message")
    @classmethod
    def message_is_safe(cls, value: str) -> str:
        """Reject obvious stack traces and absolute local path disclosures."""
        if _contains_sensitive_diagnostic(value):
            raise ValueError("warning message contains sensitive diagnostic details")
        return value


class PdfPageRotation(PreprocessingModel):
    """Record one page's normalized source rotation in clockwise degrees."""
    page_number: PageNumber
    degrees: Literal[0, 90, 180, 270]


class PdfInspectionResult(PreprocessingModel):
    """Describe safe document-level facts discovered by future PDF inspection."""
    pdf_version: Annotated[str, Field(max_length=32)] | None = None
    page_count: Annotated[int, Field(ge=1)] | None
    is_encrypted: bool
    processing_permitted: bool
    metadata: dict[str, str] = Field(default_factory=dict)
    page_rotations: list[PdfPageRotation]
    warnings: list[PreprocessingWarning] = Field(default_factory=list)

    @field_validator("metadata")
    @classmethod
    def metadata_is_bounded(cls, value: dict[str, str]) -> dict[str, str]:
        """Allow only a small map of short textual PDF metadata values."""
        if len(value) > 32 or any(not key or len(key) > 100 or len(item) > 1000
                                  for key, item in value.items()):
            raise ValueError("PDF metadata exceeds safe bounds")
        if any(_contains_sensitive_diagnostic(item) for item in value.values()):
            raise ValueError("PDF metadata contains an unsafe local path")
        return value

    @model_validator(mode="after")
    def rotations_cover_every_page(self) -> "PdfInspectionResult":
        """Require unavailable page data only for documents that cannot be processed."""
        if self.page_count is None:
            if self.processing_permitted or self.page_rotations:
                raise ValueError("unknown page count is allowed only for unprocessable PDFs")
            return self
        if [item.page_number for item in self.page_rotations] != list(
            range(1, self.page_count + 1)
        ):
            raise ValueError("page rotations must cover sequential pages")
        if any(warning.page_number is not None and warning.page_number > self.page_count
               for warning in self.warnings):
            raise ValueError("inspection warning page number is out of range")
        return self


class FontMetadata(PreprocessingModel):
    """Preserve safely representable native font formatting without interpretation."""
    family: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    size_points: Annotated[float, Field(gt=0)] | None = None
    bold: bool | None = None
    italic: bool | None = None


class NativeTextBlock(PreprocessingModel):
    """Represent one positioned native PDF text block in page reading order."""
    block_id: Identifier
    page_number: PageNumber
    reading_order: ReadingOrder
    text: str
    bounding_box: BoundingBox
    extraction_source: Literal[ExtractionSource.NATIVE] = ExtractionSource.NATIVE
    font: FontMetadata | None = None


class NativeTextExtractionResult(PreprocessingModel):
    """Return ordered embedded text plus deterministic counts for one page."""
    page_number: PageNumber
    blocks: list[NativeTextBlock]
    plain_text: str
    warnings: list[PreprocessingWarning] = Field(default_factory=list)
    character_count: Annotated[int, Field(ge=0)]
    word_count: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def blocks_match_page_and_order(self) -> "NativeTextExtractionResult":
        """Require matching page numbers and unique, increasing reading positions."""
        if any(block.page_number != self.page_number for block in self.blocks):
            raise ValueError("native text block page numbers must match")
        orders = [block.reading_order for block in self.blocks]
        if orders != sorted(orders) or len(orders) != len(set(orders)):
            raise ValueError("native text reading order must be unique and ordered")
        if any(warning.page_number not in (None, self.page_number) for warning in self.warnings):
            raise ValueError("native text warning page numbers must match")
        return self


class LayoutBlock(PreprocessingModel):
    """Represent one positioned physical page element without semantic classification."""
    block_id: Identifier
    page_number: PageNumber
    reading_order: ReadingOrder
    block_type: BlockType
    bounding_box: BoundingBox
    text: str | None = None
    extraction_source: ExtractionSource


class LayoutExtractionResult(PreprocessingModel):
    """Return ordered positioned elements and warnings for one page."""
    page_number: PageNumber
    width_points: Annotated[float, Field(gt=0)]
    height_points: Annotated[float, Field(gt=0)]
    blocks: list[LayoutBlock]
    warnings: list[PreprocessingWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def blocks_match_page(self) -> "LayoutExtractionResult":
        """Require all positioned elements to belong to this page."""
        if any(block.page_number != self.page_number for block in self.blocks):
            raise ValueError("layout block page numbers must match")
        orders = [block.reading_order for block in self.blocks]
        if orders != sorted(orders) or len(orders) != len(set(orders)):
            raise ValueError("layout reading order must be unique and ordered")
        if any(warning.page_number not in (None, self.page_number) for warning in self.warnings):
            raise ValueError("layout warning page numbers must match")
        return self


class TableCell(PreprocessingModel):
    """Preserve one table cell's grid position, text, spans, and optional geometry."""
    row_index: Annotated[int, Field(ge=0)]
    column_index: Annotated[int, Field(ge=0)]
    text: str
    bounding_box: BoundingBox | None = None
    row_span: Annotated[int, Field(ge=1)] = 1
    column_span: Annotated[int, Field(ge=1)] = 1


class TableRow(PreprocessingModel):
    """Group cells belonging to one zero-based table row."""
    row_index: Annotated[int, Field(ge=0)]
    cells: list[TableCell]

    @model_validator(mode="after")
    def cells_match_row(self) -> "TableRow":
        """Require cell row indexes and column positions to be consistent and unique."""
        if any(cell.row_index != self.row_index for cell in self.cells):
            raise ValueError("table cell row indexes must match their row")
        columns = [cell.column_index for cell in self.cells]
        if len(columns) != len(set(columns)):
            raise ValueError("table cell column indexes must be unique within a row")
        return self


class ExtractedTable(PreprocessingModel):
    """Preserve one positioned table without converting rows into product candidates."""
    table_id: Identifier
    page_number: PageNumber
    bounding_box: BoundingBox
    table_index: Annotated[int, Field(ge=0)]
    rows: list[TableRow]
    warnings: list[PreprocessingWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def rows_are_ordered(self) -> "ExtractedTable":
        """Require unique ordered rows and page-consistent table warnings."""
        indexes = [row.row_index for row in self.rows]
        if indexes != sorted(indexes) or len(indexes) != len(set(indexes)):
            raise ValueError("table row indexes must be unique and ordered")
        if any(warning.page_number not in (None, self.page_number) for warning in self.warnings):
            raise ValueError("table warning page numbers must match")
        return self


class TableExtractionResult(PreprocessingModel):
    """Return indexed structured tables and warnings for one page."""
    page_number: PageNumber
    tables: list[ExtractedTable]
    warnings: list[PreprocessingWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def tables_match_page(self) -> "TableExtractionResult":
        """Require all tables to belong to this page and have unique page indexes."""
        if any(table.page_number != self.page_number for table in self.tables):
            raise ValueError("table page numbers must match")
        indexes = [table.table_index for table in self.tables]
        if len(indexes) != len(set(indexes)):
            raise ValueError("table indexes must be unique within a page")
        if any(warning.page_number not in (None, self.page_number) for warning in self.warnings):
            raise ValueError("table extraction warning page numbers must match")
        return self


class PageRenderResult(PreprocessingModel):
    """Reference one stored full-page raster without embedding bytes or local paths."""
    page_number: PageNumber
    storage_key: Annotated[str, Field(min_length=1, max_length=1024)]
    media_type: Literal["image/png", "image/jpeg"]
    pixel_width: Annotated[int, Field(gt=0)]
    pixel_height: Annotated[int, Field(gt=0)]
    dpi: Annotated[int, Field(gt=0, le=2400)]
    sha256_checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("storage_key")
    @classmethod
    def storage_key_is_safe(cls, value: str) -> str:
        """Reject absolute and traversing rendered-object references."""
        return validate_storage_key(value)


class PageQualityAssessment(PreprocessingModel):
    """Record deterministic signals and the future rule-based OCR decision."""
    page_number: PageNumber
    native_character_count: Annotated[int, Field(ge=0)]
    readable_character_ratio: Annotated[float, Field(ge=0, le=1)]
    native_extraction_empty: bool
    suspicious_replacement_characters_found: bool
    ocr_required: bool
    ocr_reasons: list[OcrDecisionReason]

    @field_validator("readable_character_ratio")
    @classmethod
    def ratio_is_finite(cls, value: float) -> float:
        """Reject non-finite ratios even when numeric bounds appear satisfied."""
        if not math.isfinite(value):
            raise ValueError("readable character ratio must be finite")
        return value


class OcrTextBlock(PreprocessingModel):
    """Represent one positioned OCR text block while preserving its OCR origin."""
    block_id: Identifier
    page_number: PageNumber
    reading_order: ReadingOrder
    text: str
    bounding_box: BoundingBox
    extraction_source: Literal[ExtractionSource.OCR] = ExtractionSource.OCR
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None

    @field_validator("confidence")
    @classmethod
    def confidence_is_finite(cls, value: float | None) -> float | None:
        """Reject non-finite OCR confidence values."""
        if value is not None and not math.isfinite(value):
            raise ValueError("OCR confidence must be finite")
        return value


class OcrPageResult(PreprocessingModel):
    """Return explicit OCR state, ordered OCR text, engine identity, and warnings."""
    page_number: PageNumber
    status: OcrStatus
    blocks: list[OcrTextBlock]
    plain_text: str
    warnings: list[PreprocessingWarning] = Field(default_factory=list)
    engine_name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    engine_version: Annotated[str, Field(min_length=1, max_length=100)] | None = None

    @model_validator(mode="after")
    def state_is_consistent(self) -> "OcrPageResult":
        """Prevent failed, pending, or unnecessary OCR from appearing successful."""
        if any(block.page_number != self.page_number for block in self.blocks):
            raise ValueError("OCR block page numbers must match")
        orders = [block.reading_order for block in self.blocks]
        if orders != sorted(orders) or len(orders) != len(set(orders)):
            raise ValueError("OCR reading order must be unique and ordered")
        if self.status != OcrStatus.COMPLETED and (self.blocks or self.plain_text):
            raise ValueError("only completed OCR may contain extracted text")
        if self.status == OcrStatus.FAILED and not any(
                warning.severity == WarningSeverity.ERROR for warning in self.warnings):
            raise ValueError("failed OCR requires an error warning")
        if any(warning.page_number not in (None, self.page_number) for warning in self.warnings):
            raise ValueError("OCR warning page numbers must match")
        return self


class PreprocessedPage(PreprocessingModel):
    """Combine validated deterministic representations for one normalized PDF page."""
    page_number: PageNumber
    width_points: Annotated[float, Field(gt=0)]
    height_points: Annotated[float, Field(gt=0)]
    rotation_degrees: Literal[0, 90, 180, 270]
    native_text: NativeTextExtractionResult
    layout: LayoutExtractionResult
    tables: TableExtractionResult
    render: PageRenderResult
    quality: PageQualityAssessment
    ocr: OcrPageResult | None = None
    warnings: list[PreprocessingWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def nested_results_are_consistent(self) -> "PreprocessedPage":
        """Validate page identity, run-local IDs, and the declared OCR decision."""
        nested_pages = [self.native_text.page_number, self.layout.page_number,
                        self.tables.page_number, self.render.page_number, self.quality.page_number]
        if self.ocr is not None:
            nested_pages.append(self.ocr.page_number)
        if any(page != self.page_number for page in nested_pages):
            raise ValueError("all nested page numbers must match")
        block_ids = [block.block_id for block in self.native_text.blocks]
        block_ids.extend(block.block_id for block in self.layout.blocks)
        if self.ocr:
            block_ids.extend(block.block_id for block in self.ocr.blocks)
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("block identifiers must be unique within a page")
        table_ids = [table.table_id for table in self.tables.tables]
        if len(table_ids) != len(set(table_ids)):
            raise ValueError("table identifiers must be unique within a page")
        if self.quality.ocr_required:
            if self.ocr is None or self.ocr.status not in {OcrStatus.COMPLETED, OcrStatus.FAILED}:
                raise ValueError("required OCR must have a completed or failed result")
        elif self.ocr is not None and self.ocr.status != OcrStatus.NOT_REQUIRED:
            raise ValueError("OCR result conflicts with a not-required decision")
        if self.quality.native_character_count != self.native_text.character_count:
            raise ValueError("quality and native text character counts must agree")
        if any(warning.page_number not in (None, self.page_number) for warning in self.warnings):
            raise ValueError("page warning page numbers must match")
        return self


class PreprocessedDocument(PreprocessingModel):
    """Provide the validated deterministic document contract for a future analysis agent."""
    schema_version: Annotated[str, Field(min_length=1, max_length=50)]
    organization_id: uuid.UUID
    document_id: uuid.UUID
    processing_job_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    processor_version: Annotated[str, Field(min_length=1, max_length=100)]
    original_document_checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    inspection: PdfInspectionResult
    pages: list[PreprocessedPage]
    warnings: list[PreprocessingWarning] = Field(default_factory=list)
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def document_is_internally_consistent(self) -> "PreprocessedDocument":
        """Validate page coverage, time order, and run-wide block/table uniqueness."""
        if not self.inspection.processing_permitted or self.inspection.page_count is None:
            raise ValueError("final documents require a processable PDF inspection")
        if self.completed_at < self.started_at:
            raise ValueError("completion time cannot precede start time")
        if len(self.pages) != self.inspection.page_count:
            raise ValueError("inspection page count must equal output page count")
        if [page.page_number for page in self.pages] != list(range(1, self.inspection.page_count + 1)):
            raise ValueError("document pages must be sequential and begin at one")
        rotations = {item.page_number: item.degrees for item in self.inspection.page_rotations}
        if any(rotations[page.page_number] != page.rotation_degrees for page in self.pages):
            raise ValueError("page rotations must agree with PDF inspection")
        block_ids: list[str] = []
        table_ids: list[str] = []
        for page in self.pages:
            block_ids.extend(block.block_id for block in page.native_text.blocks)
            block_ids.extend(block.block_id for block in page.layout.blocks)
            if page.ocr:
                block_ids.extend(block.block_id for block in page.ocr.blocks)
            table_ids.extend(table.table_id for table in page.tables.tables)
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("block identifiers must be unique within a preprocessing run")
        if len(table_ids) != len(set(table_ids)):
            raise ValueError("table identifiers must be unique within a preprocessing run")
        if any(warning.page_number is not None and warning.page_number > self.inspection.page_count
               for warning in self.warnings):
            raise ValueError("document warning page number is out of range")
        return self
