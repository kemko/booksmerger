from __future__ import annotations

import os
import subprocess

import httpx
from test_pipeline import LocalDownloader
from test_review_regressions import contents

from bookmerger.cli import Command, assemble, main
from bookmerger.download import Downloader


def test_malformed_remote_xml_redacts_cli_diagnostic(
    epub_factory, edit_epub, tmp_path, monkeypatch, capsys
):
    source = edit_epub(
        epub_factory(3),
        {
            "OEBPS/images/diagram.svg": (
                b'<svg xmlns="http://www.w3.org/2000/svg">'
                b'<use href="https://private-user:private-password@assets.test/private.svg'
                b'?token=SECRET#paint"/></svg>'
            )
        },
    )
    output = tmp_path / "collection.epub"
    output.write_bytes(b"previous collection")
    monkeypatch.chdir(tmp_path)

    def handler(request):
        if request.url.host == "source.test":
            return httpx.Response(200, content=source.read_bytes())
        return httpx.Response(200, content=b"<svg", headers={"content-type": "image/svg+xml"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr("bookmerger.cli.Downloader", lambda: Downloader(client=client))
        assert (
            main(
                [
                    "--title",
                    "collection",
                    "--overwrite",
                    "https://source.test/book",
                ]
            )
            == 1
        )
    error = capsys.readouterr().err
    assert "invalid XML resource: https://assets.test/private.svg" in error
    for secret in ("private-user", "private-password", "token", "SECRET"):
        assert secret not in error
    assert output.read_bytes() == b"previous collection"
    assert not list(tmp_path.glob(".collection-*"))


def test_temporal_media_fragment_survives_assembly(epub_factory, edit_epub, monkeypatch, tmp_path):
    source = epub_factory(3)
    original = contents(source)
    appendix = original["OEBPS/text/appendix.xhtml"].replace(
        b"../media/audio.mp3", b"../media/audio.mp3#t=0,1"
    )
    edit_epub(source, {"OEBPS/text/appendix.xhtml": appendix})
    output = tmp_path / "collection.epub"
    monkeypatch.chdir(tmp_path)
    assemble(Command("collection", ("one",), False), downloader=LocalDownloader((source,)))
    result = contents(output)
    assert result["books/0001/OEBPS/text/appendix.xhtml"] == appendix
    assert result["books/0001/OEBPS/media/audio.mp3"] == original["OEBPS/media/audio.mp3"]
    if os.environ.get("EPUBCHECK"):
        checked = subprocess.run(
            ["java", "-jar", os.environ["EPUBCHECK"], str(output)], capture_output=True, text=True
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
