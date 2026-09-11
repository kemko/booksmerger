from __future__ import annotations

import base64
import zipfile
from pathlib import Path

import pytest

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
    "KSbP8wAAAABJRU5ErkJggg=="
)


@pytest.fixture
def epub_factory(tmp_path: Path):
    def make(version: int) -> Path:
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
                '<head><link rel="stylesheet" href="../styles/book.css"/></head><body><section id="same-id">'
                '<h1>Chapter</h1><p>Text <a id="back" href="#note">1</a>.</p><aside id="note">'
                '<a href="#back">Return</a></aside><img src="../images/cover.png" alt="cover"/>'
                '<img src="../images/diagram.svg" alt="diagram"/></section></body></html>'
            ).encode(),
            f"{root}/styles/book.css": b"p { color: navy; }",
            f"{root}/text/appendix.xhtml": (
                '<html xmlns="http://www.w3.org/1999/xhtml"><body><p id="end">Appendix</p>'
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
                '<html xmlns="http://www.w3.org/1999/xhtml"><body><nav epub:type="toc" '
                'xmlns:epub="http://www.idpf.org/2007/ops"><ol><li><a href="text/chapter.xhtml">Chapter</a>'
                '<ol><li><a href="text/chapter.xhtml#note">Note</a></li></ol></li></ol>'
                "</nav></body></html>"
            ).encode(),
        }
        with zipfile.ZipFile(book, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return book

    return make
