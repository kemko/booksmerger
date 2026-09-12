from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bookmerger.cli import Command, main, output_filename, parse_args


def test_positional_urls_keep_order_and_duplicates() -> None:
    command = parse_args(
        ["--title", " Collection ", "--output", "collection.epub", "one", "two", "one"]
    )

    assert command == Command("Collection", Path("collection.epub"), ("one", "two", "one"), False)


def test_input_file_keeps_order_and_duplicates(tmp_path: Path) -> None:
    sources = tmp_path / "sources.txt"
    sources.write_text(
        "https://example.test/a.fb2\n\nhttps://example.test/a.fb2\n", encoding="utf-8"
    )

    command = parse_args(
        ["--title", "Collection", "--output", "out.epub", "--input-file", str(sources)]
    )

    assert command.sources == ("https://example.test/a.fb2", "https://example.test/a.fb2")


@pytest.mark.parametrize(
    ("arguments", "title", "output"),
    [
        (["--title", "Title", "--output", "out.epub", "url"], "Title", Path("out.epub")),
        (["--title", "Title", "url"], "Title", None),
        (["--output", "out.epub", "url"], None, Path("out.epub")),
        (["url"], None, None),
    ],
)
def test_title_and_output_are_independently_optional(
    arguments: list[str], title: str | None, output: Path | None
) -> None:
    command = parse_args(arguments)

    assert (command.title, command.output) == (title, output)


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
        return command.output

    monkeypatch.setattr("bookmerger.cli.assemble", fake_assemble)

    assert (
        main(["--title", "Collection", "--output", "collection.epub", "https://example.test/book"])
        == 0
    )
    assert "Downloading sources" in capsys.readouterr().err


def test_main_reports_invalid_output_parent_before_downloading(tmp_path, capsys, monkeypatch):
    parent = tmp_path / "file"
    parent.write_bytes(b"existing file")
    monkeypatch.setattr(
        "bookmerger.cli.Downloader.download_all", lambda *args: pytest.fail("download started")
    )
    assert main(["--output", str(parent / "out.epub"), "https://example.test/book"]) == 1
    diagnostic = capsys.readouterr().err
    assert "bookmerger:" in diagnostic
    assert "Traceback" not in diagnostic
    assert parent.read_bytes() == b"existing file"


def test_main_configures_one_handler_and_verbose_logging(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_assemble(command: Command) -> Path:
        logging.getLogger("bookmerger.cli").info("Downloading sources")
        return command.output

    monkeypatch.setattr("bookmerger.cli.assemble", fake_assemble)

    assert main(["--title", "Collection", "--output", "one.epub", "https://example.test/book"]) == 0
    assert (
        main(
            [
                "--verbose",
                "--title",
                "Collection",
                "--output",
                "two.epub",
                "https://example.test/book",
            ]
        )
        == 0
    )

    diagnostics = capsys.readouterr().err
    assert diagnostics.count("Downloading sources") == 2
    assert diagnostics.count("Saved EPUB") == 2


def test_assemble_logs_ordered_stages(
    epub_factory, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from test_pipeline import LocalDownloader

    command = Command("Collection", tmp_path / "collection.epub", ("one",), False)
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
        ["--title", "Collection", "--output", "out.epub"],
        ["--title", "", "--output", "out.epub", "https://example.test/book"],
        ["--title", "Collection", "--output", "out.epub", "--input-file", "missing.txt"],
    ],
)
def test_empty_or_unreadable_input_is_rejected(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(arguments)


def test_input_file_and_positional_urls_are_exclusive(tmp_path: Path) -> None:
    sources = tmp_path / "sources.txt"
    sources.write_text("https://example.test/a.fb2\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        parse_args(
            ["--title", "Collection", "--output", "out.epub", "--input-file", str(sources), "other"]
        )
