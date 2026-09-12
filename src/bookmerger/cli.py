"""Command-line argument handling."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from bookmerger.collection import build_collection
from bookmerger.converter import FB2Converter
from bookmerger.download import Downloader
from bookmerger.epub import stage_epub, write_epub
from bookmerger.validate import validate_epub


@dataclass(frozen=True)
class Command:
    """Validated command-line input for a collection build."""

    title: str
    output: Path
    sources: tuple[str, ...]
    overwrite: bool


class BuildError(RuntimeError):
    """The collection could not be built without risking the existing output."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create one EPUB from FB2 and EPUB URLs.")
    parser.add_argument("--title", required=True, help="Collection title")
    parser.add_argument("--output", required=True, type=Path, help="Output EPUB path")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output file")
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
    title = namespace.title.strip()
    if not title:
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
    return Command(title, namespace.output, sources, namespace.overwrite)


def assemble(
    command: Command,
    *,
    downloader: Downloader | None = None,
    converter: FB2Converter | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Build and atomically publish one validated collection EPUB."""
    output = command.output
    if output.exists() and not command.overwrite:
        raise BuildError(f"output already exists: {output} (use --overwrite to replace it)")
    output.parent.mkdir(parents=True, exist_ok=True)
    downloader = downloader or Downloader()
    converter = converter or FB2Converter()
    temporary: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix=f".{output.stem}-", dir=output.parent) as work_name:
            work = Path(work_name)
            if progress:
                progress("Downloading sources")
            sources = downloader.download_all(command.sources, work / "sources")
            converted: list[Path] = []
            for number, source in enumerate(sources, 1):
                if source.format == "epub":
                    converted.append(source.path)
                    continue
                if progress:
                    progress(f"Converting book {number}")
                target = work / "converted" / f"book-{number:04d}.epub"
                converter.convert(source.path, target)
                converted.append(target)
            if progress:
                progress("Building collection")
            staging = work / "staging"
            books = tuple(
                stage_epub(source, staging, number, downloader=downloader)
                for number, source in enumerate(converted, 1)
            )
            build_collection(command.title, books, staging)
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{output.stem}-", suffix=".epub", dir=output.parent
            )
            os.close(descriptor)
            temporary = Path(temp_name)
            write_epub(staging, temporary)
            validate_epub(temporary, staging)
            if command.overwrite:
                os.replace(temporary, output)
            else:
                try:
                    os.link(temporary, output)
                except FileExistsError:
                    raise BuildError(
                        f"output already exists: {output} (use --overwrite to replace it)"
                    ) from None
                temporary.unlink()
            temporary = None
    except BuildError:
        raise
    except Exception as error:
        raise BuildError(str(error)) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def main(argv: Sequence[str] | None = None) -> int:
    """Run the complete collection pipeline and print concise diagnostics."""
    command = parse_args(argv)
    try:
        assemble(command, progress=lambda message: print(message, file=sys.stderr))
    except BuildError as error:
        print(f"bookmerger: {error}", file=sys.stderr)
        return 1
    return 0
