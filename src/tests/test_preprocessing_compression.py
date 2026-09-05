"""Verify canonical preprocessing compression is lossless and bounded."""

import gzip

import pytest

from maximor.preprocessing.persistence import (
    compress_canonical_json,
    decompress_canonical_json,
)


def test_gzip_round_trip_is_byte_exact() -> None:
    original = (
        b'{"native_text":"same representation","layout_text":"same representation",'
        b'"table_text":"same representation"}'
    )

    compressed = compress_canonical_json(original)

    assert compressed != original
    assert decompress_canonical_json(compressed, maximum_bytes=len(original)) == original


def test_compression_is_reproducible() -> None:
    original = b'{"complete":"representation"}'

    assert compress_canonical_json(original) == compress_canonical_json(original)


def test_expanded_size_limit_is_enforced() -> None:
    compressed = gzip.compress(b"x" * 101, mtime=0)

    with pytest.raises(ValueError, match="expanded size limit"):
        decompress_canonical_json(compressed, maximum_bytes=100)


def test_invalid_gzip_is_rejected_safely() -> None:
    with pytest.raises(ValueError, match="compression is invalid"):
        decompress_canonical_json(b"not-gzip", maximum_bytes=100)
