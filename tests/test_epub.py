from __future__ import annotations

import hashlib
from pathlib import Path

from bookmerger.epub import read_package, stage_epub, validate_merge_input


def test_reads_epub2_and_epub3_topology(epub_factory, tmp_path: Path) -> None:
    epub2 = read_package(epub_factory(2))
    epub3 = read_package(epub_factory(3))

    assert [item.idref for item in epub2.spine] == ["chapter", "appendix"]
    assert epub2.spine[0].linear
    assert not epub2.spine[1].linear
    assert epub2.navigation == ("toc.ncx",)
    assert epub3.navigation == ("nav.xhtml",)


def test_merge_input_reads_metadata_without_unpacking(epub_factory) -> None:
    package = validate_merge_input(epub_factory(2))

    assert package.metadata.title == "Fixture 2"
    assert [(person.name, person.role) for person in package.metadata.contributors] == [
        ("Author 2", "aut"),
        ("Translator 2", "trl"),
    ]
    assert package.metadata.languages == ("ru",)


def test_stages_books_without_collisions_or_resource_changes(epub_factory, tmp_path: Path) -> None:
    first = epub_factory(2)
    second = epub_factory(3)
    output = tmp_path / "staging"
    staged_one = stage_epub(first, output, 1)
    staged_two = stage_epub(second, output, 2)

    assert all(item.id.startswith("book-0001-") for item in staged_one.package.manifest)
    assert all(item.id.startswith("book-0002-") for item in staged_two.package.manifest)
    assert staged_one.package.spine[0].linear
    assert not staged_one.package.spine[1].linear
    one_cover = output / "books/0001/OEBPS/images/cover.png"
    two_cover = output / "books/0002/OEBPS/images/cover.png"
    assert (
        hashlib.sha256(one_cover.read_bytes()).digest()
        == hashlib.sha256(two_cover.read_bytes()).digest()
    )
    diagram = output / "books/0001/OEBPS/images/diagram.svg"
    assert b'<text id="same-id">SVG</text>' in diagram.read_bytes()
    assert b'id="same-id"' in (output / "books/0002/OEBPS/text/chapter.xhtml").read_bytes()
    assert (output / "books/0001/OEBPS/fonts/reader.woff2").read_bytes() == b"ordinary-font-bytes"
    assert (output / "books/0002/OEBPS/media/audio.mp3").read_bytes() == b"media-bytes"
