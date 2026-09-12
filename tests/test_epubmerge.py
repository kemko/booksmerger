from __future__ import annotations

import os
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from epubmerge.epubmerge import doMerge
from lxml import etree

from bookmerger.converter import FB2Converter


def test_epubmerge_merges_two_epub2_books_with_ncx(epub_factory, edit_epub, tmp_path: Path) -> None:
    first = epub_factory(2)
    second = tmp_path / "second.epub"
    second.write_bytes(first.read_bytes())
    with zipfile.ZipFile(second) as archive:
        package = archive.read("OEBPS/content.opf").replace(b"Fixture 2", b"Second fixture")
    edit_epub(second, {"OEBPS/content.opf": package})

    merged = BytesIO()
    doMerge(merged, [str(first), str(second)], titleopt="Merged fixture", languages=["ru"])

    with zipfile.ZipFile(merged) as archive:
        names = archive.namelist()
        package = etree.fromstring(archive.read("content.opf"))
        ncx = archive.read("toc.ncx").decode()

    assert "1/OEBPS/text/chapter.xhtml" in names
    assert "2/OEBPS/text/chapter.xhtml" in names
    assert package.xpath("string(//*[local-name()='title'][1])") == "Merged fixture"
    assert "Fixture 2" in ncx
    assert "Second fixture" in ncx


@pytest.mark.skipif(not os.environ.get("FBC_INTEGRATION"), reason="requires pinned fbc")
def test_real_fbc_epub2_merges_with_ncx(tmp_path: Path) -> None:
    source = tmp_path / "source.fb2"
    source.write_bytes(Path("tests/fixtures/book.fb2").read_bytes())
    converted = tmp_path / "converted.epub"

    FB2Converter().convert(source, converted)
    with zipfile.ZipFile(converted) as archive:
        opf_name = etree.fromstring(archive.read("META-INF/container.xml")).xpath(
            "string(//*[local-name()='rootfile']/@full-path)"
        )
        opf = etree.fromstring(archive.read(opf_name))
        assert opf.get("version") == "2.0"
        assert opf.xpath("string(//*[local-name()='spine']/@toc)")
        assert any(name.endswith(".ncx") for name in archive.namelist())

    merged = BytesIO()
    doMerge(merged, [str(converted)], titleopt="FB2 fixture", languages=["ru"])
    with zipfile.ZipFile(merged) as archive:
        assert "toc.ncx" in archive.namelist()
        assert "FB2 fixture" in archive.read("toc.ncx").decode()
