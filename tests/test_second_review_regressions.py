from __future__ import annotations

import os
import subprocess

import httpx
import pytest
from conftest import PNG
from lxml import etree
from test_pipeline import LocalDownloader
from test_review_regressions import build, contents

from bookmerger.cli import BuildError, Command, assemble
from bookmerger.download import Downloader
from bookmerger.epub import EpubError, stage_epub, write_epub
from bookmerger.validate import ValidationError, validate_epub


@pytest.mark.parametrize("filename", ["chapter.htm", "chapter", "chapter.css"])
def test_xhtml_processing_uses_manifest_type(epub_factory, edit_epub, tmp_path, filename):
    source = epub_factory(3)
    entries = contents(source)
    entries = {
        name.replace("chapter.xhtml", filename): (
            data.replace(b"chapter.xhtml", filename.encode())
            if name.endswith((".opf", ".ncx", ".xhtml"))
            else data
        )
        for name, data in entries.items()
    }
    document = "OEBPS/text/" + filename
    entries[document] = entries[document].replace(
        b"</body>", b'<img src="https://assets.test/image" alt="remote"/></body>'
    )
    edit_epub(source, entries)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=PNG))
    ) as client:
        _, output = build(source, tmp_path / "staging", downloader=Downloader(client=client))
    assert b"https://assets.test" not in output["books/0001/" + document]
    result = tmp_path / "collection.epub"
    (tmp_path / "staging" / "books/0001" / document).write_bytes(
        output["books/0001/" + document].replace(b'href="#note"', b'href="#missing"')
    )
    write_epub(tmp_path / "staging", result)
    with pytest.raises(ValidationError, match="missing anchor"):
        validate_epub(result)


def test_extensionless_css_embeds_remote_dependencies(epub_factory, edit_epub, tmp_path):
    source = epub_factory(3)
    entries = contents(source)
    entries["OEBPS/content.opf"] = entries["OEBPS/content.opf"].replace(b"book.css", b"book")
    entries["OEBPS/text/chapter.xhtml"] = entries["OEBPS/text/chapter.xhtml"].replace(
        b"book.css", b"book"
    )
    entries["OEBPS/styles/book"] = b'p{background:url("https://assets.test/image")}'
    edit_epub(source, entries)
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, content=PNG)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _, output = build(source, tmp_path / "staging", downloader=Downloader(client=client))
    assert requested == ["https://assets.test/image"]
    assert b"https://" not in output["books/0001/OEBPS/styles/book"]


def test_primary_nav_precedes_compatibility_ncx(epub_factory, edit_epub, tmp_path):
    source = epub_factory(3)
    entries = contents(source)
    entries["OEBPS/content.opf"] = entries["OEBPS/content.opf"].replace(
        b'<item id="nav"',
        b'<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/><item id="nav"',
    )
    entries["OEBPS/nav.xhtml"] = entries["OEBPS/nav.xhtml"].replace(
        b">Note</a></li>",
        b'>Note</a><ol><li><a href="text/chapter.xhtml#back">Deep NAV entry</a></li></ol></li>',
    )
    edit_epub(source, entries)
    _, output = build(source, tmp_path / "staging")
    for name in ("EPUB/nav.xhtml", "EPUB/toc.xhtml"):
        root = etree.fromstring(output[name])
        assert root.xpath(
            '//*[local-name()="li"][*[local-name()="a" and text()="Note"]]'
            '/*[local-name()="ol"]//*[local-name()="a" and text()="Deep NAV entry"]'
        )


def test_rendition_defaults_and_rtl_are_preserved(epub_factory, edit_epub, tmp_path):
    source = epub_factory(3)
    entries = contents(source)
    entries["OEBPS/content.opf"] = (
        entries["OEBPS/content.opf"]
        .replace(b"<spine >", b'<spine page-progression-direction="rtl">')
        .replace(
            b"</metadata>",
            b'<meta property="rendition:layout">pre-paginated</meta>'
            b'<meta property="rendition:orientation">landscape</meta>'
            b'<meta property="rendition:flow">paginated</meta>'
            b'<meta property="rendition:spread">none</meta></metadata>',
        )
        .replace(
            b'<itemref idref="appendix"',
            b'<itemref properties="rendition:layout-reflowable" idref="appendix"',
        )
    )
    entries["OEBPS/text/chapter.xhtml"] = entries["OEBPS/text/chapter.xhtml"].replace(
        b"</head>", b'<meta name="viewport" content="width=600,height=800"/></head>'
    )
    edit_epub(source, entries)
    _, output = build(source, tmp_path / "staging")
    root = etree.fromstring(output["EPUB/package.opf"])
    assert root.xpath('//*[local-name()="spine"]/@page-progression-direction') == ["rtl"]
    assert set(root.xpath('//*[@idref="book-0001-chapter"]/@properties')[0].split()) == {
        "rendition:layout-pre-paginated",
        "rendition:orientation-landscape",
        "rendition:flow-paginated",
        "rendition:spread-none",
    }
    appendix = root.xpath('//*[@idref="book-0001-appendix"]/@properties')[0].split()
    assert "rendition:layout-reflowable" in appendix
    assert "rendition:layout-pre-paginated" not in appendix
    assert not root.xpath('//*[@idref="title"]/@properties')
    if os.environ.get("EPUBCHECK"):
        checked = subprocess.run(
            ["java", "-jar", os.environ["EPUBCHECK"], str(tmp_path / "collection.epub")],
            capture_output=True,
            text=True,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr


def test_unsupported_rendition_is_rejected(epub_factory, edit_epub, tmp_path):
    source = epub_factory(3)
    opf = contents(source)["OEBPS/content.opf"].replace(
        b"</metadata>",
        b'<meta property="rendition:viewport">width=600,height=800</meta></metadata>',
    )
    edit_epub(source, {"OEBPS/content.opf": opf})
    with pytest.raises(EpubError, match="unsupported rendition setting: rendition:viewport"):
        stage_epub(source, tmp_path / "staging", 1)


def test_conflicting_page_progression_preserves_output(epub_factory, edit_epub, tmp_path):
    sources = [epub_factory(2), epub_factory(3)]
    for source, direction in zip(sources, ("rtl", "ltr"), strict=True):
        opf = contents(source)["OEBPS/content.opf"]
        edit_epub(
            source,
            {
                "OEBPS/content.opf": opf.replace(
                    b"<spine ", f'<spine page-progression-direction="{direction}" '.encode()
                )
            },
        )
    output = tmp_path / "collection.epub"
    output.write_bytes(b"existing output")
    with pytest.raises(BuildError, match="conflicting page progression"):
        assemble(
            Command("Collection", output, ("one", "two"), True), downloader=LocalDownloader(sources)
        )
    assert output.read_bytes() == b"existing output"
    assert not list(tmp_path.glob(".collection-*"))
