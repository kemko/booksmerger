from __future__ import annotations

import os
import zipfile
from pathlib import Path

import httpx
import pytest

from bookmerger.cli import BuildError, Command, assemble
from bookmerger.download import DownloadedSource, Downloader
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


def mock_downloader(
    handler: httpx.MockTransport, cache_directory: Path, *, retries: int = 0
) -> Downloader:
    return Downloader(
        client=httpx.Client(transport=handler), cache_directory=cache_directory, retries=retries
    )


def test_pipeline_publishes_valid_epub_atomically(
    epub_factory, monkeypatch, tmp_path: Path
) -> None:
    output = tmp_path / "collection.epub"
    monkeypatch.chdir(tmp_path)
    command = Command("collection", ("one", "two"), False)

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
    monkeypatch.chdir(tmp_path)
    command = Command("collection", ("one",), True)

    monkeypatch.setattr(
        "bookmerger.cli.validate_epub", lambda *_: (_ for _ in ()).throw(ValidationError("bad"))
    )
    with pytest.raises(BuildError, match="bad"):
        assemble(command, downloader=LocalDownloader((epub_factory(3),)))

    assert output.read_bytes() == b"old result"
    assert not list(tmp_path.glob(".collection-*.epub"))


def test_pipeline_generates_title_and_output_after_staging(
    epub_factory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    command = Command(None, ("one", "two"), False)

    output = assemble(command, downloader=LocalDownloader((epub_factory(2), epub_factory(3))))

    assert output == tmp_path / "Сборник — Author 2, Author 3 — Fixture 2, Fixture 3.epub"
    with zipfile.ZipFile(output) as archive:
        package = archive.read("EPUB/package.opf").decode()
        title_page = archive.read("EPUB/title.xhtml").decode()
    assert "Сборник — Author 2, Author 3 — Fixture 2, Fixture 3" in package
    assert "Сборник — Author 2, Author 3 — Fixture 2, Fixture 3" in title_page


def test_pipeline_uses_supplied_title_for_output_and_epub(
    epub_factory, monkeypatch, tmp_path: Path
) -> None:
    title = "Collection title"
    output = tmp_path / "Collection title.epub"
    monkeypatch.chdir(tmp_path)

    assemble(Command(title, ("one",), False), downloader=LocalDownloader((epub_factory(3),)))

    with zipfile.ZipFile(output) as archive:
        assert title in archive.read("EPUB/package.opf").decode()
        assert title in archive.read("EPUB/title.xhtml").decode()


def test_pipeline_preserves_racing_output(
    epub_factory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "result.epub"
    monkeypatch.chdir(tmp_path)
    original_link = os.link

    def racing_link(source: Path, target: Path) -> None:
        output.write_bytes(b"racing result")
        original_link(source, target)

    monkeypatch.setattr("bookmerger.cli.os.link", racing_link)

    with pytest.raises(BuildError, match="already exists"):
        assemble(
            Command("result", ("one",), False),
            downloader=LocalDownloader((epub_factory(3),)),
        )

    assert output.read_bytes() == b"racing result"
    assert not list(tmp_path.glob(".result-*.epub"))


def test_pipeline_reuses_cached_sources_without_title_or_output(
    epub_factory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    books = {
        "/one": epub_factory(2).read_bytes(),
        "/two": epub_factory(3).read_bytes(),
    }
    requested: list[str] = []

    def first_handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, content=books[request.url.path], request=request)

    cache = tmp_path / "cache"
    urls = ("https://example.test/one", "https://example.test/two")
    first_directory = tmp_path / "first"
    first_directory.mkdir()
    monkeypatch.chdir(first_directory)
    first = assemble(
        Command(None, urls, False),
        downloader=mock_downloader(httpx.MockTransport(first_handler), cache),
    )

    expected_name = "Сборник — Author 2, Author 3 — Fixture 2, Fixture 3.epub"
    assert first.name == expected_name
    assert requested == ["/one", "/two"]

    second_directory = tmp_path / "second"
    second_directory.mkdir()
    monkeypatch.chdir(second_directory)
    second = assemble(
        Command(None, urls, False),
        downloader=mock_downloader(
            httpx.MockTransport(lambda request: pytest.fail(f"network used: {request.url}")), cache
        ),
    )

    assert second.name == expected_name
    assert second.is_file()


def test_pipeline_retries_only_failed_source_and_preserves_output(
    epub_factory, monkeypatch, tmp_path: Path
) -> None:
    books = {"/one": epub_factory(2).read_bytes(), "/two": epub_factory(3).read_bytes()}
    cache = tmp_path / "cache"
    output = tmp_path / "collection.epub"
    output.write_bytes(b"old result")
    monkeypatch.chdir(tmp_path)
    urls = ("https://example.test/one", "https://example.test/two")

    def failing_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/one":
            return httpx.Response(200, content=books["/one"], request=request)
        return httpx.Response(404, content=b"missing", request=request)

    with pytest.raises(BuildError, match=r"book 2/2"):
        assemble(
            Command("collection", urls, True),
            downloader=mock_downloader(httpx.MockTransport(failing_handler), cache),
        )

    assert output.read_bytes() == b"old result"
    requested: list[str] = []

    def retry_handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        assert request.url.path == "/two"
        return httpx.Response(200, content=books["/two"], request=request)

    assemble(
        Command("collection", urls, True),
        downloader=mock_downloader(httpx.MockTransport(retry_handler), cache),
    )

    assert requested == ["/two"]
    with zipfile.ZipFile(output) as archive:
        navigation = archive.read("EPUB/nav.xhtml").decode()
    assert navigation.index("Fixture 2") < navigation.index("Fixture 3")
    assert not list(tmp_path.glob(".collection-*.epub"))


def test_pipeline_keeps_existing_output_when_conversion_fails(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source.fb2"
    source.write_bytes(b"source")
    output = tmp_path / "collection.epub"
    output.write_bytes(b"old result")
    monkeypatch.chdir(tmp_path)
    converted: list[Path] = []

    class FailingConverter:
        def convert(self, _: Path, target: Path) -> None:
            converted.append(target)
            target.parent.mkdir(parents=True)
            target.write_bytes(b"partial EPUB")
            raise RuntimeError("conversion failed")

    class FB2Downloader:
        def download_all(self, urls: tuple[str, ...], _: Path) -> tuple[DownloadedSource, ...]:
            return (DownloadedSource(urls[0], source, "fb2"),)

    with pytest.raises(BuildError, match="conversion failed"):
        assemble(
            Command("collection", ("one",), True),
            downloader=FB2Downloader(),  # type: ignore[arg-type]
            converter=FailingConverter(),  # type: ignore[arg-type]
        )

    assert output.read_bytes() == b"old result"
    assert converted and not converted[0].exists()
    assert not list(tmp_path.glob(".collection-*.epub"))


@pytest.mark.skipif(not os.environ.get("FBC_INTEGRATION"), reason="requires pinned fbc")
def test_mixed_fb2_and_epub_uses_pinned_converter(
    epub_factory, rich_fb2, monkeypatch, tmp_path: Path
) -> None:
    output = tmp_path / "collection.epub"
    monkeypatch.chdir(tmp_path)
    command = Command("collection", ("fb2", "epub2", "epub3"), False)
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
    assert b"\xd0\xa1\xd0\xb1\xd0\xbe\xd1\x80\xd0\xbd\xd0\xb8\xd0\xba" in package
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
