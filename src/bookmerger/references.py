"""Helpers used to validate EPUB-internal references."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from urllib.parse import quote, unquote, urljoin, urlsplit

SVG_NS = "http://www.w3.org/2000/svg"
SVG_URL_ATTRIBUTES = (
    "fill",
    "stroke",
    "filter",
    "clip-path",
    "mask",
    "marker",
    "marker-start",
    "marker-mid",
    "marker-end",
    "cursor",
    "color-profile",
)


def is_external(uri: str) -> bool:
    parts = urlsplit(uri)
    return bool(parts.scheme or parts.netloc)


def _normal_path(path: str) -> str:
    value = posixpath.normpath(path).lstrip("/")
    if value in {"", ".", ".."} or value.startswith("../"):
        raise ValueError(f"unsafe EPUB URI path: {path!r}")
    return value


def absolute_uri(document: str, uri: str, base_uri: str | None = None) -> str:
    base = document if is_external(document) else quote(document, safe="/")
    if base_uri:
        base = urljoin(base, base_uri)
    return urljoin(base, uri)


def resolve_uri(
    document: str, uri: str, base_uri: str | None = None
) -> tuple[str, str, str] | None:
    """Resolve URI encoding once, keeping literal archive names distinct."""
    value = absolute_uri(document, uri, base_uri)
    if is_external(value):
        return None
    parts = urlsplit(value)
    return _normal_path(unquote(parts.path)), parts.query, parts.fragment


def check_srcset(value: str, check: Callable[[str], None]) -> None:
    # Commas inside a data URL belong to its URL token, not to the separator.
    for match in re.finditer(r"(?:^|,)[\s,]*(data:[^\s]+|[^\s,]+)([^,]*)", value):
        check(match.group(1).rstrip(","))


def check_css_tokens(tokens: list[object], check: Callable[[str], None]) -> None:
    for token in tokens:
        if token.type == "url":
            check(token.value)
        elif token.type == "function" and token.lower_name == "url":
            arguments = [
                part for part in token.arguments if part.type not in {"whitespace", "comment"}
            ]
            if len(arguments) == 1 and arguments[0].type == "string":
                check(arguments[0].value)
        if token.type == "at-rule" and token.lower_at_keyword == "import":
            for part in token.prelude:
                if part.type == "string":
                    check(part.value)
                    break
        for attribute in ("prelude", "content", "arguments"):
            children = getattr(token, attribute, None)
            if children is not None:
                check_css_tokens(children, check)
