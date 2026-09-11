"""Command-line argument handling."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Command:
    """Validated command-line input for a collection build."""

    title: str
    output: Path
    sources: tuple[str, ...]
    overwrite: bool


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


def main(argv: Sequence[str] | None = None) -> int:
    """Validate CLI input; downloading is performed by the assembly pipeline."""
    parse_args(argv)
    return 0
