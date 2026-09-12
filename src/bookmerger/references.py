"""Resolve and rewrite EPUB-internal resource references."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import tinycss2
from lxml import etree

XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
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


@dataclass(frozen=True)
class ResourceMap:
    resources: dict[str, str]
    fetch: Callable[[str], str] | None = None

    @classmethod
    def under(cls, names: list[str] | tuple[str, ...], prefix: str) -> ResourceMap:
        return cls({name: f"{prefix.rstrip('/')}/{name}" for name in names})

    def rewrite(
        self, document: str, uri: str, *, base_uri: str | None = None, required: bool = False
    ) -> str:
        value = absolute_uri(document, uri, base_uri)
        parts = urlsplit(value)
        if is_external(value):
            if not required or parts.scheme == "data":
                return value if base_uri or is_external(document) else uri
            if parts.scheme not in {"http", "https"}:
                raise ValueError("unsupported external resource scheme")
            key = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
            destination = self.resources.get(key)
            if destination is None:
                if self.fetch is None:
                    raise ValueError("external resource requires a downloader")
                destination = self.fetch(key)
        else:
            key = _normal_path(unquote(parts.path))
            destination = self.resources.get(key)
        source_document = self.resources.get(document)
        if destination is None or source_document is None:
            return uri
        if base_uri is None and not is_external(value):
            unchanged = resolve_uri(source_document, uri)
            if unchanged and unchanged[0] == destination:
                return uri
        relative = posixpath.relpath(destination, posixpath.dirname(source_document))
        # A fetched URL's query belongs to the request, not its local filename.
        query = "" if is_external(value) else parts.query
        return urlunsplit(("", "", quote(relative, safe="/"), query, parts.fragment))


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


def rewrite_css(
    data: bytes,
    mapping: ResourceMap,
    document: str,
    base_uri: str | None = None,
    *,
    inline: bool = False,
) -> bytes:
    encoding_name = "utf-8"
    if inline:
        rules = tinycss2.parse_component_value_list(data.decode("utf-8"))
    else:
        rules, encoding = tinycss2.parse_stylesheet_bytes(
            data, skip_comments=False, skip_whitespace=False
        )
        encoding_name = encoding.codec_info.name
    changed = _rewrite_css_tokens(
        rules, lambda uri: mapping.rewrite(document, uri, base_uri=base_uri, required=True)
    )
    return tinycss2.serialize(rules).encode(encoding_name) if changed else data


def rewrite_xml(data: bytes, mapping: ResourceMap, document: str) -> bytes:
    """Resolve all references before removing HTML/XML base declarations."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(data, parser)
    bases = root.xpath("//*[local-name()='base' and @href]")
    html_base = bases[0].get("href") if bases else None
    changed = False

    def visit(element: etree._Element, base: str | None) -> None:
        nonlocal changed
        if not isinstance(element.tag, str):
            return
        local = etree.QName(element).localname
        if element.get(XML_BASE) is not None:
            base = absolute_uri(document, element.get(XML_BASE), base)
            if not is_external(base):
                base = "/" + base.lstrip("/")
        if local != "base":
            for name in ("href", "src", XLINK_HREF, "poster", "data"):
                value = element.get(name)
                if value is None:
                    continue
                required = name in {"src", "poster", "data"} or local in {
                    "image",
                    "use",
                    "link",
                    "item",
                }
                replacement = mapping.rewrite(document, value, base_uri=base, required=required)
                if replacement != value:
                    element.set(name, replacement)
                    changed = True
        if element.get("srcset") is not None:
            replacement = _rewrite_srcset(
                element.get("srcset"),
                lambda uri: mapping.rewrite(document, uri, base_uri=base, required=True),
            )
            if replacement != element.get("srcset"):
                element.set("srcset", replacement)
                changed = True
        if local == "style" and element.text:
            replacement = rewrite_css(element.text.encode(), mapping, document, base).decode()
            if replacement != element.text:
                element.text = replacement
                changed = True
        css_attributes = ("style",)
        if etree.QName(element).namespace == SVG_NS:
            css_attributes += SVG_URL_ATTRIBUTES
        for attribute in css_attributes:
            value = element.get(attribute)
            if not value:
                continue
            replacement = rewrite_css(value.encode(), mapping, document, base, inline=True).decode()
            if replacement != value:
                element.set(attribute, replacement)
                changed = True
        for child in element:
            visit(child, base)
        if XML_BASE in element.attrib:
            del element.attrib[XML_BASE]
            changed = True

    visit(root, html_base)
    for base in bases:
        base.getparent().remove(base)
        changed = True
    if not changed:
        return data
    return etree.tostring(root, xml_declaration=data.startswith(b"<?xml"), encoding="utf-8")
