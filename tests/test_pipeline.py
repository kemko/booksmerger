from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from bookmerger.cli import BuildError, Command, assemble
from bookmerger.download import DownloadedSource
from bookmerger.validate import ValidationError


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
        "bookmerger.cli.validate_epub", lambda *_: (_ for _ in ()).throw(ValidationError("bad"))
    )
    with pytest.raises(BuildError, match="bad"):
        assemble(command, downloader=LocalDownloader((epub_factory(3),)))

    assert output.read_bytes() == b"old result"
    assert not list(tmp_path.glob(".collection-*.epub"))


@pytest.mark.skipif(not os.environ.get("FBC_INTEGRATION"), reason="requires pinned fbc")
def test_mixed_fb2_and_epub_uses_pinned_converter(epub_factory, rich_fb2, tmp_path: Path) -> None:
    output = tmp_path / "collection.epub"
    command = Command("Collection", output, ("fb2", "epub2", "epub3"), False)
    sources = (
        rich_fb2,
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

    from lxml import etree

    from bookmerger.converter import fb2_images
    from bookmerger.references import resolve_uri
    from bookmerger.validate import validate_epub

    validate_epub(output)
    with zipfile.ZipFile(output) as archive:
        resources = {
            name: archive.read(name)
            for name in archive.namelist()
            if name.startswith("books/0001/")
        }
    for original in fb2_images(rich_fb2):
        assert original.data in resources.values(), original.id
    roots = {
        name: etree.fromstring(data)
        for name, data in resources.items()
        if name.endswith((".html", ".xhtml"))
    }
    assert any("Annotation" in "".join(root.itertext()) for root in roots.values())
    toc = etree.fromstring(nav)
    assert toc.xpath(
        "//*[local-name()='li'][*[local-name()='a' and "
        "text()='Chapter']]/*[local-name()='ol']//*[local-name()='a' and "
        "text()='Nested chapter']"
    )
    assert [
        link
        for link in toc.xpath(
            "//*[local-name()='nav'][@epub:type='toc']/*[local-name()='ol']/*[local"
            "-name()='li']/*[local-name()='a']/text()",
            namespaces={"epub": "http://www.idpf.org/2007/ops"},
        )
    ] == ["FB2 fixture", "Fixture 2", "Fixture 3"]
    targets = []
    for name, root in roots.items():
        for link in root.xpath("//*[local-name()='a'][@class='link-internal']"):
            if (
                "return" in "".join(link.itertext()).lower()
                or "".join(link.itertext()).strip() == "1"
            ):
                target = resolve_uri(name, link.get("href"))
                assert target and target[0] in roots
                assert roots[target[0]].xpath("//*[@id=$id]", id=target[2])
                targets.append(target)
    assert len(targets) >= 2

    original_images = {image.id: image.data for image in fb2_images(rich_fb2)}
    source_root = etree.parse(rich_fb2)
    expected = [
        original_images[uri.removeprefix("#")]
        for uri in source_root.xpath(
            "//*[local-name()='body']//*[local-name()='image']/@*[local-name()='href']"
        )
    ]
    actual = []
    for name, root in roots.items():
        if name.endswith(("cover.xhtml", "nav.xhtml")):
            continue
        for uri in root.xpath(
            "//*[local-name()='img']/@src | //*[local-name()='image']/@*[local-name()='href']"
        ):
            target = resolve_uri(name, uri)
            actual.append(resources[target[0]])
    assert actual == expected

    if os.environ.get("EPUBCHECK"):
        import subprocess

        checked = subprocess.run(
            ["java", "-jar", os.environ["EPUBCHECK"], str(output)], capture_output=True, text=True
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
