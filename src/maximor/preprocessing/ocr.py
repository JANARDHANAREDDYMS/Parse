"""Run bounded Tesseract OCR over one stored rendered page.

Input is a `PageRenderResult`. Output preserves OCR separately from native text;
this module does not alter native extraction, infer meaning, or expose paths.
"""
import asyncio
import hashlib
from collections import defaultdict
from io import BytesIO
from typing import Any, Callable, Protocol
from PIL import Image, UnidentifiedImageError
from maximor.preprocessing.contracts import validate_storage_key
from maximor.preprocessing.errors import OcrError
from maximor.preprocessing.schemas import BoundingBox, OcrPageResult, OcrStatus, OcrTextBlock, PageNumber, PageRenderResult, PreprocessingWarning, WarningSeverity
from maximor.storage import ObjectStorage, StorageError

class OcrExtractor(Protocol):
    """Perform conditional OCR only when called by the document preprocessor."""
    async def extract(self, rendered_page: PageRenderResult, page_number: PageNumber) -> OcrPageResult:
        """Return completed or explicit failed OCR output without native text mutation."""
        ...

class TesseractOcrExtractor:
    """Read a verified page raster and use Tesseract word data to form OCR lines.

    Input is a safe stored-page reference; output is `OcrPageResult`. Tesseract
    availability failures are represented as failed OCR rather than hidden success.
    """
    def __init__(self, storage: ObjectStorage, *, language: str = "eng", timeout_seconds: float = 30, maximum_input_bytes: int = 25 * 1024 * 1024, maximum_pixel_count: int = 40_000_000, image_to_data: Callable[..., Any] | None = None, version_getter: Callable[[], str] | None = None) -> None:
        """Inject bounded storage and optional Tesseract call boundaries."""
        if image_to_data is None or version_getter is None:
            import pytesseract
            image_to_data = image_to_data or pytesseract.image_to_data
            version_getter = version_getter or pytesseract.get_tesseract_version
        self._storage, self._language, self._timeout = storage, language, timeout_seconds
        self._maximum_bytes, self._maximum_pixels = maximum_input_bytes, maximum_pixel_count
        self._image_to_data, self._version_getter = image_to_data, version_getter

    async def extract(self, rendered_page: PageRenderResult, page_number: PageNumber) -> OcrPageResult:
        """Verify rendered bytes then run blocking image/Tesseract work in a thread."""
        if rendered_page.page_number != page_number:
            raise OcrError(page_number)
        try: validate_storage_key(rendered_page.storage_key)
        except ValueError: raise OcrError(page_number) from None
        try:
            encoded = await self._storage.read(rendered_page.storage_key, maximum_bytes=self._maximum_bytes)
        except StorageError:
            raise OcrError(page_number) from None
        if hashlib.sha256(encoded).hexdigest() != rendered_page.sha256_checksum:
            raise OcrError(page_number)
        return await asyncio.to_thread(self._ocr_sync, encoded, rendered_page)

    def _ocr_sync(self, encoded: bytes, rendered: PageRenderResult) -> OcrPageResult:
        page_number=rendered.page_number
        try:
            with Image.open(BytesIO(encoded)) as image:
                image.load()
                if image.width != rendered.pixel_width or image.height != rendered.pixel_height or image.width * image.height > self._maximum_pixels:
                    raise OcrError(page_number)
                from pytesseract import Output, TesseractError, TesseractNotFoundError
                data = self._image_to_data(image, lang=self._language, timeout=self._timeout, output_type=Output.DICT)
                version = str(self._version_getter())
        except OcrError: raise
        except (RuntimeError, TimeoutError, UnidentifiedImageError, OSError):
            return self._failed(page_number, "ocr_engine_failed", "OCR could not be completed for the page.")
        except Exception:
            return self._failed(page_number, "ocr_engine_failed", "OCR could not be completed for the page.")
        lines: dict[tuple[int,int,int], list[tuple[int,str,float,float,float,float,float]]] = defaultdict(list)
        for i, raw in enumerate(data.get("text", [])):
            text=str(raw).strip()
            try: confidence=float(data.get("conf", [])[i])
            except (ValueError, TypeError, IndexError): confidence=-1
            if not text or confidence < 0: continue
            try:
                key=(int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
                x,y,w,h=(float(data[name][i]) for name in ("left","top","width","height"))
            except (KeyError, ValueError, TypeError, IndexError): continue
            lines[key].append((i,text,confidence,x,y,w,h))
        values=[]
        scale=72/rendered.dpi
        for words in lines.values():
            words.sort(key=lambda word: word[0]); xs=[word[3] for word in words]; ys=[word[4] for word in words]
            rights=[word[3]+word[5] for word in words]; bottoms=[word[4]+word[6] for word in words]
            values.append((min(ys)*scale,min(xs)*scale, " ".join(word[1] for word in words), BoundingBox(x0=min(xs)*scale,y0=min(ys)*scale,x1=max(rights)*scale,y1=max(bottoms)*scale), sum(word[2] for word in words)/len(words)/100))
        values.sort(key=lambda value:(value[0],value[1]))
        blocks=[OcrTextBlock(block_id=f"ocr:p{page_number:04d}:b{i:06d}",page_number=page_number,reading_order=i,text=value[2],bounding_box=value[3],confidence=max(0,min(1,value[4]))) for i,value in enumerate(values)]
        warnings=[] if blocks else [PreprocessingWarning(code="ocr_no_text",severity=WarningSeverity.INFORMATION,message="OCR completed without recognized text.",page_number=page_number,component_name="ocr")]
        return OcrPageResult(page_number=page_number,status=OcrStatus.COMPLETED,blocks=blocks,plain_text="\n".join(block.text for block in blocks),warnings=warnings,engine_name="tesseract",engine_version=version[:100])

    @staticmethod
    def _failed(page_number:int, code:str, message:str) -> OcrPageResult:
        return OcrPageResult(page_number=page_number,status=OcrStatus.FAILED,blocks=[],plain_text="",warnings=[PreprocessingWarning(code=code,severity=WarningSeverity.ERROR,message=message,page_number=page_number,component_name="ocr")],engine_name="tesseract")
