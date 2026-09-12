from __future__ import annotations

import hashlib
import os
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import pytest
from epubmerge.epubmerge import doMerge
from lxml import etree

from bookmerger.converter import FB2Converter, fb2_images
from bookmerger.download import DownloadLimits
from bookmerger.merge import MergeError, merge_epubs
from bookmerger.references import resolve_uri
from bookmerger.validate import validate_epub

NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"


def _ncx_book_points(ncx: bytes) -> list[etree._Element]:
    root = etree.fromstring(ncx)
    nav_map = root.find(f"{{{NCX_NS}}}navMap")
    assert nav_map is not None
    return nav_map.findall(f"{{{NCX_NS}}}navPoint")


def test_epubmerge_merges_two_epub2_books_with_ncx(epub_factory, edit_epub, tmp_path: Path) -> None:
    first = epub_factory(2)
    second = tmp_path / "second.epub"
    second.write_bytes(first.read_bytes())
    with zipfile.ZipFile(second) as archive:
        package = archive.read("OEBPS/content.opf").replace(b"Fixture 2", b"Second fixture")
    edit_epub(second, {"OEBPS/content.opf": package})

    merged = BytesIO()
    doMerge(merged, [str(first), str(second)], titleopt="Merged fixture", languages=["ru"])

    with zipfile.ZipFile(merged) as archive:
        names = archive.namelist()
        package = etree.fromstring(archive.read("content.opf"))
        ncx = archive.read("toc.ncx").decode()

    assert "1/OEBPS/text/chapter.xhtml" in names
    assert "2/OEBPS/text/chapter.xhtml" in names
    assert package.xpath("string(//*[local-name()='title'][1])") == "Merged fixture"
    assert "Fixture 2" in ncx
    assert "Second fixture" in ncx
    book_points = _ncx_book_points(ncx.encode())
    assert [
        point.xpath("string(ncx:navLabel/ncx:text)", namespaces={"ncx": NCX_NS})
        for point in book_points
    ] == [
        "Fixture 2",
        "Second fixture",
    ]
    assert all(point.findall(f"{{{NCX_NS}}}navPoint") for point in book_points)


def test_merge_epubs_keeps_order_duplicates_names_and_inputs(
    epub_factory, edit_epub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = epub_factory(2)
    second = tmp_path / "second.epub"
    second.write_bytes(first.read_bytes())
    with zipfile.ZipFile(second) as archive:
        package = archive.read("OEBPS/content.opf").replace(b"Fixture 2", b"Second fixture")
    edit_epub(second, {"OEBPS/content.opf": package})
    before = {path: hashlib.sha256(path.read_bytes()).digest() for path in (first, second)}
    monkeypatch.setattr(
        "socket.create_connection", lambda *_args, **_kwargs: pytest.fail("network used")
    )

    packages = merge_epubs(tmp_path / "merged.epub", (second, first, second), "Full title")

    assert [package.metadata.title for package in packages] == [
        "Second fixture",
        "Fixture 2",
        "Second fixture",
    ]
    assert {path: hashlib.sha256(path.read_bytes()).digest() for path in before} == before
    with zipfile.ZipFile(tmp_path / "merged.epub") as archive:
        names = set(archive.namelist())
        ncx = archive.read("toc.ncx").decode()
        package = etree.fromstring(archive.read("content.opf"))
        for number, source in enumerate((second, first, second), 1):
            with zipfile.ZipFile(source) as original:
                for name in (
                    "text/chapter.xhtml",
                    "text/appendix.xhtml",
                    "styles/book.css",
                    "images/cover.png",
                    "images/diagram.svg",
                    "fonts/reader.woff2",
                    "media/audio.mp3",
                ):
                    assert archive.read(f"{number}/OEBPS/{name}") == original.read(f"OEBPS/{name}")
    assert {f"{number}/OEBPS/text/chapter.xhtml" for number in range(1, 4)} <= names
    assert ncx.index("Second fixture") < ncx.index("Fixture 2")
    assert package.xpath("string(//*[local-name()='title'][1])") == "Full title"
    assert package.xpath("//*[local-name()='language']/text()") == ["ru"]
    validate_epub(tmp_path / "merged.epub")


def test_merge_preserves_single_ncx_entry_with_distinct_target(
    epub_factory, edit_epub, tmp_path: Path
) -> None:
    source = epub_factory(2)
    with zipfile.ZipFile(source) as archive:
        root = etree.fromstring(archive.read("OEBPS/toc.ncx"))
    point = root.find(f"{{{NCX_NS}}}navMap/{{{NCX_NS}}}navPoint")
    point.remove(point.find(f"{{{NCX_NS}}}navPoint"))
    point.find(f"{{{NCX_NS}}}navLabel/{{{NCX_NS}}}text").text = "Appendix"
    point.find(f"{{{NCX_NS}}}content").set("src", "text/appendix.xhtml#end")
    edit_epub(source, {"OEBPS/toc.ncx": etree.tostring(root)})

    output = tmp_path / "merged.epub"
    merge_epubs(output, (source,), "Collection")
    validate_epub(output)

    with zipfile.ZipFile(output) as archive:
        books = _ncx_book_points(archive.read("toc.ncx"))
    children = books[0].findall(f"{{{NCX_NS}}}navPoint")
    assert len(children) == 1
    assert children[0].find(f"{{{NCX_NS}}}navLabel/{{{NCX_NS}}}text").text == "Appendix"
    assert children[0].find(f"{{{NCX_NS}}}content").get("src") == "1/OEBPS/text/appendix.xhtml#end"


@pytest.mark.parametrize("tag", ["ncx", "navMap", "navPoint", "content"])
def test_merge_rejects_prefixed_ncx_before_creating_output(
    epub_factory, edit_epub, tmp_path: Path, tag: str
) -> None:
    source = epub_factory(2)
    with zipfile.ZipFile(source) as archive:
        ncx = archive.read("OEBPS/toc.ncx").replace(
            b"<ncx ", f'<ncx xmlns:ncx="{NCX_NS}" '.encode()
        )
    ncx = ncx.replace(f"<{tag}".encode(), f"<ncx:{tag}".encode()).replace(
        f"</{tag}>".encode(), f"</ncx:{tag}>".encode()
    )
    edit_epub(source, {"OEBPS/toc.ncx": ncx})
    output = tmp_path / "merged.epub"

    with pytest.raises(MergeError, match="source 1: namespace-prefixed NCX"):
        merge_epubs(output, (source,), "Collection")
    assert not output.exists()


def test_merge_rejects_ncx_in_another_directory_before_creating_output(
    epub_factory, edit_epub, tmp_path: Path
) -> None:
    source = epub_factory(2)
    with zipfile.ZipFile(source) as archive:
        package = archive.read("OEBPS/content.opf").replace(
            b' href="toc.ncx"', b' href="nav/toc.ncx"'
        )
        package = package.replace(
            b"</manifest>",
            b'<item id="decoy" href="../text/chapter.xhtml" '
            b'media-type="application/xhtml+xml"/></manifest>',
        )
        ncx = archive.read("OEBPS/toc.ncx").replace(b'src="text/', b'src="../text/')
    edit_epub(
        source,
        {
            "OEBPS/content.opf": package,
            "OEBPS/nav/toc.ncx": ncx,
            # Both wrong targets exist, so output reference validation cannot detect misrouting.
            "text/chapter.xhtml": (
                b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Decoy</title></head>'
                b'<body><p id="note">Wrong chapter and footnote</p></body></html>'
            ),
        },
    )
    output = tmp_path / "merged.epub"

    with pytest.raises(MergeError, match="source 1: NCX must share the OPF directory"):
        merge_epubs(output, (source,), "Collection")
    assert not output.exists()


@pytest.mark.parametrize("resource", ["toc name.ncx", "оглавление.ncx"])
def test_merge_rejects_encoded_ncx_path_before_using_unvalidated_navigation(
    epub_factory, edit_epub, tmp_path: Path, resource: str
) -> None:
    source = epub_factory(2)
    href = quote(resource)
    with zipfile.ZipFile(source) as archive:
        package = archive.read("OEBPS/content.opf").replace(b"toc.ncx", href.encode())
        ncx = archive.read("OEBPS/toc.ncx")
    edit_epub(
        source,
        {
            "OEBPS/content.opf": package,
            f"OEBPS/{resource}": ncx,
            # The engine reads this raw href instead of the validated, decoded path.
            f"OEBPS/{href}": ncx.replace(b"text/chapter.xhtml#note", b"text/appendix.xhtml#end")
            .replace(b"text/chapter.xhtml", b"text/appendix.xhtml")
            .replace(b">Chapter<", b">Wrong chapter<"),
        },
    )
    output = tmp_path / "merged.epub"

    with pytest.raises(MergeError, match="source 1: encoded NCX paths are unsupported"):
        merge_epubs(output, (source,), "Collection")
    assert not output.exists()


@pytest.mark.parametrize(
    "resource",
    [
        "text/chapter%20.xhtml",
        "text/chapter?.xhtml",
        "text/chapter#.xhtml",
        "text%20/chapter.xhtml",
    ],
)
def test_merge_rejects_paths_that_change_meaning_when_unescaped(
    epub_factory, edit_epub, tmp_path: Path, resource: str
) -> None:
    source = epub_factory(2)
    href = quote(resource).encode()
    with zipfile.ZipFile(source) as archive:
        package = archive.read("OEBPS/content.opf").replace(b"text/chapter.xhtml", href)
        package = package.replace(
            b"</manifest>",
            b'<item id="decoy" href="text/chapter%20.xhtml" '
            b'media-type="application/xhtml+xml"/></manifest>',
        )
        chapter = archive.read("OEBPS/text/chapter.xhtml")
        ncx = archive.read("OEBPS/toc.ncx").replace(b"text/chapter.xhtml", href)
    edit_epub(
        source,
        {
            "OEBPS/content.opf": package,
            "OEBPS/toc.ncx": ncx,
            f"OEBPS/{resource}": chapter,
            # A second decoding resolves to this valid but incorrect chapter.
            "OEBPS/text/chapter .xhtml": chapter.replace(b"<h1>Chapter", b"<h1>Wrong chapter"),
        },
    )
    output = tmp_path / "merged.epub"

    with pytest.raises(MergeError, match="source 1: unsupported URI characters in manifest path"):
        merge_epubs(output, (source,), "Collection")
    assert not output.exists()


@pytest.mark.parametrize("resource", ["text/chapter 1.xhtml", "text/глава.xhtml"])
def test_merge_preserves_encoded_spaces_and_unicode_paths(
    epub_factory, edit_epub, tmp_path: Path, resource: str
) -> None:
    source = epub_factory(2)
    href = quote(resource).encode()
    with zipfile.ZipFile(source) as archive:
        chapter = archive.read("OEBPS/text/chapter.xhtml")
        package = archive.read("OEBPS/content.opf").replace(b"text/chapter.xhtml", href)
        ncx = archive.read("OEBPS/toc.ncx").replace(b"text/chapter.xhtml", href)
    edit_epub(
        source,
        {"OEBPS/content.opf": package, "OEBPS/toc.ncx": ncx, f"OEBPS/{resource}": chapter},
    )
    output = tmp_path / "merged.epub"

    merge_epubs(output, (source,), "Collection")
    validate_epub(output)

    with zipfile.ZipFile(output) as archive:
        package = etree.fromstring(archive.read("content.opf"))
        href = package.xpath('//*[local-name()="item" and @id="a1chapter"]/@href')[0]
        target = resolve_uri("content.opf", href)[0]
        assert target == f"1/OEBPS/{resource}"
        assert archive.read(target) == chapter


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda entries: entries.update(
                {
                    "OEBPS/content.opf": entries["OEBPS/content.opf"].replace(
                        b"cover.png", b"missing.png"
                    )
                }
            ),
            "missing manifest resource",
        ),
        (
            lambda entries: entries.update(
                {"META-INF/encryption.xml": b"<encryption><EncryptedData/></encryption>"}
            ),
            "DRM",
        ),
        (
            lambda entries: entries.update(
                {
                    "OEBPS/content.opf": entries["OEBPS/content.opf"].replace(
                        b'<itemref idref="chapter"/><itemref idref="appendix" linear="no"/>', b""
                    )
                }
            ),
            "spine is empty",
        ),
        (
            lambda entries: entries.update(
                {
                    "OEBPS/toc.ncx": entries["OEBPS/toc.ncx"].replace(
                        b"text/chapter.xhtml", b"https://example.test/chapter.xhtml"
                    )
                }
            ),
            "NCX refers",
        ),
    ],
)
def test_merge_epubs_rejects_unsafe_input_before_merging(
    change, message: str, edit_epub, epub_factory, tmp_path: Path
) -> None:
    source = epub_factory(2)
    with zipfile.ZipFile(source) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    change(entries)
    edit_epub(source, entries)

    with pytest.raises(MergeError, match=rf"source 1: .*{message}"):
        merge_epubs(tmp_path / "merged.epub", (source,), "Title")
    assert not (tmp_path / "merged.epub").exists()


def test_merge_epubs_rejects_nav_only_and_ambiguous_ncx(
    epub_factory, edit_epub, tmp_path: Path
) -> None:
    nav_only = epub_factory(3)
    with pytest.raises(MergeError, match="exactly one NCX"):
        merge_epubs(tmp_path / "nav-only.epub", (nav_only,), "Title")

    source = epub_factory(2)
    with zipfile.ZipFile(source) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries["OEBPS/second.ncx"] = entries["OEBPS/toc.ncx"]
    entries["OEBPS/content.opf"] = entries["OEBPS/content.opf"].replace(
        b"</manifest>",
        b'<item id="toc2" href="second.ncx" media-type="application/x-dtbncx+xml"/></manifest>',
    )
    edit_epub(source, entries)
    with pytest.raises(MergeError, match="exactly one NCX"):
        merge_epubs(tmp_path / "ambiguous.epub", (source,), "Title")


def test_merge_epubs_accepts_epub3_with_ncx(epub_factory, tmp_path: Path) -> None:
    """EPUB 3 sources are supported only through their NCX, not their nav document."""
    source = epub_factory(3, ncx=True)
    output = tmp_path / "merged.epub"

    merge_epubs(output, (source,), "EPUB 3 source")

    with zipfile.ZipFile(output) as archive:
        points = _ncx_book_points(archive.read("toc.ncx"))
    assert [
        point.xpath("string(ncx:navLabel/ncx:text)", namespaces={"ncx": NCX_NS}) for point in points
    ] == ["Fixture 3"]


def test_merge_epubs_applies_zip_limits_and_safe_paths(
    epub_factory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = epub_factory(2)
    with monkeypatch.context() as scoped:
        scoped.setattr("bookmerger.epub.DEFAULT_LIMITS", DownloadLimits(max_zip_entries=1))
        with pytest.raises(MergeError, match="ZIP has too many entries"):
            merge_epubs(tmp_path / "limited.epub", (source,), "Title")

    unsafe = tmp_path / "unsafe.epub"
    unsafe.write_bytes(source.read_bytes())
    with zipfile.ZipFile(unsafe, "a") as archive:
        archive.writestr("../outside.xhtml", b"outside")
    with pytest.raises(MergeError, match="ZIP contains an unsafe path"):
        merge_epubs(tmp_path / "unsafe-output.epub", (unsafe,), "Title")


@pytest.mark.skipif(not os.environ.get("FBC_INTEGRATION"), reason="requires pinned fbc")
def test_real_fbc_epub2_merges_with_nested_ncx_in_source_order(tmp_path: Path) -> None:
    converted: list[Path] = []
    for number, title in enumerate(("First FB2", "Second FB2"), 1):
        source = tmp_path / f"source-{number}.fb2"
        source.write_bytes(
            Path("tests/fixtures/book.fb2").read_bytes().replace(b"FB2 fixture", title.encode())
        )
        target = tmp_path / f"converted-{number}.epub"
        FB2Converter().convert(source, target)
        converted.append(target)
        with zipfile.ZipFile(target) as archive:
            opf_name = etree.fromstring(archive.read("META-INF/container.xml")).xpath(
                "string(//*[local-name()='rootfile']/@full-path)"
            )
            opf = etree.fromstring(archive.read(opf_name))
            assert opf.get("version") == "2.0"
            assert opf.xpath("string(//*[local-name()='spine']/@toc)")
            assert any(name.endswith(".ncx") for name in archive.namelist())

    output = tmp_path / "merged.epub"
    merge_epubs(output, converted, "FB2 collection")
    with zipfile.ZipFile(output) as archive:
        points = _ncx_book_points(archive.read("toc.ncx"))
    assert [
        point.xpath("string(ncx:navLabel/ncx:text)", namespaces={"ncx": NCX_NS}) for point in points
    ] == [
        "First FB2",
        "Second FB2",
    ]
    assert all(point.findall(f"{{{NCX_NS}}}navPoint") for point in points)


@pytest.mark.skipif(not os.environ.get("FBC_INTEGRATION"), reason="requires pinned fbc")
def test_real_fbc_merge_preserves_images_text_and_footnotes(rich_fb2, tmp_path: Path) -> None:
    converted, output = tmp_path / "converted.epub", tmp_path / "merged.epub"
    FB2Converter().convert(rich_fb2, converted)
    merge_epubs(output, (converted,), "Rich FB2")
    validate_epub(output)

    with zipfile.ZipFile(converted) as original, zipfile.ZipFile(output) as merged:
        for name in original.namelist():
            if name.endswith((".xhtml", ".css", ".png", ".svg")):
                assert merged.read(f"1/{name}") == original.read(name)
        merged_data = [merged.read(name) for name in merged.namelist()]
        assert all(image.data in merged_data for image in fb2_images(rich_fb2))
        documents = [
            etree.fromstring(merged.read(name))
            for name in merged.namelist()
            if name.endswith(".xhtml")
        ]
    text = " ".join(" ".join(root.itertext()) for root in documents)
    assert "Nested chapter" in text
    assert "Nested text" in text
    assert "Note" in text
    links = [link for root in documents for link in root.xpath("//*[local-name()='a']")]
    assert any(link.get("href", "").endswith("#note") for link in links)
    assert any(
        link.get("href", "").endswith("#same-id") and "return" in "".join(link.itertext())
        for link in links
    )
