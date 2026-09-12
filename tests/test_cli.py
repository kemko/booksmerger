from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bookmerger.cli import Command, main, output_filename, parse_args


def test_positional_urls_keep_order_and_duplicates() -> None:
    command = parse_args(["--title", " Collection ", "one", "two", "one"])

    assert command == Command("Collection", ("one", "two", "one"), False)


def test_input_file_keeps_order_and_duplicates(tmp_path: Path) -> None:
    sources = tmp_path / "sources.txt"
    sources.write_text(
        "https://example.test/a.fb2\n\nhttps://example.test/a.fb2\n", encoding="utf-8"
    )

    command = parse_args(["--title", "Collection", "--input-file", str(sources)])

    assert command.sources == ("https://example.test/a.fb2", "https://example.test/a.fb2")


@pytest.mark.parametrize(
    ("arguments", "title"), [(["--title", "Title", "url"], "Title"), (["url"], None)]
)
def test_title_is_optional(arguments: list[str], title: str | None) -> None:
    command = parse_args(arguments)

    assert command.title == title


def test_output_option_is_rejected() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--output", "out.epub", "url"])


def test_help_does_not_include_output(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    assert "--output" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("title", "filename"),
    [
        ("Сборник — Автор", "Сборник — Автор.epub"),
        (" ../CON. ", "Сборник.epub"),
        ("con .txt", "Сборник.epub"),
        ("a/b\\c:*?", "a b c.epub"),
        ("я" * 150, "я" * 100 + ".epub"),
    ],
)
def test_output_filename_is_portable_and_keeps_utf8_boundaries(title: str, filename: str) -> None:
    assert output_filename(title) == filename


def test_main_reports_short_progress(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_assemble(command: Command) -> Path:
        assert command.title == "Collection"
        logging.getLogger("bookmerger.cli").info("Downloading sources")
        return Path.cwd() / "Collection.epub"

    monkeypatch.setattr("bookmerger.cli.assemble", fake_assemble)

    assert main(["--title", "Collection", "https://example.test/book"]) == 0
    assert "Downloading sources" in capsys.readouterr().err


def test_main_rejects_existing_output_before_downloading(tmp_path, capsys, monkeypatch):
    output = tmp_path / "Collection.epub"
    output.write_bytes(b"existing file")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "bookmerger.cli.Downloader.download_all", lambda *args: pytest.fail("download started")
    )
    assert main(["--title", "Collection", "https://example.test/book"]) == 1
    diagnostic = capsys.readouterr().err
    assert "bookmerger:" in diagnostic
    assert "Traceback" not in diagnostic
    assert output.read_bytes() == b"existing file"


@pytest.mark.parametrize(
    ("failure", "message"),
    [("encoding", "surrogates not allowed"), ("cwd", "current directory is missing")],
)
def test_main_reports_output_path_errors_before_downloading(failure, message, monkeypatch, capsys):
    title = "\udcff" if failure == "encoding" else "Collection"
    if failure == "cwd":

        def missing_cwd():
            raise FileNotFoundError("current directory is missing")

        monkeypatch.setattr("bookmerger.cli.Path.cwd", missing_cwd)
    monkeypatch.setattr(
        "bookmerger.cli.Downloader.download_all", lambda *args: pytest.fail("download started")
    )

    assert main(["--title", title, "https://example.test/book"]) == 1
    diagnostic = capsys.readouterr().err
    assert "bookmerger:" in diagnostic
    assert message in diagnostic
    assert "Traceback" not in diagnostic


def test_main_configures_one_handler_and_verbose_logging(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_assemble(command: Command) -> Path:
        logging.getLogger("bookmerger.cli").info("Downloading sources")
        return Path.cwd() / f"{command.title}.epub"

    monkeypatch.setattr("bookmerger.cli.assemble", fake_assemble)

    assert main(["--title", "Collection", "https://example.test/book"]) == 0
    assert (
        main(
            [
                "--verbose",
                "--title",
                "Collection",
                "https://example.test/book",
            ]
        )
        == 0
    )

    diagnostics = capsys.readouterr().err
    assert diagnostics.count("Downloading sources") == 2
    assert diagnostics.count("Saved EPUB") == 2


def test_assemble_logs_ordered_stages(
    epub_factory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from test_pipeline import LocalDownloader

    monkeypatch.chdir(tmp_path)
    command = Command("Collection", ("one",), False)
    logger = logging.getLogger("bookmerger")
    handlers, propagate = logger.handlers[:], logger.propagate
    logger.handlers.clear()
    logger.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="bookmerger.cli"):
            from bookmerger.cli import assemble

            assemble(command, downloader=LocalDownloader((epub_factory(3),)))
    finally:
        logger.handlers[:] = handlers
        logger.propagate = propagate

    messages = [record.message for record in caplog.records if record.name == "bookmerger.cli"]
    assert [message for message in messages if message.startswith("Starting ")] == [
        "Starting downloading sources",
        "Starting staging EPUB 1/1",
        "Starting determining collection title",
        "Starting building collection",
        "Starting packaging EPUB",
        "Starting validating EPUB",
        "Starting saving EPUB",
    ]
    assert any(message.startswith("Finished saving EPUB in") for message in messages)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--title", "Collection"],
        ["--title", "", "https://example.test/book"],
        ["--title", "Collection", "--input-file", "missing.txt"],
    ],
)
def test_empty_or_unreadable_input_is_rejected(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(arguments)


def test_input_file_and_positional_urls_are_exclusive(tmp_path: Path) -> None:
    sources = tmp_path / "sources.txt"
    sources.write_text("https://example.test/a.fb2\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        parse_args(["--title", "Collection", "--input-file", str(sources), "other"])
