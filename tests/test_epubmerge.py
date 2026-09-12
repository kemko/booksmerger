from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

from epubmerge.epubmerge import doMerge
from lxml import etree


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
