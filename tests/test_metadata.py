from __future__ import annotations

from bookmerger.epub import BookMetadata, Contributor, EpubPackage
from bookmerger.metadata import generated_title


def _book(
    title: str, contributors: tuple[Contributor, ...], *, fallback: bool = False
) -> EpubPackage:
    return EpubPackage(
        "",
        (),
        (),
        (),
        BookMetadata(title, contributors, (), (), None, (), (), (), title_is_fallback=fallback),
        None,
    )


def test_generated_title_uses_unique_source_metadata() -> None:
    books = (
        _book(" Work ", (Contributor(" Author ", "aut"),)),
        _book("work", (Contributor("author", "AUT"),)),
        _book("Third", (Contributor("Second", "aut"),)),
        _book("Fourth", (Contributor("Third", "aut"),)),
    )

    assert generated_title(books) == "Сборник — Author, Second и др. — Work, Third и др."


def test_generated_title_omits_fallback_metadata() -> None:
    assert generated_title((_book("Untitled", (), fallback=True),)) == "Сборник"
