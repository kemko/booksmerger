from __future__ import annotations

import hashlib
import os
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from epubmerge.epubmerge import doMerge
from lxml import etree

from bookmerger.converter import FB2Converter
from bookmerger.download import DownloadLimits
from bookmerger.merge import MergeError, merge_epubs


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
    assert {f"{number}/OEBPS/text/chapter.xhtml" for number in range(1, 4)} <= names
    assert ncx.index("Second fixture") < ncx.index("Fixture 2")
    assert package.xpath("string(//*[local-name()='title'][1])") == "Full title"
    assert package.xpath("//*[local-name()='language']/text()") == ["ru"]


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
def test_real_fbc_epub2_merges_with_ncx(tmp_path: Path) -> None:
    source = tmp_path / "source.fb2"
    source.write_bytes(Path("tests/fixtures/book.fb2").read_bytes())
    converted = tmp_path / "converted.epub"

    FB2Converter().convert(source, converted)
    with zipfile.ZipFile(converted) as archive:
        opf_name = etree.fromstring(archive.read("META-INF/container.xml")).xpath(
            "string(//*[local-name()='rootfile']/@full-path)"
        )
        opf = etree.fromstring(archive.read(opf_name))
        assert opf.get("version") == "2.0"
        assert opf.xpath("string(//*[local-name()='spine']/@toc)")
        assert any(name.endswith(".ncx") for name in archive.namelist())

    merged = BytesIO()
    doMerge(merged, [str(converted)], titleopt="FB2 fixture", languages=["ru"])
    with zipfile.ZipFile(merged) as archive:
        assert "toc.ncx" in archive.namelist()
        assert "FB2 fixture" in archive.read("toc.ncx").decode()
