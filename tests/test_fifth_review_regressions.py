from __future__ import annotations

import os
import subprocess
import zipfile

import pytest
from lxml import etree
from test_pipeline import LocalDownloader
from test_review_regressions import contents

from bookmerger.cli import BuildError, Command, assemble


@pytest.mark.parametrize(
    "kind",
    ["css", "css-no-extension", "import", "inline", "style", "srcset", "object", "anchor"],
)
def test_missing_embedded_reference_preserves_output(epub_factory, edit_epub, tmp_path, kind):
    source = epub_factory(3)
    entries = contents(source)
    chapter = entries["OEBPS/text/chapter.xhtml"]
    if kind in {"css", "css-no-extension", "import", "anchor"}:
        css = b'p { background: url("../images/missing.png"); }'
        if kind == "import":
            css = b'@import "missing.css";'
        elif kind == "anchor":
            css = b'p { filter: url("../images/diagram.svg#missing"); }'
        name = "book" if kind == "css-no-extension" else "book.css"
        entries["OEBPS/styles/" + name] = css
        entries["OEBPS/content.opf"] = entries["OEBPS/content.opf"].replace(
            b"book.css", name.encode()
        )
        chapter = chapter.replace(b"book.css", name.encode())
    else:
        markup = {
            "inline": b'<p style="background:url(../images/missing.png)">Text</p>',
            "style": b"<style>p { background:url(../images/missing.png); }</style>",
            "srcset": b'<img srcset="../images/cover.png 1x, ../images/missing.png 2x"/>',
            "object": b'<object data="../images/missing.png"/>',
        }[kind]
        chapter = chapter.replace(b"</body>", markup + b"</body>")
    entries["OEBPS/text/chapter.xhtml"] = chapter
    edit_epub(source, entries)
    output = tmp_path / "collection.epub"
    output.write_bytes(b"previous collection")
    with pytest.raises(BuildError, match="missing (resource|anchor)"):
        assemble(
            Command("Collection", output, ("one",), True), downloader=LocalDownloader((source,))
        )
    assert output.read_bytes() == b"previous collection"
    assert not list(tmp_path.glob(".collection-*"))


def test_all_contributor_roles_survive_assembly(epub_factory, edit_epub, tmp_path):
    source = epub_factory(3)
    opf = contents(source)["OEBPS/content.opf"].replace(
        b"</metadata>",
        b'<meta refines="#author" property="role" scheme="marc:relators">ill</meta>'
        b'<meta refines="#author" property="role" scheme="marc:relators">aut</meta>'
        b"</metadata>",
    )
    edit_epub(source, {"OEBPS/content.opf": opf})
    output = tmp_path / "collection.epub"
    assemble(Command("Collection", output, ("one",), False), downloader=LocalDownloader((source,)))
    with zipfile.ZipFile(output) as archive:
        root = etree.fromstring(archive.read("EPUB/package.opf"))
        front = etree.fromstring(archive.read("EPUB/book-0001.xhtml"))
    assert root.xpath('//*[local-name()="creator"]/text()') == ["Author 3"]
    assert root.xpath('//*[@property="role"]/text()') == ["aut", "ill", "trl"]
    assert "ill: Author 3" in "".join(front.itertext())
    assert "aut: Author 3" in "".join(front.itertext())


def test_valid_embedded_reference_forms_are_accepted(epub_factory, edit_epub, tmp_path):
    source = epub_factory(3)
    chapter = contents(source)["OEBPS/text/chapter.xhtml"].replace(
        b"</body>",
        b'<p style="filter:url(../images/diagram.svg#same-id)">Text</p>'
        b'<style>p { background:url("../images/cover.png"); }</style>'
        b'<img srcset="data:image/png;base64,AAAA 1x, ../images/cover.png 2x"/>'
        b'<object data="../images/diagram.svg#same-id"/></body>',
    )
    edit_epub(
        source,
        {
            "OEBPS/text/chapter.xhtml": chapter,
            "OEBPS/styles/book.css": b"@font-face { src:url(../fonts/reader.woff2); }"
            b"p { filter:url(../images/diagram.svg#same-id); }",
        },
    )
    output = tmp_path / "collection.epub"
    assemble(Command("Collection", output, ("one",), False), downloader=LocalDownloader((source,)))
    assert output.is_file()


@pytest.mark.parametrize("declaration", ["", "custom:", "rendition: https://example.org/other#"])
def test_unsafe_prefix_declarations_preserve_output(epub_factory, edit_epub, tmp_path, declaration):
    source = epub_factory(3)
    opf = (
        contents(source)["OEBPS/content.opf"]
        .replace(b"<package ", f'<package prefix="{declaration}" '.encode())
        .replace(b'<item id="chapter"', b'<item properties="custom:illustrated" id="chapter"')
    )
    edit_epub(source, {"OEBPS/content.opf": opf})
    output = tmp_path / "collection.epub"
    output.write_bytes(b"previous collection")
    with pytest.raises(BuildError, match="prefix"):
        assemble(
            Command("Collection", output, ("one",), True), downloader=LocalDownloader((source,))
        )
    assert output.read_bytes() == b"previous collection"


def test_conflicting_source_prefixes_keep_their_vocabularies(epub_factory, edit_epub, tmp_path):
    sources = (epub_factory(2), epub_factory(3))
    for number, source in enumerate(sources, 1):
        opf = (
            contents(source)["OEBPS/content.opf"]
            .replace(
                b"<package ",
                f'<package prefix="custom: https://example.org/vocab{number}#" '.encode(),
            )
            .replace(b'<item id="chapter"', b'<item properties="custom:illustrated" id="chapter"')
            .replace(
                b'<itemref idref="chapter"', b'<itemref properties="custom:special" idref="chapter"'
            )
        )
        edit_epub(source, {"OEBPS/content.opf": opf})
    output = tmp_path / "collection.epub"
    assemble(
        Command("Collection", output, ("one", "two"), False), downloader=LocalDownloader(sources)
    )
    with zipfile.ZipFile(output) as archive:
        root = etree.fromstring(archive.read("EPUB/package.opf"))
    tokens = root.get("prefix", "").split()
    prefixes = dict(zip((token[:-1] for token in tokens[::2]), tokens[1::2], strict=True))
    for number in (1, 2):
        for attribute, term in (("id", "illustrated"), ("idref", "special")):
            prop = root.xpath(f'//*[@{attribute}="book-{number:04d}-chapter"]/@properties')[0]
            prefix, local = prop.split(":")
            assert prefixes[prefix] == f"https://example.org/vocab{number}#"
            assert local == term
    if os.environ.get("EPUBCHECK"):
        result = subprocess.run(
            ["java", "-jar", os.environ["EPUBCHECK"], str(output)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr
