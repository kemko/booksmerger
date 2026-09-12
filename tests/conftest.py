from __future__ import annotations

import base64
import zipfile
from pathlib import Path

import pytest

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def epub_factory(tmp_path: Path):
    def make(version: int, *, svg_cover: bool = False) -> Path:
        book = tmp_path / f"book{version}.epub"
        root = "OEBPS"
        navigation = (
            '<item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
            if version == 2
            else '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        )
        toc_ref = 'toc="toc"' if version == 2 else ""
        entries = {
            "mimetype": b"application/epub+zip",
            "META-INF/container.xml": (
                '<?xml version="1.0"?><container version="1.0" '
                'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                f'<rootfiles><rootfile full-path="{root}/content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>'
            ).encode(),
            f"{root}/content.opf": (
                f'<?xml version="1.0" encoding="UTF-8"?><package version="{version}.0" '
                'unique-identifier="book" xmlns="http://www.idpf.org/2007/opf">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book">id'
                f"-{version}</dc:identifier><dc:title>Fixture {version}</dc:title>"
                f'<dc:creator id="author">Author {version}</dc:creator>'
                f'<dc:contributor id="translator">Translator {version}</dc:contributor>'
                f"<dc:language>{'ru' if version == 2 else 'en'}</dc:language>"
                "<dc:subject>Shared subject</dc:subject><dc:publisher>Fixture Press</dc:publisher>"
                f"<dc:rights>Rights {version}</dc:rights>"
                '<meta refines="#author" property="role" scheme="marc:relators">aut</meta>'
                '<meta refines="#translator" property="role" scheme="marc:relators">trl</meta>'
                "</metadata><manifest>"
                '<item id="chapter" href="text/chapter.xhtml" media-type="application/xhtml+xml"/>'
                '<item id="css" href="styles/book.css" media-type="text/css"/>'
                '<item id="appendix" href="text/appendix.xhtml" media-type="application/xhtml+xml"/>'
                '<item id="font" href="fonts/reader.woff2" media-type="font/woff2"/>'
                '<item id="audio" href="media/audio.mp3" media-type="audio/mpeg"/>'
                '<item id="cover" href="images/cover.png" media-type="image/png" properties="cover-image"/>'
                '<item id="diagram" href="images/diagram.svg" media-type="image/svg+xml"/>'
                f'{navigation}</manifest><spine {toc_ref}><itemref idref="chapter"/>'
                '<itemref idref="appendix" linear="no"/></spine></package>'
            ).encode(),
            f"{root}/text/chapter.xhtml": (
                '<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                '<head><title>Chapter</title><link rel="stylesheet" href="../styles/book.css"/></head><body><section id="same-id">'
                '<h1>Chapter</h1><p id="page-1">Text <a id="back" href="#note">1</a>.</p><aside id="note">'
                '<a href="#back">Return</a></aside><img src="../images/cover.png" alt="cover"/>'
                '<img src="../images/diagram.svg" alt="diagram"/></section></body></html>'
            ).encode(),
            f"{root}/styles/book.css": b"p { color: navy; }",
            f"{root}/text/appendix.xhtml": (
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Appendix</title></head><body><p id="end">Appendix</p>'
                '<audio src="../media/audio.mp3"/></body></html>'
            ).encode(),
            f"{root}/fonts/reader.woff2": b"ordinary-font-bytes",
            f"{root}/media/audio.mp3": b"media-bytes",
            f"{root}/images/cover.png": PNG,
            f"{root}/images/diagram.svg": (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><text id="same-id">SVG</text></svg>'
            ).encode(),
            f"{root}/toc.ncx": (
                '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">'
                '<navMap><navPoint id="n1" playOrder="1"><navLabel><text>Chapter</text></navLabel>'
                '<content src="text/chapter.xhtml"/><navPoint id="n2" playOrder="2">'
                '<navLabel><text>Note</text></navLabel><content src="text/chapter.xhtml#note"/>'
                "</navPoint></navPoint></navMap></ncx>"
            ).encode(),
            f"{root}/nav.xhtml": (
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Contents</title></head><body><nav epub:type="toc" '
                'xmlns:epub="http://www.idpf.org/2007/ops"><ol><li><a href="text/chapter.xhtml">Chapter</a>'
                '<ol><li><a href="text/chapter.xhtml#note">Note</a></li></ol></li></ol>'
                '</nav><nav epub:type="page-list" xmlns:epub="http://www.idpf.org/2007/ops">'
                '<ol><li><a href="text/chapter.xhtml#page-1">1</a></li></ol></nav>'
                '<nav epub:type="landmarks" xmlns:epub="http://www.idpf.org/2007/ops">'
                '<ol><li><a href="text/chapter.xhtml" epub:type="bodymatter">Start</a></li></ol></nav></body></html>'
            ).encode(),
        }
        if svg_cover:
            package = entries[f"{root}/content.opf"]
            package = package.replace(
                b'id="cover" href="images/cover.png" media-type="image/png" properties="cover-image"',
                b'id="cover-raster" href="images/cover.png" media-type="image/png"',
            ).replace(
                b'<item id="diagram"',
                b'<item id="cover" href="images/cover.svg" media-type="image/svg+xml" '
                b'properties="cover-image"/><item id="diagram"',
            )
            entries[f"{root}/content.opf"] = package
            entries[f"{root}/images/cover.svg"] = (
                b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1">'
                b"<text>Cover</text></svg>"
            )
        with zipfile.ZipFile(book, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return book

    return make


@pytest.fixture
def edit_epub():
    def edit(path: Path, updates: dict[str, bytes]) -> Path:
        with zipfile.ZipFile(path) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        entries.update(updates)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for name, data in entries.items():
                target.writestr(name, data)
        return path

    return edit


@pytest.fixture
def rich_fb2(tmp_path: Path) -> Path:
    from lxml import etree

    source = etree.parse("tests/fixtures/book.fb2")
    ns = "http://www.gribuser.ru/xml/fictionbook/2.0"
    xlink = "http://www.w3.org/1999/xlink"
    section = source.find(f".//{{{ns}}}body/{{{ns}}}section")
    # Two distinct PNGs with IDs containing extensions, plus a vector illustration.
    import struct
    import zlib

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    second = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\0\xff\0\xff"))
        + chunk(b"IEND", b"")
    )
    images = [
        ("one.png", "image/png", PNG),
        ("two.png", "image/png", second),
        (
            "diagram.svg",
            "image/svg+xml",
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><text id="label">Vector</text></svg>',
        ),
    ]
    source.getroot().remove(source.find(f"{{{ns}}}binary"))
    source.find(f".//{{{ns}}}coverpage/{{{ns}}}image").set(f"{{{xlink}}}href", "#one.png")
    for name, mime, data in images:
        image = etree.Element(f"{{{ns}}}image")
        section.insert(2, image)
        image.set(f"{{{xlink}}}href", f"#{name}")
        binary = etree.SubElement(source.getroot(), f"{{{ns}}}binary", id=name)
        binary.set("content-type", mime)
        binary.text = base64.b64encode(data).decode()
    child = etree.SubElement(section, f"{{{ns}}}section", id="nested")
    etree.SubElement(
        etree.SubElement(child, f"{{{ns}}}title"), f"{{{ns}}}p"
    ).text = "Nested chapter"
    etree.SubElement(child, f"{{{ns}}}p").text = "Nested text"
    title_info = source.find(f".//{{{ns}}}title-info")
    etree.SubElement(title_info, f"{{{ns}}}lang").text = "en"
    result = tmp_path / "rich.fb2"
    source.write(result, encoding="utf-8", xml_declaration=True)
    return result
