from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import UUID

from lxml import etree

from bookmerger.collection import build_collection, generated_title
from bookmerger.epub import BookMetadata, Contributor, EpubPackage, StagedBook, stage_epub

OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
XHTML = "http://www.w3.org/1999/xhtml"
EPUB = "http://www.idpf.org/2007/ops"


def test_generated_title_uses_unique_authors_and_source_titles() -> None:
    def book(number: int, title: str, contributors: tuple[Contributor, ...]) -> StagedBook:
        metadata = BookMetadata(title, contributors, (), (), None, (), (), ())
        return StagedBook(number, "", EpubPackage("", (), (), (), metadata, None), {})

    books = (
        book(1, " Work ", (Contributor(" Author ", "aut"), Contributor("Editor", "edt"))),
        book(2, "work", (Contributor("author", "AUT"), Contributor("Translator", "trl"))),
        book(3, "Third", (Contributor("Second", "aut"),)),
        book(4, "Fourth", (Contributor("Third", "aut"),)),
        book(5, "Untitled", ()),
    )

    assert generated_title(books) == "Сборник — Author, Second и др. — Work, Third и др."


def test_generated_title_without_meaningful_metadata_is_collection() -> None:
    metadata = BookMetadata("Untitled", (), (), (), None, (), (), (), title_is_fallback=True)
    book = StagedBook(1, "", EpubPackage("", (), (), (), metadata, None), {})

    assert generated_title((book,)) == "Сборник"


def test_generated_title_preserves_explicit_untitled():
    from bookmerger.epub import _metadata

    for element, expected in [
        ("<dc:title>Untitled</dc:title>", "Сборник — Untitled"),
        ("", "Сборник"),
    ]:
        root = etree.fromstring(
            f'<package xmlns="{OPF}"><metadata xmlns:dc="{DC}">{element}</metadata></package>'
        )
        metadata = _metadata(root)
        book = StagedBook(1, "", EpubPackage("", (), (), (), metadata, None), {})
        assert generated_title((book,)) == expected


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
    links = toc[0].xpath(".//x:a/text()", namespaces={"x": XHTML})
    assert links[:3] == [
        "Fixture 2",
        "Chapter",
        "Note",
    ]
    assert "Supplementary content" in links and "Original contents" in links
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
    assert "Сборник," in (directory / "EPUB" / "title.xhtml").read_text()


def test_source_navigation_is_retained_when_no_toc_tree(
    epub_factory, edit_epub, tmp_path: Path
) -> None:
    source = edit_epub(
        epub_factory(3),
        {
            "OEBPS/nav.xhtml": (
                b'<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                b"<title>Original contents</title></head>"
                b"<body><p>No TOC tree</p></body></html>"
            )
        },
    )
    directory = tmp_path / "staging"
    book = stage_epub(source, directory, 1)
    build_collection("Collection", (book,), directory)
    nav = etree.parse(directory / "EPUB/nav.xhtml")
    assert nav.xpath("//*[local-name()='a' and text()='Fixture 3']/@href") == ["book-0001.xhtml"]
    assert nav.xpath("//*[local-name()='a' and text()='Original contents']/@href") == [
        "../books/0001/OEBPS/nav.xhtml"
    ]
    assert b"No TOC tree" in (directory / "books/0001/OEBPS/nav.xhtml").read_bytes()
