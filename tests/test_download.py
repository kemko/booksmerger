from __future__ import annotations

import io
import threading
import time
import warnings
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from bookmerger.download import Downloader, DownloadError, DownloadLimits, temporary_downloads

FB2 = b'<?xml version="1.0" encoding="windows-1251"?><FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><description/></FictionBook>'
HTML = b"<html><body>not a book</body></html>"


def response(request: httpx.Request, body: bytes, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=body, request=request)


def archive(entries: dict[str, bytes], *, duplicate: bool = False, symlink: bool = False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, contents in entries.items():
            info = zipfile.ZipInfo(name)
            if symlink:
                info.external_attr = 0o120777 << 16
            bundle.writestr(info, contents)
            if duplicate:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    bundle.writestr(name, contents)
    return output.getvalue()


def client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler, follow_redirects=True, max_redirects=3)


@contextmanager
def local_server() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/book")
                self.end_headers()
                return
            if self.path == "/slow":
                time.sleep(0.1)
            self.send_response(200)
            self.send_header("Content-Length", str(len(FB2)))
            self.end_headers()
            try:
                self.wfile.write(FB2)
            except BrokenPipeError:
                pass

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_downloads_redirected_fb2_without_changing_encoding_or_order(tmp_path: Path) -> None:
    with local_server() as server:
        result = Downloader().download_all([f"{server}/redirect", f"{server}/book"], tmp_path)

    assert [item.format for item in result] == ["fb2", "fb2"]
    assert [item.url for item in result] == [f"{server}/redirect", f"{server}/book"]
    assert result[0].path.read_bytes() == FB2


def test_retries_temporary_error_and_removes_partial_file(tmp_path: Path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return response(request, b"busy", 503) if attempts == 1 else response(request, FB2)

    downloaded = Downloader(client=client(httpx.MockTransport(handler))).download(
        "https://example.test/book?token=secret", tmp_path
    )

    assert attempts == 2
    assert downloaded.path.name == "source-0001.fb2"
    assert not list(tmp_path.glob("*.download"))


def test_removes_prior_sources_when_a_later_source_fails(tmp_path: Path) -> None:
    payloads = iter([FB2, HTML])
    downloader = Downloader(
        client=client(httpx.MockTransport(lambda request: response(request, next(payloads))))
    )

    with pytest.raises(DownloadError):
        downloader.download_all(["https://example.test/one", "https://example.test/two"], tmp_path)

    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (HTML, "unsupported file format"),
        (archive({"one.fb2": FB2, "two.fb2": FB2}), "one FB2 file"),
        (archive({"../book.fb2": FB2}), "unsafe path"),
        (archive({"book.fb2": FB2}, duplicate=True), "duplicate paths"),
        (archive({"book.fb2": FB2}, symlink=True), "symbolic link"),
    ],
)
def test_rejects_invalid_and_unsafe_input(tmp_path: Path, body: bytes, message: str) -> None:
    downloader = Downloader(
        client=client(httpx.MockTransport(lambda request: response(request, body)))
    )

    with pytest.raises(DownloadError, match=message) as error:
        downloader.download("https://user:pass@example.test/book?token=secret", tmp_path)

    assert "secret" not in str(error.value)
    assert "user:pass" not in str(error.value)


def test_accepts_epub_and_single_fb2_zip(tmp_path: Path) -> None:
    epub = archive(
        {
            "mimetype": b"application/epub+zip",
            "META-INF/container.xml": (
                b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"/>'
            ),
            "OPS/package.opf": b"<package/>",
        }
    )
    wrapped_fb2 = archive({"nested/book.fb2": FB2})
    payloads = iter([epub, wrapped_fb2])
    downloader = Downloader(
        client=client(httpx.MockTransport(lambda request: response(request, next(payloads))))
    )

    result = downloader.download_all(["https://example.test/a", "https://example.test/b"], tmp_path)

    assert [item.format for item in result] == ["epub", "fb2"]
    assert result[1].path.read_bytes() == FB2


def test_enforces_download_and_zip_limits(tmp_path: Path) -> None:
    body = FB2 + b" " * 100
    downloader = Downloader(
        limits=DownloadLimits(
            max_download_bytes=len(FB2), max_zip_entries=1, max_zip_uncompressed_bytes=1
        ),
        client=client(httpx.MockTransport(lambda request: response(request, body))),
    )

    with pytest.raises(DownloadError, match="allowed size"):
        downloader.download("https://example.test/book", tmp_path)

    zip_downloader = Downloader(
        limits=DownloadLimits(max_zip_entries=1, max_zip_uncompressed_bytes=1),
        client=client(
            httpx.MockTransport(lambda request: response(request, archive({"book.fb2": FB2})))
        ),
    )
    with pytest.raises(DownloadError, match="expands beyond"):
        zip_downloader.download("https://example.test/book", tmp_path)


def test_rejects_timeout_and_cleans_temporary_directory() -> None:
    with local_server() as server:
        with pytest.raises(DownloadError, match="download failed"):
            with temporary_downloads([f"{server}/slow"], timeout=0.01) as downloaded:
                assert downloaded
