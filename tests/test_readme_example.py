from __future__ import annotations

import os
import subprocess
import sys
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


@contextmanager
def fixture_server(files: dict[str, bytes]) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = files.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_readme_example_generates_output_and_reuses_source_cache(
    epub_factory, tmp_path: Path
) -> None:
    sources = {
        "/book2.epub": epub_factory(2).read_bytes(),
        "/book3.epub": epub_factory(3).read_bytes(),
    }
    output_name = "Сборник — Author 2, Author 3 — Fixture 2, Fixture 3.epub"
    output = tmp_path / output_name
    titled_output = tmp_path / "titled" / "My collection.epub"
    repeated_output = tmp_path / "repeated" / output_name
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COVERAGE_", "COV_CORE_"))
    }

    with fixture_server(sources) as base_url:
        arguments = [f"{base_url}/book2.epub", f"{base_url}/book3.epub"]
        completed = subprocess.run(
            [str(Path(sys.executable).with_name("bookmerger")), *arguments],
            capture_output=True,
            check=False,
            cwd=tmp_path,
            env=environment,
            text=True,
            timeout=30,
        )

    assert completed.returncode == 0, completed.stderr
    assert output.is_file()
    assert len(list((tmp_path / "cache" / "bookmerger" / "sources").iterdir())) == 2

    titled_output.parent.mkdir()
    titled = subprocess.run(
        [
            str(Path(sys.executable).with_name("bookmerger")),
            "--title",
            "My collection",
            *arguments,
        ],
        capture_output=True,
        check=False,
        cwd=titled_output.parent,
        env=environment,
        text=True,
        timeout=30,
    )

    assert titled.returncode == 0, titled.stderr
    assert titled_output.is_file()
    with zipfile.ZipFile(titled_output) as archive:
        assert "My collection" in archive.read("EPUB/package.opf").decode()
        assert "My collection" in archive.read("EPUB/title.xhtml").decode()

    repeated_output.parent.mkdir()
    repeated = subprocess.run(
        [str(Path(sys.executable).with_name("bookmerger")), *arguments],
        capture_output=True,
        check=False,
        cwd=repeated_output.parent,
        env=environment,
        text=True,
        timeout=30,
    )

    assert repeated.returncode == 0, repeated.stderr
    assert repeated_output.is_file()
    assert "Source cache hit" in repeated.stderr
