from __future__ import annotations

import zipfile

from ebooklib import epub


def test_ebooklib_rebuilds_opf_and_resource_paths(epub_factory, tmp_path) -> None:
    source = epub_factory(3)
    rebuilt = tmp_path / "rebuilt.epub"

    epub.write_epub(rebuilt, epub.read_epub(source))

    with zipfile.ZipFile(source) as original, zipfile.ZipFile(rebuilt) as output:
        assert "OEBPS/content.opf" in original.namelist()
        assert "EPUB/content.opf" in output.namelist()
        assert "OEBPS/toc.ncx" in original.namelist()
        assert "EPUB/toc.ncx" not in output.namelist()
        assert original.read("OEBPS/images/cover.png") == output.read("EPUB/images/cover.png")
