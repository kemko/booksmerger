from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from bookmerger.collection import build_collection
from bookmerger.epub import stage_epub, write_epub
from bookmerger.validate import ValidationError, validate_epub


def assembled_epub(epub_factory, tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    books = (stage_epub(epub_factory(2), staging, 1), stage_epub(epub_factory(3), staging, 2))
    build_collection("Collection", books, staging)
    output = tmp_path / "collection.epub"
    write_epub(staging, output)
    return output


def test_validates_container_order_and_unmodified_images(epub_factory, tmp_path: Path) -> None:
    output = assembled_epub(epub_factory, tmp_path)
    validate_epub(output, tmp_path / "staging")

    with zipfile.ZipFile(output) as archive:
        infos = archive.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert not infos[0].extra
        assert [entry.filename for entry in infos[1:]] == sorted(
            entry.filename for entry in infos[1:]
        )


def test_rejects_missing_anchor(epub_factory, tmp_path: Path) -> None:
    output = assembled_epub(epub_factory, tmp_path)
    replacement = tmp_path / "broken.epub"
    with zipfile.ZipFile(output) as source, zipfile.ZipFile(replacement, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename.endswith("chapter.xhtml"):
                data = data.replace(b'href="#note"', b'href="#missing"')
            target.writestr(info, data)

    with pytest.raises(ValidationError, match="missing anchor"):
        validate_epub(replacement)


@pytest.mark.skipif(not os.environ.get("EPUBCHECK"), reason="EPUBCheck is installed in CI")
def test_epubcheck_accepts_control_book(epub_factory, tmp_path: Path) -> None:
    output = assembled_epub(epub_factory, tmp_path)
    result = subprocess.run(
        ["java", "-jar", os.environ["EPUBCHECK"], str(output)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
