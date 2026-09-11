from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from bookmerger.cli import BuildError, Command, assemble
from bookmerger.download import DownloadedSource


class LocalDownloader:
    def __init__(self, sources: tuple[Path, ...]) -> None:
        self.sources = sources

    def download_all(self, urls: tuple[str, ...], directory: Path) -> tuple[DownloadedSource, ...]:
        directory.mkdir(parents=True)
        return tuple(
            DownloadedSource(url, source, "epub")
            for url, source in zip(urls, self.sources, strict=True)
        )


def test_pipeline_publishes_valid_epub_atomically(epub_factory, tmp_path: Path) -> None:
    output = tmp_path / "collection.epub"
    command = Command("Collection", output, ("one", "two"), False)

    assert (
        assemble(command, downloader=LocalDownloader((epub_factory(2), epub_factory(3)))) == output
    )
    assert output.is_file()

    with pytest.raises(BuildError, match="already exists"):
        assemble(command, downloader=LocalDownloader((epub_factory(2), epub_factory(3))))


def test_pipeline_keeps_existing_output_when_validation_fails(
    epub_factory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "collection.epub"
    output.write_bytes(b"old result")
    command = Command("Collection", output, ("one",), True)

    monkeypatch.setattr(
        "bookmerger.cli.validate_epub", lambda *_: (_ for _ in ()).throw(ValueError("bad"))
    )
    with pytest.raises(BuildError, match="bad"):
        assemble(command, downloader=LocalDownloader((epub_factory(3),)))

    assert output.read_bytes() == b"old result"
    assert not list(tmp_path.glob(".collection-*.epub"))


@pytest.mark.skipif(not os.environ.get("FBC_INTEGRATION"), reason="requires pinned fbc")
def test_mixed_fb2_and_epub_uses_pinned_converter(epub_factory, tmp_path: Path) -> None:
    output = tmp_path / "collection.epub"
    command = Command("Collection", output, ("fb2", "epub2", "epub3"), False)
    sources = (
        Path("tests/fixtures/book.fb2"),
        epub_factory(2),
        epub_factory(3),
    )

    class MixedDownloader(LocalDownloader):
        def download_all(
            self, urls: tuple[str, ...], directory: Path
        ) -> tuple[DownloadedSource, ...]:
            directory.mkdir(parents=True)
            return (
                DownloadedSource(urls[0], sources[0], "fb2"),
                DownloadedSource(urls[1], sources[1], "epub"),
                DownloadedSource(urls[2], sources[2], "epub"),
            )

    assemble(command, downloader=MixedDownloader(sources))
    with zipfile.ZipFile(output) as archive:
        package = archive.read("EPUB/package.opf")
        nav = archive.read("EPUB/nav.xhtml")
        first_book_text = b"".join(
            archive.read(name)
            for name in archive.namelist()
            if name.startswith("books/0001/") and name.endswith((".html", ".xhtml"))
        )
        images = [
            archive.read(name)
            for name in archive.namelist()
            if name.startswith("books/0001/")
            and archive.read(name).startswith(b"\x89PNG\r\n\x1a\n")
        ]
    assert b"Translator Trudy" in package
    assert b"FB2 fixture" in nav and b"Fixture 2" in nav and b"Fixture 3" in nav
    assert b"Chapter" in first_book_text and images
