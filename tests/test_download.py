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


def test_reuses_validated_source_cache_across_downloaders_and_positions(tmp_path: Path) -> None:
    requests = 0
    cache = tmp_path / "cache"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return response(request, FB2)

    url = "https://user:pass@example.test/book?edition=1#chapter"
    first = Downloader(client=client(httpx.MockTransport(handler)), cache_directory=cache)
    second = Downloader(client=client(httpx.MockTransport(handler)), cache_directory=cache)

    first_result = first.download_all([url, url], tmp_path / "first")
    second_result = second.download(url, tmp_path / "second")

    assert requests == 1
    assert [item.path.read_bytes() for item in first_result] == [FB2, FB2]
    assert second_result.path.read_bytes() == FB2
    assert first_result[0].path != first_result[1].path
    assert all("example" not in path.name for path in cache.iterdir())


def test_default_source_cache_uses_xdg_cache_home(tmp_path: Path) -> None:
    assert Downloader().cache_directory == tmp_path / "cache" / "bookmerger" / "sources"


def test_cache_keeps_epub_and_wrapped_fb2_response_bodies(tmp_path: Path) -> None:
    epub = archive(
        {
            "mimetype": b"application/epub+zip",
            "META-INF/container.xml": (
                b'<container xmlns="urn:oasis:tc:opendocument:xmlns:container"/>'
            ),
        }
    )
    wrapped_fb2 = archive({"book.fb2": FB2})
    cache = tmp_path / "cache"
    payloads = iter([epub, wrapped_fb2])
    first = Downloader(
        client=client(httpx.MockTransport(lambda request: response(request, next(payloads)))),
        cache_directory=cache,
    )
    cached = Downloader(
        client=client(httpx.MockTransport(lambda request: pytest.fail("network used"))),
        cache_directory=cache,
    )

    result = first.download_all(
        ["https://example.test/a", "https://example.test/b"], tmp_path / "first"
    )
    repeat = cached.download_all(
        ["https://example.test/a", "https://example.test/b"], tmp_path / "second"
    )

    assert [item.format for item in result] == ["epub", "fb2"]
    assert [item.path.read_bytes() for item in repeat] == [epub, FB2]


def test_cache_key_includes_query_and_ignores_fragment(tmp_path: Path) -> None:
    requests: list[str] = []
    cache = tmp_path / "cache"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return response(request, FB2)

    downloader = Downloader(client=client(httpx.MockTransport(handler)), cache_directory=cache)
    downloader.download("https://example.test/book?edition=1#one", tmp_path / "one")
    downloader.download("https://example.test/book?edition=2", tmp_path / "two")
    downloader.download("https://example.test/book?edition=1#two", tmp_path / "three")

    assert len(requests) == 2


def test_corrupt_or_limited_cache_is_re_downloaded(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return response(request, FB2)

    downloader = Downloader(client=client(httpx.MockTransport(handler)), cache_directory=cache)
    url = "https://example.test/book"
    cache.mkdir()
    downloader._cache_path(url).write_bytes(HTML)
    downloader.download(url, tmp_path / "first")
    downloader._cache_path(url).write_bytes(FB2 + b" " * 20)
    limited = Downloader(
        limits=DownloadLimits(max_download_bytes=len(FB2)),
        client=client(httpx.MockTransport(handler)),
        cache_directory=cache,
    )
    limited.download(url, tmp_path / "second")
    downloader._cache_path(url).write_bytes(archive({"book.fb2": FB2}))
    zip_limited = Downloader(
        limits=DownloadLimits(max_zip_entries=1, max_zip_uncompressed_bytes=1),
        client=client(httpx.MockTransport(handler)),
        cache_directory=cache,
    )
    zip_limited.download(url, tmp_path / "third")

    assert requests == 3


def test_failed_cache_publication_leaves_no_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    downloader = Downloader(
        client=client(httpx.MockTransport(lambda request: response(request, FB2))),
        cache_directory=cache,
    )

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("cache is unavailable")

    monkeypatch.setattr("bookmerger.download.os.replace", fail_replace)
    with pytest.warns(RuntimeWarning, match="cannot use source cache"):
        result = downloader.download("https://example.test/book", tmp_path / "work")

    assert result.path.read_bytes() == FB2
    assert not list(cache.iterdir())


def test_cache_errors_warn_and_later_download_failure_keeps_cached_source(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    first = Downloader(
        client=client(httpx.MockTransport(lambda request: response(request, FB2))),
        cache_directory=cache,
    )
    first.download("https://example.test/one", tmp_path / "seed")
    contents = next(cache.iterdir()).read_bytes()
    failing = Downloader(
        client=client(httpx.MockTransport(lambda request: response(request, HTML))),
        cache_directory=cache,
    )

    with pytest.raises(DownloadError):
        failing.download_all(
            ["https://example.test/one", "https://example.test/two"], tmp_path / "work"
        )
    assert next(cache.iterdir()).read_bytes() == contents

    broken = Downloader(
        client=client(httpx.MockTransport(lambda request: response(request, FB2))),
        cache_directory=tmp_path / "not-a-directory",
    )
    broken.cache_directory.write_bytes(b"x")
    (tmp_path / "broken").mkdir()
    with pytest.warns(RuntimeWarning, match="cannot use source cache"):
        broken.download("https://example.test/three", tmp_path / "broken")

    assert not list((tmp_path / "broken").glob("*.download"))


def test_removes_prior_sources_when_a_later_source_fails(tmp_path: Path) -> None:
    payloads = iter([FB2, HTML])
    downloader = Downloader(
        client=client(httpx.MockTransport(lambda request: response(request, next(payloads))))
    )

    with pytest.raises(DownloadError):
        downloader.download_all(["https://example.test/one", "https://example.test/two"], tmp_path)

    assert not list(tmp_path.glob("source-*"))
    assert list((tmp_path / "cache" / "bookmerger" / "sources").iterdir())


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
