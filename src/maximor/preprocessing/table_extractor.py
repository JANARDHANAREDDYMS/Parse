"""Detect physical page tables with pdfplumber.

Input is a trusted source/page. Output preserves grids and geometry; no table row
is converted into a product, SKU, or other semantic interpretation.
"""
import asyncio
from typing import Any, Callable, Protocol
from maximor.preprocessing.contracts import TrustedPdfSource
from maximor.preprocessing.errors import InvalidPageNumberError, LayoutExtractionError, PdfOpenError
from maximor.preprocessing.schemas import BoundingBox, ExtractedTable, PageNumber, PreprocessingWarning, TableCell, TableExtractionResult, TableRow, WarningSeverity

DEFAULT_TABLE_SETTINGS = {"vertical_strategy": "lines", "horizontal_strategy": "lines", "snap_tolerance": 3, "join_tolerance": 3}

class TableExtractor(Protocol):
    """Detect physical grids and retain table geometry without meaning."""
    async def extract(self, source: TrustedPdfSource, page_number: PageNumber) -> TableExtractionResult:
        """Return deterministic table structures for one page."""
        ...

class PdfplumberTableExtractor:
    """Use bounded line-based pdfplumber table detection.

    Input is trusted PDF/page; output is `TableExtractionResult`. Merged spans are
    left at one when not reliable, and product interpretation is explicitly absent.
    """
    def __init__(self, pdf_opener: Callable[..., Any] | None = None, *, table_settings: dict[str, Any] | None = None, maximum_tables: int = 20, maximum_rows: int = 200, maximum_columns: int = 100, maximum_cells: int = 10_000) -> None:
        """Receive replaceable opener, explicit settings, and deterministic limits."""
        if pdf_opener is None:
            from pdfplumber import open as pdfplumber_open
            pdf_opener = pdfplumber_open
        self._pdf_opener = pdf_opener
        self._settings = dict(DEFAULT_TABLE_SETTINGS if table_settings is None else table_settings)
        self._maximum_tables, self._maximum_rows, self._maximum_columns, self._maximum_cells = maximum_tables, maximum_rows, maximum_columns, maximum_cells

    async def extract(self, source: TrustedPdfSource, page_number: PageNumber) -> TableExtractionResult:
        """Run blocking pdfplumber work in a thread."""
        if not isinstance(page_number, int) or page_number < 1:
            raise InvalidPageNumberError(int(page_number), "table_extractor")
        return await asyncio.to_thread(self._extract_sync, source, page_number)

    def _extract_sync(self, source: TrustedPdfSource, page_number: int) -> TableExtractionResult:
        try:
            handle = source.local_path.open("rb")
            with self._pdf_opener(handle) as pdf:
                if page_number > len(pdf.pages):
                    raise InvalidPageNumberError(page_number, "table_extractor")
                found = pdf.pages[page_number - 1].find_tables(self._settings)
        except InvalidPageNumberError:
            raise
        except OSError:
            raise PdfOpenError("table_extractor") from None
        except Exception:
            raise LayoutExtractionError(page_number) from None
        finally:
            try: handle.close()
            except UnboundLocalError: pass
        warnings: list[PreprocessingWarning] = []
        unique = []
        seen = set()
        for table in found:
            key = tuple(round(v, 4) for v in table.bbox)
            if key not in seen:
                seen.add(key); unique.append(table)
        if len(unique) < len(found):
            warnings.append(self._warning("duplicate_table_removed", "A duplicate detected table was removed.", page_number, WarningSeverity.INFORMATION))
        unique.sort(key=lambda table: (table.bbox[1], table.bbox[0]))
        if len(unique) > self._maximum_tables:
            unique = unique[:self._maximum_tables]
            warnings.append(self._warning("table_limit_reached", "Detected tables exceeded the configured limit.", page_number, WarningSeverity.WARNING))
        tables=[]; cells_seen=0
        try:
            for index, table in enumerate(unique):
                rows=[]
                for row_index, row in enumerate(table.rows[:self._maximum_rows]):
                    cells=[]
                    for col_index, cell_box in enumerate(row.cells[:self._maximum_columns]):
                        if cells_seen >= self._maximum_cells: break
                        text = "" if cell_box is None else (table.extract()[row_index][col_index] or "")
                        box = None if cell_box is None else BoundingBox(x0=cell_box[0], y0=cell_box[1], x1=cell_box[2], y1=cell_box[3])
                        cells.append(TableCell(row_index=row_index, column_index=col_index, text=text, bounding_box=box))
                        cells_seen += 1
                    rows.append(TableRow(row_index=row_index, cells=cells))
                bbox=table.bbox
                tables.append(ExtractedTable(table_id=f"table:p{page_number:04d}:t{index:04d}", page_number=page_number, table_index=index, bounding_box=BoundingBox(x0=bbox[0],y0=bbox[1],x1=bbox[2],y1=bbox[3]), rows=rows))
        except Exception:
            raise LayoutExtractionError(page_number) from None
        if cells_seen >= self._maximum_cells:
            warnings.append(self._warning("table_cell_limit_reached", "Detected table cells exceeded the configured limit.", page_number, WarningSeverity.WARNING))
        if not tables:
            warnings.append(self._warning("no_tables_detected", "No physical tables were detected on the page.", page_number, WarningSeverity.INFORMATION))
        return TableExtractionResult(page_number=page_number, tables=tables, warnings=warnings)

    @staticmethod
    def _warning(code: str, message: str, page_number: int, severity: WarningSeverity) -> PreprocessingWarning:
        return PreprocessingWarning(code=code, severity=severity, message=message, page_number=page_number, component_name="table_extractor")
