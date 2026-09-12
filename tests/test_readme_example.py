from __future__ import annotations

import os
import subprocess
import sys
import threading
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


def test_readme_example_runs_through_local_http_server(epub_factory, tmp_path: Path) -> None:
    sources = {
        "/book2.epub": epub_factory(2).read_bytes(),
        "/book3.epub": epub_factory(3).read_bytes(),
    }
    output = tmp_path / "example.epub"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COVERAGE_", "COV_CORE_"))
    }

    with fixture_server(sources) as base_url:
        completed = subprocess.run(
            [
                str(Path(sys.executable).with_name("bookmerger")),
                "--title",
                "Example collection",
                "--output",
                "example.epub",
                f"{base_url}/book2.epub",
                f"{base_url}/book3.epub",
            ],
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
