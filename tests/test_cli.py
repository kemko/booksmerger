from __future__ import annotations

from pathlib import Path

import pytest

from bookmerger.cli import Command, main, parse_args


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


def test_main_reports_short_progress(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_assemble(command: Command, *, progress, **_: object) -> Path:
        assert command.title == "Collection"
        progress("Downloading sources")
        return command.output

    monkeypatch.setattr("bookmerger.cli.assemble", fake_assemble)

    assert (
        main(["--title", "Collection", "--output", "collection.epub", "https://example.test/book"])
        == 0
    )
    assert "Downloading sources" in capsys.readouterr().err


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
