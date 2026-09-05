"""Test pypdf structural inspection using only PDFs generated in temporary directories."""

from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf._page import PageObject
from pypdf.generic import NameObject, NumberObject

from maximor.preprocessing.contracts import TrustedPdfSource
from maximor.preprocessing.errors import InvalidPdfError
from maximor.preprocessing.pdf_inspector import PypdfPdfInspector


def write_pdf(
    path: Path,
    *,
    pages: int = 1,
    metadata: dict[str, str] | None = None,
    password: str | None = None,
    rotations: list[int] | None = None,
) -> None:
    """Generate a small structural PDF without adding any text content."""
    writer = PdfWriter()
    for index in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        if rotations:
            page[NameObject("/Rotate")] = NumberObject(rotations[index])
    if metadata:
        writer.add_metadata(metadata)
    if password is not None:
        writer.encrypt(password)
    with path.open("wb") as handle:
        writer.write(handle)


def source(path: Path) -> TrustedPdfSource:
    """Wrap a temporary generated PDF with a safe logical storage key."""
    return TrustedPdfSource(path.resolve(), f"tests/{path.name}")


@pytest.mark.asyncio
async def test_valid_one_page_pdf_and_version(tmp_path):
    path = tmp_path / "one.pdf"
    write_pdf(path)
    result = await PypdfPdfInspector().inspect(source(path))
    assert result.page_count == 1
    assert result.pdf_version is not None
    assert result.processing_permitted is True
    assert result.page_rotations[0].degrees == 0


@pytest.mark.asyncio
async def test_valid_multi_page_pdf_count(tmp_path):
    path = tmp_path / "many.pdf"
    write_pdf(path, pages=3)
    result = await PypdfPdfInspector().inspect(source(path))
    assert result.page_count == 3
    assert [item.page_number for item in result.page_rotations] == [1, 2, 3]


@pytest.mark.asyncio
async def test_metadata_is_allowlisted_and_safe(tmp_path):
    path = tmp_path / "metadata.pdf"
    write_pdf(path, metadata={
        "/Title": "Safe title", "/Author": "Safe author",
        "/Subject": "Safe subject", "/Creator": "Test creator",
        "/Producer": "Test producer", "/CreationDate": "D:20260101000000Z",
        "/ModDate": "D:20260102000000Z", "/Keywords": "not allowlisted",
    })
    result = await PypdfPdfInspector().inspect(source(path))
    assert result.metadata == {
        "title": "Safe title", "author": "Safe author", "subject": "Safe subject",
        "creator": "Test creator", "producer": "Test producer",
        "creation_date": "D:20260101000000Z", "modification_date": "D:20260102000000Z",
    }


@pytest.mark.asyncio
async def test_metadata_limits_and_unsafe_values(tmp_path):
    path = tmp_path / "bounded.pdf"
    write_pdf(path, metadata={"/Title": "x" * 1200, "/Author": "/Users/private/name"})
    result = await PypdfPdfInspector().inspect(source(path))
    assert len(result.metadata["title"]) == 1000
    assert "author" not in result.metadata
    assert {warning.code for warning in result.warnings} >= {
        "pdf_metadata_value_truncated", "pdf_metadata_value_omitted"
    }


@pytest.mark.asyncio
async def test_page_rotations_are_normalized(tmp_path):
    path = tmp_path / "rotated.pdf"
    write_pdf(path, pages=3, rotations=[-90, 450, 45])
    result = await PypdfPdfInspector().inspect(source(path))
    assert [item.degrees for item in result.page_rotations] == [270, 90, 0]
    assert any(warning.code == "invalid_page_rotation" for warning in result.warnings)


@pytest.mark.asyncio
async def test_empty_password_encryption_is_readable_and_reported(tmp_path):
    path = tmp_path / "empty-password.pdf"
    write_pdf(path, password="")
    result = await PypdfPdfInspector().inspect(source(path))
    assert result.is_encrypted is True
    assert result.processing_permitted is True
    assert result.page_count == 1
    assert any(warning.code == "encrypted_pdf_empty_password" for warning in result.warnings)


@pytest.mark.asyncio
async def test_password_protected_pdf_is_not_processable(tmp_path):
    path = tmp_path / "protected.pdf"
    write_pdf(path, password="secret")
    result = await PypdfPdfInspector().inspect(source(path))
    assert result.is_encrypted is True
    assert result.processing_permitted is False
    assert result.page_count is None
    assert result.page_rotations == []
    assert result.warnings[0].code == "encrypted_pdf_password_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"not a pdf", b"%PDF-1.7\ntruncated"])
async def test_invalid_and_truncated_input_raise_safe_error(tmp_path, payload):
    path = tmp_path / "bad.pdf"
    path.write_bytes(payload)
    with pytest.raises(InvalidPdfError) as captured:
        await PypdfPdfInspector().inspect(source(path))
    assert str(path.resolve()) not in str(captured.value)


@pytest.mark.asyncio
async def test_missing_file_raises_safe_error(tmp_path):
    path = tmp_path / "missing.pdf"
    with pytest.raises(InvalidPdfError) as captured:
        await PypdfPdfInspector().inspect(source(path))
    assert str(path.resolve()) not in str(captured.value)


@pytest.mark.asyncio
async def test_unreadable_file_raises_safe_error(tmp_path, monkeypatch):
    path = tmp_path / "unreadable.pdf"
    write_pdf(path)
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(InvalidPdfError) as captured:
        await PypdfPdfInspector().inspect(source(path))
    assert str(path.resolve()) not in str(captured.value)


@pytest.mark.asyncio
async def test_zero_page_pdf_is_rejected(tmp_path):
    path = tmp_path / "zero.pdf"
    write_pdf(path, pages=0)
    with pytest.raises(InvalidPdfError):
        await PypdfPdfInspector().inspect(source(path))


@pytest.mark.asyncio
async def test_inspection_never_calls_text_extraction(tmp_path, monkeypatch):
    path = tmp_path / "no-text.pdf"
    write_pdf(path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("text extraction was called")

    monkeypatch.setattr(PageObject, "extract_text", forbidden)
    result = await PypdfPdfInspector().inspect(source(path))
    assert result.page_count == 1


@pytest.mark.asyncio
async def test_reader_file_handle_is_closed(tmp_path):
    path = tmp_path / "closed.pdf"
    write_pdf(path)
    captured = []

    def factory(handle, **kwargs):
        from pypdf import PdfReader
        captured.append(handle)
        return PdfReader(handle, **kwargs)

    await PypdfPdfInspector(reader_factory=factory).inspect(source(path))
    assert captured[0].closed is True
