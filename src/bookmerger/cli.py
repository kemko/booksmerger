"""Command-line argument handling."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from bookmerger.collection import build_collection, generated_title
from bookmerger.converter import FB2Converter
from bookmerger.download import Downloader, _safe_detail
from bookmerger.epub import stage_epub, write_epub
from bookmerger.validate import validate_epub


@dataclass(frozen=True)
class Command:
    """Validated command-line input for a collection build."""

    title: str | None
    sources: tuple[str, ...]
    overwrite: bool
    verbose: bool = False


class BuildError(RuntimeError):
    """The collection could not be built without risking the existing output."""


LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


class _Stderr:
    def write(self, message: str) -> int:
        return sys.stderr.write(message)

    def flush(self) -> None:
        sys.stderr.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create one EPUB from FB2 and EPUB URLs.")
    parser.add_argument(
        "--title", help="Collection title and output filename (saved in the current directory)"
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output file")
    parser.add_argument("--verbose", action="store_true", help="Show debug diagnostics")
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("urls", nargs="*", metavar="URL", help="Source URLs in collection order")
    sources.add_argument("--input-file", type=Path, help="UTF-8 file with one source URL per line")
    return parser


def _sources_from_file(path: Path) -> tuple[str, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise argparse.ArgumentTypeError(f"cannot read input file {path}: {error}") from error
    return tuple(line.strip() for line in lines if line.strip())


def parse_args(argv: Sequence[str] | None = None) -> Command:
    parser = build_parser()
    namespace = parser.parse_args(argv)
    title = namespace.title.strip() if namespace.title is not None else None
    if namespace.title is not None and not title:
        parser.error("--title must not be empty")
    try:
        sources = (
            _sources_from_file(namespace.input_file)
            if namespace.input_file
            else tuple(namespace.urls)
        )
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    if not sources:
        parser.error("provide at least one URL")
    return Command(title, sources, namespace.overwrite, namespace.verbose)


_FORBIDDEN_FILENAME_CHARACTERS = '<>:"/\\|?*'
_RESERVED_FILENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_MAX_OUTPUT_STEM_BYTES = 200


def output_filename(title: str) -> str:
    """Return a portable EPUB filename without shortening the embedded title."""
    stem = "".join(
        " " if character in _FORBIDDEN_FILENAME_CHARACTERS or ord(character) < 32 else character
        for character in title
    ).strip(". ")
    if not stem or stem.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_FILENAMES:
        stem = "Сборник"
    encoded = stem.encode("utf-8")[:_MAX_OUTPUT_STEM_BYTES]
    stem = encoded.decode("utf-8", errors="ignore").rstrip(". ") or "Сборник"
    return f"{stem}.epub"


def _configure_logging(verbose: bool) -> None:
    logger = logging.getLogger("bookmerger")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    handler = next((item for item in logger.handlers if getattr(item, "_bookmerger", False)), None)
    if handler is None:
        handler = logging.StreamHandler(_Stderr())
        handler._bookmerger = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))


def _stage(name: str, operation: Callable[[], T]) -> T:
    started = time.monotonic()
    LOGGER.info("Starting %s", name)
    try:
        result = operation()
    except Exception as error:
        elapsed = time.monotonic() - started
        detail = _safe_detail(error) or type(error).__name__
        LOGGER.error("%s failed after %.2fs: %s", name, elapsed, detail)
        raise BuildError(f"{detail} ({name})") from error
    LOGGER.info("Finished %s in %.2fs", name, time.monotonic() - started)
    return result


def _prepare_output(output: Path, overwrite: bool) -> None:
    if output.exists() and not overwrite:
        raise BuildError(f"output already exists: {output} (use --overwrite to replace it)")
    output.parent.mkdir(parents=True, exist_ok=True)


def assemble(
    command: Command,
    *,
    downloader: Downloader | None = None,
    converter: FB2Converter | None = None,
) -> Path:
    """Build and atomically publish one validated collection EPUB."""
    downloader = downloader or Downloader()
    converter = converter or FB2Converter()
    temporary: Path | None = None
    try:
        output = Path.cwd() / output_filename(command.title) if command.title else None
        if output is not None:
            _prepare_output(output, command.overwrite)
        with tempfile.TemporaryDirectory(prefix=".bookmerger-") as work_name:
            work = Path(work_name)
            sources = _stage(
                "downloading sources",
                lambda: downloader.download_all(command.sources, work / "sources"),
            )
            converted: list[Path] = []
            for number, source in enumerate(sources, 1):
                if source.format == "epub":
                    converted.append(source.path)
                    continue
                target = work / "converted" / f"book-{number:04d}.epub"
                _stage(
                    f"converting book {number}/{len(sources)}",
                    lambda source=source, target=target: converter.convert(source.path, target),
                )
                converted.append(target)
            staging = work / "staging"
            books = tuple(
                _stage(
                    f"staging EPUB {number}/{len(converted)}",
                    lambda source=source, number=number: stage_epub(
                        source, staging, number, downloader=downloader
                    ),
                )
                for number, source in enumerate(converted, 1)
            )
            title = _stage(
                "determining collection title", lambda: command.title or generated_title(books)
            )
            if output is None:
                output = Path.cwd() / output_filename(title)
            _prepare_output(output, command.overwrite)
            _stage("building collection", lambda: build_collection(title, books, staging))
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{output.stem}-", suffix=".epub", dir=output.parent
            )
            os.close(descriptor)
            temporary = Path(temp_name)
            _stage("packaging EPUB", lambda: write_epub(staging, temporary))
            _stage("validating EPUB", lambda: validate_epub(temporary, staging))

            def publish() -> None:
                if command.overwrite:
                    os.replace(temporary, output)
                    return
                try:
                    os.link(temporary, output)
                except FileExistsError:
                    raise BuildError(
                        f"output already exists: {output} (use --overwrite to replace it)"
                    ) from None
                temporary.unlink()

            _stage("saving EPUB", publish)
            temporary = None
    except BuildError:
        raise
    except Exception as error:
        raise BuildError(_safe_detail(error) or type(error).__name__) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    assert output is not None
    return output


def main(argv: Sequence[str] | None = None) -> int:
    """Run the complete collection pipeline and print concise diagnostics."""
    command = parse_args(argv)
    _configure_logging(command.verbose)
    try:
        output = assemble(command)
    except BuildError as error:
        LOGGER.error("bookmerger: %s", error)
        return 1
    LOGGER.info("Saved EPUB: %s", output)
    return 0
