from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from bookmerger.converter import FB2Converter
from bookmerger.merge import merge_epubs
from bookmerger.validate import ValidationError, validate_epub

NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"


def merged_epub(epub_factory, tmp_path: Path) -> Path:
    output = tmp_path / "merged.epub"
    merge_epubs(output, (epub_factory(2),), "Collection")
    return output


def _rewrite_epub(path: Path, output: Path, mutate) -> Path:
    with zipfile.ZipFile(path) as source:
        entries = {info.filename: source.read(info.filename) for info in source.infolist()}
        infos = source.infolist()
    mutate(entries)
    with zipfile.ZipFile(output, "w") as target:
        for info in infos:
            if info.filename in entries:
                target.writestr(info, entries[info.filename])
    return output


def test_validates_epubmerge_epub2_with_ncx(epub_factory, tmp_path: Path) -> None:
    output = merged_epub(epub_factory, tmp_path)
    validate_epub(output)

    with zipfile.ZipFile(output) as archive:
        infos = archive.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert not infos[0].extra
        opf = etree.fromstring(archive.read("content.opf"))
        assert opf.xpath("string(//*[local-name()='spine']/@toc)")
        assert "toc.ncx" in archive.namelist()


def test_rejects_missing_anchor(epub_factory, tmp_path: Path) -> None:
    output = merged_epub(epub_factory, tmp_path)
    replacement = tmp_path / "broken.epub"

    def mutate(entries: dict[str, bytes]) -> None:
        chapter = next(name for name in entries if name.endswith("chapter.xhtml"))
        entries[chapter] = entries[chapter].replace(b'href="#note"', b'href="#missing"')

    _rewrite_epub(output, replacement, mutate)

    with pytest.raises(ValidationError, match="missing anchor"):
        validate_epub(replacement)


def _empty_ncx(entries: dict[str, bytes]) -> None:
    root = etree.fromstring(entries["toc.ncx"])
    nav_map = root.find(f"{{{NCX_NS}}}navMap")
    assert nav_map is not None
    for point in nav_map.findall(f"{{{NCX_NS}}}navPoint"):
        nav_map.remove(point)
    entries["toc.ncx"] = etree.tostring(root)


def _invalid_ncx_target(entries: dict[str, bytes]) -> None:
    root = etree.fromstring(entries["toc.ncx"])
    content = root.find(f".//{{{NCX_NS}}}content")
    assert content is not None
    content.set("src", "missing.xhtml")
    entries["toc.ncx"] = etree.tostring(root)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda entries: entries.pop("1/OEBPS/images/cover.png"),
            "missing manifest resource",
        ),
        (
            lambda entries: _empty_ncx(entries),
            "empty table of contents",
        ),
        (
            lambda entries: _invalid_ncx_target(entries),
            "NCX refers",
        ),
    ],
)
def test_rejects_incomplete_epubmerge_result(
    epub_factory, tmp_path: Path, mutate, message: str
) -> None:
    output = merged_epub(epub_factory, tmp_path)
    replacement = _rewrite_epub(output, tmp_path / "broken.epub", mutate)

    with pytest.raises(ValidationError, match=message):
        validate_epub(replacement)


def test_rejects_corrupt_epub(epub_factory, tmp_path: Path) -> None:
    broken = tmp_path / "broken.epub"
    broken.write_bytes(b"not a ZIP archive")

    with pytest.raises(ValidationError, match="cannot validate EPUB"):
        validate_epub(broken)


def _check_with_epubcheck(output: Path) -> None:
    result = subprocess.run(
        ["java", "-jar", os.environ["EPUBCHECK"], str(output)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not os.environ.get("EPUBCHECK"), reason="EPUBCheck is installed in CI")
def test_epubcheck_accepts_control_book(epub_factory, tmp_path: Path) -> None:
    control = epub_factory(2)
    _check_with_epubcheck(control)
    output = tmp_path / "merged.epub"
    merge_epubs(output, (control,), "Collection")
    _check_with_epubcheck(output)


@pytest.mark.skipif(
    not os.environ.get("FBC_INTEGRATION") or not os.environ.get("EPUBCHECK"),
    reason="requires pinned fbc and EPUBCheck",
)
def test_epubcheck_accepts_fbc_epubmerge_result(tmp_path: Path) -> None:
    source = tmp_path / "source.fb2"
    source.write_bytes(Path("tests/fixtures/book.fb2").read_bytes())
    converted = tmp_path / "converted.epub"
    output = tmp_path / "merged.epub"

    FB2Converter().convert(source, converted)
    merge_epubs(output, (converted,), "FB2 fixture")

    _check_with_epubcheck(output)
