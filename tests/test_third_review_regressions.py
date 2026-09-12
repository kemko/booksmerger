from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx
import pytest
from lxml import etree
from test_review_regressions import build, contents

from bookmerger.converter import FB2Converter
from bookmerger.download import Downloader
from bookmerger.epub import write_epub
from bookmerger.validate import ValidationError, validate_epub


@pytest.mark.parametrize("remote", [False, True])
def test_svg_presentation_urls_survive_base_removal(epub_factory, edit_epub, tmp_path, remote):
    source = epub_factory(3)
    entries = contents(source)
    paints = (
        b'<svg xmlns="http://www.w3.org/2000/svg"><defs>'
        b'<linearGradient id="gradient"/><filter id="filter"/></defs></svg>'
    )
    base = "https://assets.test/" if remote else "../styles/"
    entries["OEBPS/images/diagram.svg"] = (
        f'<svg xmlns="http://www.w3.org/2000/svg" xml:base="{base}">'
        '<rect fill="url(paints.svg#gradient)" filter="url(&quot;paints.svg#filter&quot;)"/>'
        "</svg>"
    ).encode()
    if not remote:
        entries["OEBPS/styles/paints.svg"] = paints
        entries["OEBPS/content.opf"] = entries["OEBPS/content.opf"].replace(
            b"</manifest>",
            b'<item id="paints" href="styles/paints.svg" media-type="image/svg+xml"/></manifest>',
        )
    edit_epub(source, entries)
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=paints, headers={"content-type": "image/svg+xml"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _, output = build(source, tmp_path / "staging", downloader=Downloader(client=client))
    assert requests == (["https://assets.test/paints.svg"] if remote else [])
    root = etree.fromstring(output["books/0001/OEBPS/images/diagram.svg"])
    assert not root.xpath("//@xml:base")
    rect = root[0]
    expected = "../../_remote/0000.svg" if remote else "../styles/paints.svg"
    assert rect.get("fill") == f'url("{expected}#gradient")'
    assert rect.get("filter") == f'url("{expected}#filter")'
    # Validation must catch missing paint resources and missing fragment targets.
    for target, message in (
        ("missing.svg#gradient", "missing resource"),
        (expected + "#missing", "missing anchor"),
    ):
        rect.set("fill", f"url({target})")
        (tmp_path / "staging/books/0001/OEBPS/images/diagram.svg").write_bytes(etree.tostring(root))
        write_epub(tmp_path / "staging", tmp_path / "broken.epub")
        with pytest.raises(ValidationError, match=message):
            validate_epub(tmp_path / "broken.epub")


@pytest.mark.skipif(not os.environ.get("FBC_INTEGRATION"), reason="requires pinned fbc")
def test_real_fbc_preserves_source_publication_details(tmp_path):
    source = tmp_path / "source.fb2"
    source.write_bytes(
        Path("tests/fixtures/book.fb2")
        .read_bytes()
        .replace(
            b"</description>",
            b"<publish-info><book-name>Print edition title</book-name>"
            b"<publisher>Source Press</publisher><city>Source City</city><year>1999</year>"
            b'<isbn>978-1-23456-789-0</isbn><sequence name="Print Series" number="7"/>'
            b'</publish-info><custom-info info-type="rights">Source copyright</custom-info>'
            b'<custom-info info-type="edition-note">Special edition</custom-info></description>',
        )
    )
    converted = tmp_path / "converted.epub"
    FB2Converter().convert(source, converted)
    _, output = build(converted, tmp_path / "staging")
    bibliography = " ".join(etree.fromstring(output["EPUB/book-0001.xhtml"]).itertext())
    for value in (
        "978-1-23456-789-0",
        "Print edition title",
        "Source Press",
        "Source City",
        "1999",
        "Print Series",
        "7",
        "rights",
        "Source copyright",
        "edition-note",
        "Special edition",
    ):
        assert value in bibliography
    assert b"978-1-23456-789-0" not in output["EPUB/package.opf"]
    if os.environ.get("EPUBCHECK"):
        checked = subprocess.run(
            ["java", "-jar", os.environ["EPUBCHECK"], str(tmp_path / "collection.epub")],
            capture_output=True,
            text=True,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
