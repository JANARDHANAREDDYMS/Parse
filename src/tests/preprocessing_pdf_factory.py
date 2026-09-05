"""Generate temporary visual PDFs for focused preprocessing component tests only."""

from io import BytesIO
from pathlib import Path

import pymupdf
from PIL import Image


def create_visual_pdf(
    path: Path,
    *,
    page_sizes: list[tuple[float, float]] | None = None,
    rotate_last_page: bool = False,
    include_content: bool = True,
    include_annotation: bool = False,
) -> None:
    """Create a small PDF with literal text, one image, and simple vector geometry."""

    sizes = page_sizes or [(200, 300)]
    image_buffer = BytesIO()
    Image.new("RGB", (10, 10), (220, 20, 20)).save(image_buffer, format="PNG")
    document = pymupdf.open()
    try:
        for index, (width, height) in enumerate(sizes):
            page = document.new_page(width=width, height=height)
            if include_content:
                page.insert_text((20, 40), "Pr0duct SKU-1")
                page.insert_image(
                    pymupdf.Rect(width - 70, 70, width - 20, 120),
                    stream=image_buffer.getvalue(),
                )
                page.draw_rect(pymupdf.Rect(15, 140, 100, 190))
                page.draw_line(pymupdf.Point(15, 210), pymupdf.Point(100, 210))
            if include_annotation:
                page.add_text_annot(pymupdf.Point(130, 30), "annotation")
            if rotate_last_page and index == len(sizes) - 1:
                page.set_rotation(90)
        document.save(path)
    finally:
        document.close()
