# Deterministic document preprocessing contracts

These files define library boundaries. They are not separate command-line programs.
The registered `document_preprocessing` worker handler invokes the concrete
`DocumentPreprocessor` implementation.

| Component | Input | Future operation | Output |
|---|---|---|---|
| `PypdfPdfInspector` (`PdfInspector`) | `TrustedPdfSource` | **Implemented with `pypdf`:** validate PDF structure, encryption, page count, rotations, and allowlisted metadata | `PdfInspectionResult` |
| `PymupdfNativeTextExtractor` (`NativeTextExtractor`) | Trusted source and one-based page number | **Implemented with PyMuPDF:** extract embedded line-level text | `NativeTextExtractionResult` |
| `PdfplumberLayoutExtractor` (`LayoutExtractor`) | Trusted source and one-based page number | **Implemented with `pdfplumber`:** extract positioned line-level text and visible image/drawing regions | `LayoutExtractionResult` |
| `PdfplumberTableExtractor` (`TableExtractor`) | Trusted source and one-based page number | **Implemented with `pdfplumber`:** detect bounded physical tables | `TableExtractionResult` |
| `PymupdfPageRenderer` (`PageRenderer`) | Trusted source, one-based page number, rendering configuration, and caller-generated output key | **Implemented with PyMuPDF:** render and store a complete page image | `PageRenderResult` |
| `RuleBasedPageQualityEvaluator` (`PageQualityEvaluator`) | Native text, layout, and table results | **Implemented:** calculate deterministic OCR decision signals | `PageQualityAssessment` |
| `TesseractOcrExtractor` (`OcrExtractor`) | Stored rendered-page reference and page number | **Implemented with Pillow and Tesseract:** OCR only requested pages | `OcrPageResult` |
| `DeterministicDocumentPreprocessor` (`DocumentPreprocessor`) | `DocumentPreprocessingRequest` | **Implemented:** sequentially coordinate all deterministic components | `PreprocessedDocument` |

All preprocessing library components, persistence, worker registration, and API job
creation are implemented.

## Canonical conventions

- Page numbers are one-based; reading order is zero-based within each page.
- Bounding boxes are `x0, y0, x1, y1`, use a top-left origin, and contain finite
  PDF-point coordinates after page rotation is normalized.
- `x1 >= x0` and `y1 >= y0`.
- Block and table identifiers are unique within a preprocessing run. Future
  evidence references use preprocessing run ID, page number, block/table ID,
  and bounding box.
- Native and OCR text remain separate. OCR never overwrites native text.
- Serialized storage references are relative, path-safe object keys. Temporary
  local paths may exist only inside deterministic library code and must never be
  serialized, persisted, logged, or returned by an API.
- Preprocessing describes document structure only. It does not infer products,
  pricing sections, commercial status, contract meaning, or SKU mappings.

## Implemented layout and rendering behavior

Layout blocks are sorted deterministically by top coordinate, left coordinate,
stable block-type priority (text, image, drawing, unknown), and original extraction
position. Reading order is then assigned sequentially from zero. Stable identifiers
use `layout:pNNNN:bNNNNNN`, for example `layout:p0001:b000000`. Exact duplicate
physical regions are retained once and produce a safe warning.

Page rendering supports `image/png` and RGB `image/jpeg`; JPEG quality is fixed at
85. The default DPI is 200. Each request is bounded by a 40,000,000-pixel raster
limit and a 25 MiB encoded-output limit, both of which callers may lower or adjust
explicitly. Unsafe requests fail rather than silently reducing DPI. PyMuPDF is
dual-licensed under AGPL and commercial terms, so its licensing must be reviewed
before production distribution.

The orchestration order is: materialize a trusted source, inspect it,
then for every page extract native text, layout, and tables; render the full
page; evaluate quality; conditionally run OCR; validate `PreprocessedPage`; and
finally validate `PreprocessedDocument`.

## Native text, tables, quality, and OCR

Native text uses geometrically sorted line blocks with IDs `native:pNNNN:bNNNNNN`.
`plain_text` joins lines with newlines; its length is the character count and the
word count is Python whitespace-delimited tokens. Mixed-font lines omit font data.
Tables use line-based pdfplumber settings, geometric ordering, IDs
`table:pNNNN:tNNNN`, and preserve empty cells; unknown merged spans remain one.

The quality evaluator ignores whitespace, divides readable printable Unicode
characters by all non-whitespace characters, and requires OCR for empty native
text, a ratio below 0.90, or replacement/control characters. Ordinary readable
misspellings do not trigger OCR; semantic corruption is deferred to
`DocumentAnalysisAgent`.

OCR verifies its stored render checksum and dimensions, groups Tesseract words into
line blocks with IDs `ocr:pNNNN:bNNNNNN`, and converts pixels to PDF points using
`72 / DPI`. OCR failures are explicit failed OCR results with safe warnings; native
text remains intact. The service processes pages sequentially, keeps render artifacts
only on a valid result, and deletes temporary source material in all cases.

## Persistence and agent-facing access

The validated `PreprocessedDocument` is serialized as deterministic compact JSON and
stored losslessly as a gzip artifact with a `.json.gz` key. PostgreSQL records the
compressed artifact checksum, uncompressed content checksum, compression method, and
both sizes. Loading is bounded, checksum-verified, decompressed, and Pydantic-validated.

Page, block, and table projections remain searchable in PostgreSQL. No representation
is removed from the canonical artifact. A future `DocumentAnalysisAgent` should load
only the overview and relevant page text, blocks, tables, or render references through
narrow tools instead of placing the entire canonical result into every model request.
