from __future__ import annotations

from pathlib import Path

from bookmerger.epub import read_package, validate_merge_input


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
