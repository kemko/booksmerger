from __future__ import annotations

import hashlib
import io
import threading
import time
import zipfile
from importlib import resources
from pathlib import Path

import pytest

from bookmerger.converter import (
    RELEASE,
    FB2Converter,
    FbcError,
    FbcInstaller,
    _platform_key,
    fb2_images,
)


def fbc_archive(script: str) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        archive.writestr("fbc", script)
    return result.getvalue()


def fake_fbc(tmp_path: Path, script: str) -> Path:
    executable = tmp_path / "fbc"
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o700)
    return executable


def test_release_has_hash_pinned_supported_platforms() -> None:
    assert RELEASE.version == "1.7.0"
    assert set(RELEASE.assets) == {
        "darwin-amd64",
        "darwin-arm64",
        "linux-amd64",
        "linux-arm64",
        "windows-amd64",
        "windows-arm64",
    }
    assert all(
        len(item.sha256) == 64 and item.url.startswith("https://github.com/")
        for item in RELEASE.assets.values()
    )


def test_bundled_configuration_preserves_epub_content() -> None:
    config = resources.files("bookmerger.data").joinpath("fb2cng.yaml").read_text()

    assert "toc_type: normal" in config
    assert "optimize: false" in config
    assert "scale_factor: 1.0" in config
    assert "generate: false" in config
    assert "resize: none" in config
    assert "mode: default" in config
    assert "enable: true" in config


def test_rejects_unsupported_platform() -> None:
    with pytest.raises(FbcError, match="does not support"):
        _platform_key("plan9", "mips")


def test_installs_verified_binary_once_for_concurrent_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = "#!/usr/bin/env python3\nimport sys\nprint('fbc version 1.7.0')\n"
    archive = fbc_archive(script)
    key = _platform_key()
    asset = RELEASE.assets[key]
    monkeypatch.setitem(
        RELEASE.assets, key, type(asset)(asset.url, hashlib.sha256(archive).hexdigest())
    )
    monkeypatch.setattr("bookmerger.converter.shutil.which", lambda _: None)
    calls = 0
    lock = threading.Lock()

    def fetch(_: str) -> bytes:
        nonlocal calls
        with lock:
            calls += 1
        return archive

    installer = FbcInstaller(tmp_path / "cache", fetch)
    results: list[Path] = []
    threads = [threading.Thread(target=lambda: results.append(installer.find())) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == 1
    assert len(results) == 4
    assert len(set(results)) == 1
    assert results[0].is_file()


def test_rejects_archive_with_bad_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bookmerger.converter.shutil.which", lambda _: None)
    with pytest.raises(FbcError, match="SHA-256"):
        FbcInstaller(tmp_path, lambda _: b"not the expected archive").find()


def test_rejects_unsafe_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("../fbc", b"bad")
    key = _platform_key()
    asset = RELEASE.assets[key]
    payload = data.getvalue()
    monkeypatch.setitem(
        RELEASE.assets, key, type(asset)(asset.url, hashlib.sha256(payload).hexdigest())
    )
    monkeypatch.setattr("bookmerger.converter.shutil.which", lambda _: None)

    with pytest.raises(FbcError, match="unsafe path"):
        FbcInstaller(tmp_path, lambda _: payload).find()


def output_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            b'<rootfiles><rootfile full-path="EPUB/package.opf"/>'
            b"</rootfiles></container>",
        )
        archive.writestr(
            "EPUB/package.opf",
            b'<package xmlns="http://www.idpf.org/2007/opf"><metadata '
            b'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Fixture</dc:title>'
            b'</metadata><manifest><item id="cover" href="cover.png" '
            b'media-type="image/png"/></manifest></package>',
        )
        archive.writestr("EPUB/cover.png", b"changed")


def test_converter_restores_images_and_translator_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = tmp_path / "generated.epub"
    output_epub(generated)
    monkeypatch.setenv("FBC_TEST_OUTPUT", str(generated))
    binary = fake_fbc(
        tmp_path,
        "#!/usr/bin/env python3\nimport os, shutil, sys\n"
        "if '--version' in sys.argv:\n print('fbc version 1.7.0')\n"
        "else:\n shutil.copy(os.environ['FBC_TEST_OUTPUT'], "
        "sys.argv[sys.argv.index('--output-file') + 1])\n",
    )
    output = tmp_path / "result.epub"

    metadata = FB2Converter(
        FbcInstaller(tmp_path / "cache", lambda _: b""), config=tmp_path / "config.yaml"
    )
    metadata.installer.find = lambda: binary  # type: ignore[method-assign]
    metadata.config.write_text("version: 1\n", encoding="utf-8")
    import tempfile

    temporary_directory = tempfile.TemporaryDirectory
    work_parents = []

    def workspace(*args, **kwargs):
        work_parents.append(kwargs.get("dir"))
        return temporary_directory(*args, **kwargs)

    monkeypatch.setattr("bookmerger.converter.tempfile.TemporaryDirectory", workspace)
    result = metadata.convert(Path("tests/fixtures/book.fb2"), output)
    assert work_parents == [output.parent]

    assert result.translators == ("Trudy Translator",)
    original = fb2_images(Path("tests/fixtures/book.fb2"))[0].data
    with zipfile.ZipFile(output) as archive:
        assert archive.read("EPUB/cover.png") == original
        package = archive.read("EPUB/package.opf")
    assert b"Trudy Translator" in package
    assert b">trl</" in package


def test_converter_reports_process_failure(tmp_path: Path) -> None:
    binary = fake_fbc(
        tmp_path,
        "#!/usr/bin/env python3\nimport sys\nprint('broken', file=sys.stderr)\nsys.exit(2)\n",
    )
    converter = FB2Converter(
        FbcInstaller(tmp_path / "cache", lambda _: b""), config=tmp_path / "config.yaml"
    )
    converter.installer.find = lambda: binary  # type: ignore[method-assign]
    converter.config.write_text("version: 1\n", encoding="utf-8")

    with pytest.raises(FbcError, match="broken"):
        converter.convert(Path("tests/fixtures/book.fb2"), tmp_path / "out.epub")


def test_converter_reports_timeout(tmp_path: Path) -> None:
    binary = fake_fbc(tmp_path, "#!/usr/bin/env python3\nimport time\ntime.sleep(1)\n")
    converter = FB2Converter(
        FbcInstaller(tmp_path / "cache", lambda _: b""), config=tmp_path / "config.yaml"
    )
    converter.installer.find = lambda: binary  # type: ignore[method-assign]
    converter.config.write_text("version: 1\n", encoding="utf-8")

    started = time.monotonic()
    with pytest.raises(FbcError, match="timed out"):
        converter.convert(Path("tests/fixtures/book.fb2"), tmp_path / "out.epub", timeout=0.01)
    assert time.monotonic() - started < 0.5


def test_restores_multiple_changed_images_by_filename(tmp_path: Path) -> None:
    from bookmerger.converter import FB2Image, FB2Metadata, _restore_epub

    output = tmp_path / "converted.epub"
    output_epub(output)
    with zipfile.ZipFile(output) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries["EPUB/package.opf"] = entries["EPUB/package.opf"].replace(
        b'<item id="cover" href="cover.png" media-type="image/png"/>',
        b'<item id="one" href="one.png" media-type="image/png"/>'
        b'<item id="two" href="two.png" media-type="image/png"/>',
    )
    entries["EPUB/one.png"] = b"converted one"
    entries["EPUB/two.png"] = b"converted two"
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    _restore_epub(
        output,
        (
            FB2Image("one.png", "image/png", b"original one"),
            FB2Image("two.png", "image/png", b"original two"),
        ),
        FB2Metadata("Test", ()),
    )
    with zipfile.ZipFile(output) as archive:
        assert archive.read("EPUB/one.png") == b"original one"
        assert archive.read("EPUB/two.png") == b"original two"
