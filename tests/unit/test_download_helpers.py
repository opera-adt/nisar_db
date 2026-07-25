"""Unit tests for download helper functions (no real network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nisar_db import download


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/path/to/GRANULE.h5", "GRANULE.h5"),
        ("https://example.com/GRANULE.h5?token=abc&x=1", "GRANULE.h5"),
        ("s3://bucket/key/GRANULE.h5", "GRANULE.h5"),
    ],
)
def test_filename_from_url(url: str, expected: str) -> None:
    assert download._filename_from_url(url) == expected


def test_download_from_url_skips_existing(tmp_path: Path) -> None:
    existing = tmp_path / "already.h5"
    existing.write_bytes(b"data")

    # skip_existing short-circuits before any network call is attempted.
    result = download.download_from_url(
        "https://example.com/already.h5",
        output_dir=tmp_path,
        skip_existing=True,
    )
    assert result == str(existing)


class _FakeResponse:
    """Minimal stand-in for a streamed requests.Response."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.headers = {"content-length": str(sum(len(c) for c in chunks))}

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size: int) -> list[bytes]:  # noqa: ARG002
        return self._chunks


def test_stream_to_file_writes_bytes_and_returns_mb(tmp_path: Path) -> None:
    dest = tmp_path / "out.bin"
    payload = [b"a" * 1024, b"b" * 1024]
    size_mb = download._stream_to_file(
        _FakeResponse(payload), dest, show_progress=False
    )
    assert dest.read_bytes() == b"".join(payload)
    assert size_mb == pytest.approx(2048 / (1024 * 1024))
