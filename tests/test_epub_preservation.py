from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from lxml import etree
from test_pipeline import LocalDownloader

from bookmerger.cli import Command, assemble
from bookmerger.references import resolve_uri


@pytest.mark.parametrize("version", [2, 3])
def test_collection_preserves_source_documents_and_resources(
    epub_factory, tmp_path: Path, version: int
) -> None:
    source = epub_factory(version)
    output = tmp_path / "collection.epub"
    assemble(
        Command("Collection", output, ("source",), False), downloader=LocalDownloader((source,))
    )
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(output) as collection:
        for name in original.namelist():
            if name.endswith((".png", ".svg", ".css", ".woff2", ".mp3")):
                assert collection.read("books/0001/" + name) == original.read(name)
        before = etree.fromstring(original.read("OEBPS/text/chapter.xhtml"))
        after = etree.fromstring(collection.read("books/0001/OEBPS/text/chapter.xhtml"))
        assert list(before.itertext()) == list(after.itertext())
        assert [node.tag for node in before.iter()] == [node.tag for node in after.iter()]
        assert before.xpath("//@id") == after.xpath("//@id")
        for old, new in zip(before.xpath("//@href"), after.xpath("//@href"), strict=True):
            old_target = resolve_uri("OEBPS/text/chapter.xhtml", old)
            new_target = resolve_uri("books/0001/OEBPS/text/chapter.xhtml", new)
            assert new_target == ("books/0001/" + old_target[0], *old_target[1:])
