from __future__ import annotations

import zipfile

import pytest


@pytest.mark.parametrize("version", [2, 3])
def test_standard_zip_preserves_epub_documents_and_resources(epub_factory, version: int) -> None:
    source = epub_factory(version)
    with zipfile.ZipFile(source) as archive:
        entries = {entry.filename: archive.read(entry) for entry in archive.infolist()}

    assert entries["OEBPS/images/cover.png"].startswith(b"\x89PNG\r\n\x1a\n")
    assert b'xmlns="http://www.w3.org/1999/xhtml"' in entries["OEBPS/text/chapter.xhtml"]
    assert b'id="same-id"' in entries["OEBPS/text/chapter.xhtml"]
    assert b'<text id="same-id">SVG</text>' in entries["OEBPS/images/diagram.svg"]
    assert b"styles/book.css" in entries["OEBPS/content.opf"]
    assert b"text/chapter.xhtml" in entries["OEBPS/content.opf"]
    assert f"Author {version}".encode() in entries["OEBPS/content.opf"]
    assert f"Translator {version}".encode() in entries["OEBPS/content.opf"]
    assert b'playOrder="2"' in entries["OEBPS/toc.ncx"]
