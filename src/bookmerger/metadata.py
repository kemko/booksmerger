"""Collection metadata derived from merge inputs."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from bookmerger.epub import EpubPackage


def generated_title(books: Sequence[EpubPackage]) -> str:
    """Derive a concise collection title from source metadata."""
    authors = _unique(
        person.name
        for book in books
        for person in book.metadata.contributors
        if person.role.casefold() == "aut"
    )
    works = _unique(book.metadata.title for book in books if not book.metadata.title_is_fallback)

    def summary(values: tuple[str, ...]) -> str:
        if len(values) <= 2:
            return ", ".join(values)
        return f"{values[0]}, {values[1]} и др."

    return " — ".join(part for part in ("Сборник", summary(authors), summary(works)) if part)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(value.casefold().split())
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(" ".join(value.split()))
    return tuple(result)
