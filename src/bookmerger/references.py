"""Helpers used to validate EPUB-internal references."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from urllib.parse import quote, unquote, urljoin, urlsplit

import tinycss2

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


def _rewrite_srcset(value: str, rewrite: Callable[[str], str]) -> str:
    entries = []
    # Commas inside a data URL belong to its URL token, not to the separator.
    for match in re.finditer(r"(?:^|,)[\s,]*(data:[^\s]+|[^\s,]+)([^,]*)", value):
        uri, descriptor = match.groups()
        entries.append(rewrite(uri.rstrip(",")) + descriptor)
    return ", ".join(entries)


def _rewrite_css_tokens(tokens: list[object], rewrite: Callable[[str], str]) -> bool:
    changed = False
    for token in tokens:
        if token.type == "url":
            replacement = rewrite(token.value)
            if replacement != token.value:
                token.value = replacement
                token.representation = (
                    'url("' + tinycss2.serializer.serialize_string_value(replacement) + '")'
                )
                changed = True
        elif token.type == "function" and token.lower_name == "url":
            arguments = [
                part for part in token.arguments if part.type not in {"whitespace", "comment"}
            ]
            if len(arguments) == 1 and arguments[0].type == "string":
                raw = arguments[0].value
                replacement = rewrite(raw)
                if replacement != raw:
                    escaped = tinycss2.serializer.serialize_string_value(replacement)
                    token.arguments = tinycss2.parse_component_value_list('"' + escaped + '"')
                    changed = True
        if token.type == "at-rule" and token.lower_at_keyword == "import":
            for part in token.prelude:
                if part.type == "string":
                    replacement = rewrite(part.value)
                    if replacement != part.value:
                        part.value = replacement
                        part.representation = (
                            '"' + tinycss2.serializer.serialize_string_value(replacement) + '"'
                        )
                        changed = True
                    break
        for attribute in ("prelude", "content", "arguments"):
            children = getattr(token, attribute, None)
            if children is not None:
                changed |= _rewrite_css_tokens(children, rewrite)
    return changed
