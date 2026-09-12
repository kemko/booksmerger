from __future__ import annotations

import os
import zipfile
from urllib.parse import quote

import httpx
import pytest
from conftest import PNG
from lxml import etree
from test_pipeline import LocalDownloader

from bookmerger.cli import BuildError, Command, assemble, main
from bookmerger.collection import build_collection
from bookmerger.download import Downloader, DownloadError, DownloadLimits
from bookmerger.epub import EpubError, stage_epub, write_epub
from bookmerger.references import ResourceMap, rewrite_css, rewrite_xml
from bookmerger.validate import ValidationError, validate_epub

OPF = "http://www.idpf.org/2007/opf"
XHTML = "http://www.w3.org/1999/xhtml"
EPUB = "http://www.idpf.org/2007/ops"


def contents(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def build(source, directory, **kwargs):
    book = stage_epub(source, directory, 1, **kwargs)
    build_collection("Collection", (book,), directory)
    result = directory.parent / "collection.epub"
    write_epub(directory, result)
    validate_epub(result)
    return book, contents(result)


def test_comments_and_processing_instructions_survive(epub_factory, edit_epub, tmp_path):
    source = epub_factory(3)
    entries = contents(source)
    for name in ("OEBPS/text/chapter.xhtml", "OEBPS/content.opf"):
        entries[name] = (
            entries[name]
            .replace(b"</head>", b"<!-- comment --><?test value?></head>")
            .replace(b"</metadata>", b"<!-- metadata --><?test value?></metadata>")
        )
    edit_epub(source, entries)
    _, output = build(source, tmp_path / "staging")
    assert b"<!-- comment --><?test value?>" in output["books/0001/OEBPS/text/chapter.xhtml"]


@pytest.mark.parametrize("filename", ["глава.xhtml", "chapter space.xhtml", "literal%20.xhtml"])
def test_manifest_uris_are_decoded_once(epub_factory, edit_epub, tmp_path, filename):
    source = epub_factory(3)
    entries = contents(source)
    new_href = "text/../text/" + quote(filename, safe="")
    entries["OEBPS/content.opf"] = entries["OEBPS/content.opf"].replace(
        b"text/chapter.xhtml", new_href.encode()
    )
    entries["OEBPS/text/" + filename] = entries["OEBPS/text/chapter.xhtml"]
    # Existing chapter remains addressable for the fixture's navigation.
    edit_epub(source, entries)
    book, output = build(source, tmp_path / "staging")
    assert book.package.manifest[0].href == "books/0001/OEBPS/text/" + filename
    assert ("books/0001/OEBPS/text/" + filename) in output


def test_bases_are_resolved_for_xml_css_and_unicode_fragments(epub_factory, edit_epub, tmp_path):
    source = epub_factory(3)
    entries = contents(source)
    chapter = entries["OEBPS/text/chapter.xhtml"].replace(b"<head>", b'<head><base href="../"/>')
    chapter = chapter.replace(b"../styles/", b"styles/").replace(b"../images/", b"images/")
    chapter = chapter.replace(b'href="#note"', b'href="text/chapter.xhtml#note"').replace(
        b'href="#back"', b'href="text/chapter.xhtml#back"'
    )
    chapter = chapter.replace(
        b"</body>",
        (
            '<p id="я"/><a href="text/chapter.xhtml#%D1%8F">Unicode</a>'
            '<div xml:base="images/">'
            '<img src="cover.png" style="background:url(cover.png)"/></div></body>'
        ).encode(),
    )
    entries["OEBPS/text/chapter.xhtml"] = chapter
    edit_epub(source, entries)
    _, output = build(source, tmp_path / "staging")
    root = etree.fromstring(output["books/0001/OEBPS/text/chapter.xhtml"])
    assert not root.xpath('//@*[local-name()="base"] | //*[local-name()="base"]/@href')
    assert root.xpath('//*[local-name()="div"]/*/@src') == ["../images/cover.png"]
    assert "../images/cover.png" in root.xpath('//*[local-name()="div"]/*/@style')[0]


def test_css_import_and_nested_urls_are_rewritten():
    mapping = ResourceMap(
        {
            "OPS/a.css": "book/a.css",
            "OPS/image.png": "book/images/image.png",
            "OPS/b.css": "book/styles/b.css",
        }
    )
    result = rewrite_css(
        b'@import "b.css"; @media screen {p{background:image-set(url(image.png) 1x)}}',
        mapping,
        "OPS/a.css",
    )
    assert b'"styles/b.css"' in result
    assert b"images/image.png" in result
    xml = rewrite_xml(
        b'<svg xmlns="http://www.w3.org/2000/svg"><!--x--><?p a?></svg>', mapping, "OPS/a.css"
    )
    assert b"<!--x--><?p a?>" in xml


@pytest.mark.parametrize(
    "algorithm",
    ["http://www.idpf.org/2008/embedding", "http://www.w3.org/2001/04/xmlenc#aes128-cbc"],
)
def test_encrypted_resources_are_rejected(epub_factory, edit_epub, tmp_path, algorithm):
    source = edit_epub(
        epub_factory(3),
        {
            "META-INF/encryption.xml": (
                '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#">'
                f'<EncryptionMethod Algorithm="{algorithm}"/><CipherData>'
                '<CipherReference URI="OEBPS/fonts/reader.woff2"/>'
                "</CipherData></EncryptedData></encryption>"
            ).encode()
        },
    )
    with pytest.raises(EpubError, match="obfuscated"):
        stage_epub(source, tmp_path / "staging", 1)


def test_group_navigation_landmarks_and_series_survive(epub_factory, edit_epub, tmp_path):
    source = epub_factory(3)
    entries = contents(source)
    entries["OEBPS/nav.xhtml"] = (
        entries["OEBPS/nav.xhtml"]
        .replace(b'<a href="text/chapter.xhtml">Chapter</a>', b"<span>Chapter</span>", 1)
        .replace(b'epub:type="bodymatter"', b'epub:type="toc"')
    )
    entries["OEBPS/content.opf"] = entries["OEBPS/content.opf"].replace(
        b"</metadata>",
        b'<meta name="calibre:series" content="Series A"/>'
        b'<meta name="edition" content="Second edition"/></metadata>',
    )
    edit_epub(source, entries)
    _, output = build(source, tmp_path / "staging")
    nav = etree.fromstring(output["EPUB/nav.xhtml"])
    assert nav.xpath(
        '//*[local-name()="li"][*[local-name()="span" and '
        'text()="Chapter"]]/*[local-name()="ol"]//*[local-name()="a" and '
        'text()="Note"]'
    )
    assert nav.xpath(
        '//*[local-name()="nav"][@epub:type="landmarks"]//*[local-name()="a"]/@epub:type',
        namespaces={"epub": EPUB},
    ) == ["toc"]
    for name in ("EPUB/book-0001.xhtml", "EPUB/bibliography.xhtml"):
        assert b"Series A" in output[name] and b"Second edition" in output[name]


def test_source_cover_is_used_once(epub_factory, edit_epub, tmp_path):
    source = epub_factory(3)
    entries = contents(source)
    entries["OEBPS/content.opf"] = (
        entries["OEBPS/content.opf"]
        .replace(
            b"</manifest>",
            b'<item id="coverpage" href="cover.xhtml" '
            b'media-type="application/xhtml+xml"/></manifest>',
        )
        .replace(b"<spine >", b'<spine ><itemref idref="coverpage"/>')
    )
    entries["OEBPS/cover.xhtml"] = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Cover</title>'
        b'</head><body><img src="images/cover.png" alt="Cover"/></body></html>'
    )
    edit_epub(source, entries)
    _, output = build(source, tmp_path / "staging")
    opf = etree.fromstring(output["EPUB/package.opf"])
    ids = opf.xpath('//*[local-name()="itemref"]/@idref')
    assert ids[:4] == ["title", "toc", "book-0001-coverpage", "front-0001"]
    assert ids.count("book-0001-coverpage") == 1
    assert "EPUB/cover-0001.xhtml" not in output
    assert "books/0001/OEBPS/cover.xhtml" in output


def test_package_id_references_and_spread_properties_survive(epub_factory, edit_epub, tmp_path):
    source = epub_factory(3)
    entries = contents(source)
    entries["OEBPS/content.opf"] = (
        entries["OEBPS/content.opf"]
        .replace(b'id="chapter" href=', b'id="chapter" media-overlay="overlay" href=')
        .replace(
            b'<itemref idref="chapter"/>',
            b'<itemref idref="chapter" properties="page-spread-left"/>',
        )
        .replace(
            b"</manifest>",
            b'<item id="overlay" href="overlay.smil" '
            b'media-type="application/smil+xml"/>'
            b'<item id="foreign" href="foreign.bin" '
            b'media-type="application/octet-stream" fallback="chapter"/></manifest>',
        )
        .replace(
            b"</metadata>",
            b'<meta property="media:duration">00:00:01.000</meta>'
            b'<meta property="media:duration" refines="#overlay">00:00:01.000</meta>'
            b"</metadata>",
        )
    )
    entries["OEBPS/overlay.smil"] = (
        b'<smil xmlns="http://www.w3.org/ns/SMIL" version="3.0"><body><par>'
        b'<text src="text/chapter.xhtml#same-id"/>'
        b'<audio src="media/audio.mp3" clipBegin="0s" clipEnd="1s"/></par>'
        b"</body></smil>"
    )
    entries["OEBPS/foreign.bin"] = b"foreign"
    edit_epub(source, entries)
    _, output = build(source, tmp_path / "staging")
    opf = etree.fromstring(output["EPUB/package.opf"])
    assert opf.xpath('//*[@id="book-0001-chapter"]/@media-overlay') == ["book-0001-overlay"]
    assert opf.xpath('//*[@id="book-0001-foreign"]/@fallback') == ["book-0001-chapter"]
    assert opf.xpath('//*[@idref="book-0001-chapter"]/@properties') == ["page-spread-left"]
    assert opf.xpath('//*[@refines="#book-0001-overlay"]/text()') == ["00:00:01.000"]
    assert opf.xpath('//*[@property="media:duration" and not(@refines)]/text()') == ["1s"]


def test_remote_resources_are_downloaded_recursively_and_hyperlinks_retained(
    epub_factory, edit_epub, tmp_path
):
    source = epub_factory(3)
    entries = contents(source)
    entries["OEBPS/text/chapter.xhtml"] = (
        entries["OEBPS/text/chapter.xhtml"]
        .replace(b"</head>", b'<link rel="stylesheet" href="https://assets.test/book.css"/></head>')
        .replace(
            b"</body>",
            b'<img src="https://assets.test/one.png"/>'
            b'<a href="https://example.test/page">External</a></body>',
        )
    )
    entries["OEBPS/content.opf"] = entries["OEBPS/content.opf"].replace(
        b"</manifest>",
        b'<item id="remote" href="https://assets.test/one.png" media-type="image/png"/></manifest>',
    )
    edit_epub(source, entries)
    requests = []
    responses = {
        "/book.css": b'@import "more.css";p{background:url(one.png)}',
        "/more.css": b"p{color:blue}",
        "/one.png": PNG,
    }

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=responses[request.url.path])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _, output = build(source, tmp_path / "staging", downloader=Downloader(client=client))
    assert len(requests) == 3
    assert set(requests) == {"https://assets.test" + path for path in responses}
    chapter = output["books/0001/OEBPS/text/chapter.xhtml"]
    assert b"https://assets.test" not in chapter
    assert b"https://example.test/page" in chapter
    assert PNG in output.values()
    opf = etree.fromstring(output["EPUB/package.opf"])
    hrefs = opf.xpath('//*[local-name()="item"]/@href')
    assert len(hrefs) == len(set(hrefs))


def test_remote_resource_limit_and_failure_stop_build(epub_factory, edit_epub, tmp_path):
    source = epub_factory(3)
    chapter = contents(source)["OEBPS/text/chapter.xhtml"].replace(
        b"</body>", b'<img src="https://assets.test/one.png"/></body>'
    )
    edit_epub(source, {"OEBPS/text/chapter.xhtml": chapter})
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=PNG))
    ) as client:
        with pytest.raises(DownloadError, match="allowed size"):
            stage_epub(
                source,
                tmp_path / "staging",
                1,
                downloader=Downloader(client=client, limits=DownloadLimits(max_download_bytes=1)),
            )


def test_cli_handles_real_validation_error_and_cleans_output(
    epub_factory, monkeypatch, tmp_path, capsys
):
    source = epub_factory(3)
    output = tmp_path / "collection.epub"
    output.write_bytes(b"old")
    monkeypatch.setattr("bookmerger.cli.Downloader", lambda: LocalDownloader((source,)))

    def fail(*args):
        raise ValidationError("missing anchor")

    monkeypatch.setattr("bookmerger.cli.validate_epub", fail)
    assert (
        main(
            [
                "--title",
                "Collection",
                "--output",
                str(output),
                "--overwrite",
                "https://example.test/book",
            ]
        )
        == 1
    )
    assert output.read_bytes() == b"old"
    assert "bookmerger: missing anchor" in capsys.readouterr().err
    assert not list(tmp_path.glob(".collection-*"))


def test_atomic_publication_never_clobbers_a_concurrent_output(epub_factory, monkeypatch, tmp_path):
    source = epub_factory(3)
    output = tmp_path / "collection.epub"
    link = os.link

    def concurrent_link(src, dst):
        output.write_bytes(b"concurrent result")
        return link(src, dst)

    monkeypatch.setattr("bookmerger.cli.os.link", concurrent_link)
    with pytest.raises(BuildError, match="already exists"):
        assemble(
            Command("Collection", output, ("source",), False), downloader=LocalDownloader((source,))
        )
    assert output.read_bytes() == b"concurrent result"
    assert not list(tmp_path.glob(".collection-*"))


def test_remote_css_uses_redirect_base_and_response_mime(epub_factory, edit_epub, tmp_path):
    source = epub_factory(3)
    chapter = contents(source)["OEBPS/text/chapter.xhtml"].replace(
        b"</head>", b'<link rel="stylesheet" href="https://assets.test/style"/></head>'
    )
    edit_epub(source, {"OEBPS/text/chapter.xhtml": chapter})
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/style":
            return httpx.Response(302, headers={"location": "/files/theme"})
        if request.url.path == "/files/theme":
            return httpx.Response(
                200, headers={"content-type": "text/css"}, content=b"p{background:url(pic)}"
            )
        assert request.url.path == "/files/pic"
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        _, output = build(source, tmp_path / "staging", downloader=Downloader(client=client))
    assert requested == ["/style", "/files/theme", "/files/pic"]
    assert any(name.endswith(".css") and b"0001.png" in data for name, data in output.items())


def test_css_rewriting_preserves_declared_encoding():
    mapping = ResourceMap({"OPS/a.css": "book/a.css", "OPS/image.png": "book/images/image.png"})
    original = '@charset "windows-1251";p:before{content:"Привет";background:url(image.png)}'
    result = rewrite_css(original.encode("cp1251"), mapping, "OPS/a.css")
    assert result.decode("cp1251") == original.replace("url(image.png)", 'url("images/image.png")')
