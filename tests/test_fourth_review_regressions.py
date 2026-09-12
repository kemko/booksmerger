from __future__ import annotations

import os
import subprocess

import httpx
import pytest
from conftest import PNG
from lxml import etree
from test_pipeline import LocalDownloader
from test_review_regressions import contents

from bookmerger.cli import BuildError, Command, assemble
from bookmerger.download import Downloader
from bookmerger.references import resolve_uri


@pytest.mark.parametrize("attribute", ["href", "xlink:href"])
@pytest.mark.parametrize("element", ["feImage", "linearGradient", "radialGradient", "pattern"])
def test_svg_linked_resources_are_embedded(
    epub_factory, edit_epub, monkeypatch, tmp_path, attribute, element
):
    source = epub_factory(3)
    raster = element == "feImage"
    resource = "image.png" if raster else "definitions.svg#paint"
    dependency = (
        PNG
        if raster
        else (
            f'<svg xmlns="http://www.w3.org/2000/svg"><defs><{element} id="paint"/></defs></svg>'
        ).encode()
    )
    linked = f'<{element} id="linked" {attribute}="https://assets.test/{resource}"/>'
    definition = f'<filter id="filter">{linked}</filter>' if raster else linked
    effect = 'filter="url(#filter)"' if raster else 'fill="url(#linked)"'
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        f'<defs>{definition}</defs><rect width="1" height="1" {effect}/>'
        f'<a {attribute}="https://example.test/page"><text>External</text></a></svg>'
    ).encode()
    edit_epub(source, {"OEBPS/images/diagram.svg": svg})
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if request.url.host == "source.test":
            return httpx.Response(200, content=source.read_bytes())
        assert request.url.host == "assets.test"
        return httpx.Response(
            200,
            content=dependency,
            headers={"content-type": "image/png" if raster else "image/svg+xml"},
        )

    output = tmp_path / "collection.epub"
    monkeypatch.chdir(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assemble(
            Command("collection", ("https://source.test/book",), False),
            downloader=Downloader(client=client),
        )
    assert requested == [
        "https://source.test/book",
        "https://assets.test/" + resource.split("#")[0],
    ]
    result = contents(output)
    document = "books/0001/OEBPS/images/diagram.svg"
    root = etree.fromstring(result[document])
    href = "{http://www.w3.org/1999/xlink}href" if attribute == "xlink:href" else "href"
    target = resolve_uri(
        document, root.find(f".//{{http://www.w3.org/2000/svg}}{element}").get(href)
    )
    assert target and result[target[0]] == dependency
    assert target[2] == ("" if raster else "paint")
    assert root.find("{http://www.w3.org/2000/svg}a").get(href) == "https://example.test/page"


def test_svg_resource_failure_preserves_existing_output(
    epub_factory, edit_epub, monkeypatch, tmp_path
):
    source = edit_epub(
        epub_factory(3),
        {
            "OEBPS/images/diagram.svg": (
                b'<svg xmlns="http://www.w3.org/2000/svg"><filter id="f">'
                b'<feImage href="https://assets.test/missing.png"/></filter></svg>'
            )
        },
    )
    output = tmp_path / "collection.epub"
    output.write_bytes(b"existing output")
    monkeypatch.chdir(tmp_path)

    def handler(request):
        return (
            httpx.Response(200, content=source.read_bytes())
            if request.url.host == "source.test"
            else httpx.Response(404)
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BuildError, match="assets.test/missing.png"):
            assemble(
                Command("collection", ("https://source.test/book",), True),
                downloader=Downloader(client=client),
            )
    assert output.read_bytes() == b"existing output"
    assert not list(tmp_path.glob(".collection-*"))


@pytest.mark.parametrize("versions", [(2,), (2, 3)])
def test_ncx_page_list_survives_in_collection(
    epub_factory, edit_epub, monkeypatch, tmp_path, versions
):
    sources = tuple(epub_factory(version) for version in versions)
    ncx = contents(sources[0])["OEBPS/toc.ncx"].replace(
        b"</ncx>",
        b'<pageList><pageTarget id="page1" type="normal" value="1" playOrder="3">'
        b'<navLabel><text>1</text></navLabel><content src="text/chapter.xhtml#page-1"/>'
        b"</pageTarget></pageList></ncx>",
    )
    edit_epub(sources[0], {"OEBPS/toc.ncx": ncx})
    output = tmp_path / "collection.epub"
    monkeypatch.chdir(tmp_path)
    assemble(
        Command("collection", tuple(str(source) for source in sources), False),
        downloader=LocalDownloader(sources),
    )
    result = contents(output)
    for document in ("EPUB/nav.xhtml", "EPUB/toc.xhtml"):
        nav = etree.fromstring(result[document])
        links = nav.xpath(
            "//*[local-name()='nav'][@epub:type='page-list']//*[local-name()='a']",
            namespaces={"epub": "http://www.idpf.org/2007/ops"},
        )
        assert [link.text for link in links] == [f"Fixture {version}: 1" for version in versions]
        assert [resolve_uri(document, link.get("href")) for link in links] == [
            (f"books/{number:04d}/OEBPS/text/chapter.xhtml", "", "page-1")
            for number in range(1, len(sources) + 1)
        ]
    if os.environ.get("EPUBCHECK"):
        checked = subprocess.run(
            ["java", "-jar", os.environ["EPUBCHECK"], str(output)],
            capture_output=True,
            text=True,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
