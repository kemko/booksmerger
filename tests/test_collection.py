from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import UUID

from lxml import etree

from bookmerger.collection import build_collection
from bookmerger.epub import read_package, stage_epub

OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
XHTML = "http://www.w3.org/1999/xhtml"
EPUB = "http://www.idpf.org/2007/ops"


def test_builds_navigation_front_matter_and_merged_metadata(epub_factory, tmp_path: Path) -> None:
    directory = tmp_path / "staging"
    books = (
        stage_epub(epub_factory(2), directory, 1),
        stage_epub(epub_factory(3, svg_cover=True), directory, 2),
    )

    package = build_collection(
        "Collection",
        books,
        directory,
        identifier=UUID("12345678-1234-5678-1234-567812345678"),
        published=date(2026, 9, 11),
    )

    root = etree.fromstring(package.read_bytes())
    assert root.xpath("string(//dc:identifier)", namespaces={"dc": DC}) == (
        "urn:uuid:12345678-1234-5678-1234-567812345678"
    )
    assert root.xpath("//dc:language/text()", namespaces={"dc": DC}) == ["ru", "en"]
    assert root.xpath("//dc:subject/text()", namespaces={"dc": DC}) == ["Shared subject"]
    assert root.xpath("//dc:creator/text()", namespaces={"dc": DC}) == ["Author 2", "Author 3"]
    assert root.xpath("//dc:contributor/text()", namespaces={"dc": DC}) == [
        "Translator 2",
        "Translator 3",
    ]
    manifest = root.find(f"{{{OPF}}}manifest")
    assert manifest is not None
    hrefs = {item.get("href") for item in manifest}
    assert "../books/0002/OEBPS/images/cover.svg" in hrefs
    spine = root.find(f"{{{OPF}}}spine")
    assert spine is not None
    assert [item.get("idref") for item in spine][:6] == [
        "title",
        "toc",
        "cover-page-0001",
        "front-0001",
        "book-0001-chapter",
        "book-0001-appendix",
    ]
    assert (directory / "EPUB" / "cover-0002.xhtml").read_bytes().count(b"cover.svg") == 1
    bibliography = (directory / "EPUB" / "bibliography.xhtml").read_text()
    assert "id-2" in bibliography and "Rights 3" in bibliography

    nav = etree.fromstring((directory / "EPUB" / "nav.xhtml").read_bytes())
    toc = nav.xpath("//x:nav[@epub:type='toc']", namespaces={"x": XHTML, "epub": EPUB})
    assert toc[0].xpath(".//x:a/text()", namespaces={"x": XHTML}) == [
        "Fixture 2",
        "Chapter",
        "Note",
        "Fixture 3",
        "Chapter",
        "Note",
    ]
    assert toc[0].xpath(".//x:a/@href", namespaces={"x": XHTML})[1] == (
        "../books/0001/OEBPS/text/chapter.xhtml"
    )
    assert (
        nav.xpath("count(//x:nav[@epub:type='page-list'])", namespaces={"x": XHTML, "epub": EPUB})
        == 1.0
    )
    assert (
        nav.xpath("count(//x:nav[@epub:type='landmarks'])", namespaces={"x": XHTML, "epub": EPUB})
        == 1.0
    )
    assert (directory / "EPUB" / "toc.xhtml").is_file()


def test_source_navigation_is_retained_when_no_toc_tree(epub_factory, tmp_path: Path) -> None:
    source = epub_factory(3)
    package = read_package(source)
    assert package.metadata.contributors[0].role == "aut"
    assert package.cover == "images/cover.png"
