from __future__ import annotations

import os
import zipfile
from pathlib import Path

import httpx
import pytest
from lxml import etree

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


def mock_downloader(handler: httpx.MockTransport, cache_directory: Path) -> Downloader:
    return Downloader(
        client=httpx.Client(transport=handler), cache_directory=cache_directory, retries=0
    )


def test_pipeline_publishes_epubmerge_result_atomically(
    epub_factory, monkeypatch, tmp_path: Path
) -> None:
    output = tmp_path / "collection.epub"
    monkeypatch.chdir(tmp_path)

    result = assemble(
        Command("collection", ("one", "two"), False),
        downloader=LocalDownloader((epub_factory(2), epub_factory(2))),
    )
    assert result == output
    with zipfile.ZipFile(output) as archive:
        assert {"1/OEBPS/images/cover.png", "2/OEBPS/images/cover.png", "toc.ncx"} <= set(
            archive.namelist()
        )
        assert archive.read("1/OEBPS/styles/book.css") == b"p { color: navy; }"
        package = etree.fromstring(archive.read("content.opf"))
        assert package.xpath("string(//*[local-name()='title'][1])") == "collection"

    with pytest.raises(BuildError, match="already exists"):
        assemble(
            Command("collection", ("one",), False), downloader=LocalDownloader((epub_factory(2),))
        )


def test_pipeline_generates_title_and_preserves_source_order(
    epub_factory, monkeypatch, tmp_path: Path
) -> None:
    first, second = epub_factory(2), tmp_path / "second.epub"
    second.write_bytes(first.read_bytes())
    with zipfile.ZipFile(second) as archive:
        opf = archive.read("OEBPS/content.opf").replace(b"Fixture 2", b"Second fixture")
    with zipfile.ZipFile(second, "w") as archive:
        with zipfile.ZipFile(first) as original:
            for name in original.namelist():
                archive.writestr(name, opf if name == "OEBPS/content.opf" else original.read(name))
    monkeypatch.chdir(tmp_path)

    output = assemble(
        Command(None, ("second", "first", "second"), False),
        downloader=LocalDownloader((second, first, second)),
    )

    assert output.name == "Сборник — Author 2 — Second fixture, Fixture 2.epub"
    with zipfile.ZipFile(output) as archive:
        toc = archive.read("toc.ncx").decode()
    assert toc.index("Second fixture") < toc.index("Fixture 2") < toc.rindex("Second fixture")


def test_pipeline_keeps_existing_output_when_validation_fails(
    epub_factory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "collection.epub"
    output.write_bytes(b"old result")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "bookmerger.cli.validate_epub", lambda _: (_ for _ in ()).throw(ValidationError("bad"))
    )

    with pytest.raises(BuildError, match="bad"):
        assemble(
            Command("collection", ("one",), True), downloader=LocalDownloader((epub_factory(2),))
        )

    assert output.read_bytes() == b"old result"
    assert not list(tmp_path.glob(".collection-*.epub"))


@pytest.mark.parametrize("replace_fails", [False, True])
def test_pipeline_overwrite_publication(
    epub_factory, monkeypatch, tmp_path: Path, replace_fails: bool
) -> None:
    output = tmp_path / "collection.epub"
    output.write_bytes(b"old result")
    monkeypatch.chdir(tmp_path)
    command = Command("collection", ("one",), True)
    downloader = LocalDownloader((epub_factory(2),))

    if replace_fails:

        def fail_replace(source: Path, target: Path) -> None:
            assert source.is_file()
            assert target == output
            raise OSError("replace failed")

        monkeypatch.setattr("bookmerger.cli.os.replace", fail_replace)
        with pytest.raises(BuildError, match="replace failed"):
            assemble(command, downloader=downloader)
        assert output.read_bytes() == b"old result"
    else:
        assert assemble(command, downloader=downloader) == output
        with zipfile.ZipFile(output) as archive:
            assert "1/OEBPS/text/chapter.xhtml" in archive.namelist()
    assert not list(tmp_path.glob(".collection-*.epub"))


def test_pipeline_keeps_existing_output_when_download_or_merge_fails(
    epub_factory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "collection.epub"
    output.write_bytes(b"old result")
    monkeypatch.chdir(tmp_path)

    class FailingDownloader:
        def download_all(self, *_: object) -> tuple[DownloadedSource, ...]:
            raise RuntimeError("download failed")

    with pytest.raises(BuildError, match="download failed"):
        assemble(Command("collection", ("one",), True), downloader=FailingDownloader())  # type: ignore[arg-type]

    monkeypatch.setattr(
        "bookmerger.cli.merge_epubs", lambda *_: (_ for _ in ()).throw(RuntimeError("merge failed"))
    )
    with pytest.raises(BuildError, match="merge failed"):
        assemble(
            Command("collection", ("one",), True), downloader=LocalDownloader((epub_factory(2),))
        )

    assert output.read_bytes() == b"old result"
    assert not list(tmp_path.glob(".collection-*.epub"))


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
        assemble(Command("result", ("one",), False), downloader=LocalDownloader((epub_factory(2),)))

    assert output.read_bytes() == b"racing result"
    assert not list(tmp_path.glob(".result-*.epub"))


def test_pipeline_reuses_cached_sources_without_network(
    epub_factory, monkeypatch, tmp_path: Path
) -> None:
    data = epub_factory(2).read_bytes()
    requested: list[str] = []

    def first_handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, content=data, request=request)

    cache = tmp_path / "cache"
    urls = ("https://example.test/one", "https://example.test/two")
    first_directory, second_directory = tmp_path / "first", tmp_path / "second"
    first_directory.mkdir()
    second_directory.mkdir()
    monkeypatch.chdir(first_directory)
    assemble(
        Command(None, urls, False),
        downloader=mock_downloader(httpx.MockTransport(first_handler), cache),
    )
    monkeypatch.chdir(second_directory)
    offline = httpx.MockTransport(lambda request: pytest.fail(f"network used: {request.url}"))
    result = assemble(Command(None, urls, False), downloader=mock_downloader(offline, cache))

    assert requested == ["/one", "/two"]
    assert result.is_file()


def test_pipeline_keeps_existing_output_when_conversion_fails(monkeypatch, tmp_path: Path) -> None:
    source, output = tmp_path / "source.fb2", tmp_path / "collection.epub"
    source.write_bytes(b"source")
    output.write_bytes(b"old result")
    monkeypatch.chdir(tmp_path)

    class FailingConverter:
        def convert(self, _: Path, target: Path) -> None:
            target.parent.mkdir(parents=True)
            target.write_bytes(b"partial EPUB")
            raise RuntimeError("conversion failed")

    class FB2Downloader:
        def download_all(self, urls: tuple[str, ...], _: Path) -> tuple[DownloadedSource, ...]:
            return (DownloadedSource(urls[0], source, "fb2"),)

    with pytest.raises(BuildError, match="conversion failed"):
        assemble(  # type: ignore[arg-type]
            Command("collection", ("one",), True),
            downloader=FB2Downloader(),
            converter=FailingConverter(),
        )

    assert output.read_bytes() == b"old result"
    assert not list(tmp_path.glob(".collection-*.epub"))
