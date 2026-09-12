"""Safe, minimal EpubMerge integration."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from epubmerge.epubmerge import doMerge

from bookmerger.epub import EpubError, EpubPackage, validate_merge_input


class MergeError(RuntimeError):
    """An EPUB could not be prepared for or merged by EpubMerge."""


def merge_epubs(output: Path, sources: Sequence[Path], title: str) -> tuple[EpubPackage, ...]:
    """Validate and merge sources in order without altering their contents."""
    if not sources:
        raise MergeError("no EPUB sources to merge")
    packages: list[EpubPackage] = []
    for number, source in enumerate(sources, 1):
        try:
            packages.append(validate_merge_input(source))
        except EpubError as error:
            raise MergeError(f"source {number}: {error}") from error
    languages = tuple(
        dict.fromkeys(language for package in packages for language in package.metadata.languages)
    ) or ("en",)
    try:
        doMerge(
            str(output),
            [str(source) for source in sources],
            titleopt=title,
            languages=list(languages),
            titlenavpoints=True,
            originalnavpoints=True,
            keepsingletocs=True,
        )
    except Exception as error:
        raise MergeError(f"EpubMerge failed: {error}") from error
    return tuple(packages)
