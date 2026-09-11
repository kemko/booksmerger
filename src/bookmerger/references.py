"""Resolve and rewrite EPUB-internal resource references."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import tinycss2
from lxml import etree

XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


def is_external(uri: str) -> bool:
    """Whether *uri* must not be resolved inside an EPUB archive."""
    parts = urlsplit(uri)
    return bool(parts.scheme or parts.netloc) or uri.startswith("//") or uri.startswith("data:")


def _normal_path(path: str) -> str:
    value = posixpath.normpath(unquote(path)).lstrip("/")
    if value in {"", "."} or value == ".." or value.startswith("../"):
        raise ValueError(f"unsafe EPUB URI path: {path!r}")
    return value


def resolve_uri(
    document: str, uri: str, base_uri: str | None = None
) -> tuple[str, str, str] | None:
    """Resolve an internal URI to archive path, query and fragment.

    URI percent encoding is decoded only for the archive lookup; callers can
    retain canonical encoding when writing a reference back out.
    """
    if not uri or is_external(uri):
        return None
    base = urljoin(document, base_uri) if base_uri else document
    if is_external(base):
        return None
    parts = urlsplit(urljoin(base, uri))
    path = _normal_path(parts.path or document)
    return path, parts.query, parts.fragment


@dataclass(frozen=True)
class ResourceMap:
    """Mapping from one book's archive names to its collection names."""

    resources: dict[str, str]

    @classmethod
    def under(cls, names: list[str] | tuple[str, ...], prefix: str) -> ResourceMap:
        root = prefix.rstrip("/")
        return cls({_normal_path(name): f"{root}/{_normal_path(name)}" for name in names})

    def rewrite(self, document: str, uri: str, *, base_uri: str | None = None) -> str:
        """Return a URI valid from the mapped copy of *document*.

        External links and data URIs are deliberately returned unchanged.
        """
        resolved = resolve_uri(document, uri, base_uri)
        if resolved is None:
            return uri
        target, query, fragment = resolved
        destination = self.resources.get(target)
        source_document = self.resources.get(_normal_path(document))
        if destination is None or source_document is None:
            return uri
        relative = posixpath.relpath(destination, posixpath.dirname(source_document))
        return urlunsplit(("", "", quote(relative, safe="/%:@"), query, fragment))


def _base_uri(root: etree._Element, document: str) -> str:
    bases = root.xpath("//*[local-name()='base' and @href]")
    return bases[0].get("href") if bases else document


def _rewrite_srcset(value: str, mapping: ResourceMap, document: str, base: str) -> str:
    entries = []
    candidates = value.split(",")
    while candidates:
        candidate = candidates.pop(0)
        if candidate.lstrip().startswith("data:"):
            candidate = ",".join((candidate, *candidates))
            candidates.clear()
        parts = candidate.strip().split(None, 1)
        if not parts:
            continue
        rewritten = mapping.rewrite(document, parts[0], base_uri=base)
        entries.append(" ".join((rewritten, *parts[1:])))
    return ", ".join(entries)


def _rewrite_css_tokens(
    tokens: list[object], mapping: ResourceMap, document: str, base: str
) -> bool:
    changed = False
    for token in tokens:
        if token.type == "url":
            replacement = mapping.rewrite(document, token.value, base_uri=base)
            if replacement != token.value:
                token.value = replacement
                token.representation = f'url("{replacement}")'
                changed = True
        elif token.type == "function" and token.lower_name == "url":
            raw = tinycss2.serialize(token.arguments).strip().strip("\"'")
            replacement = mapping.rewrite(document, raw, base_uri=base)
            if replacement != raw:
                token.arguments = tinycss2.parse_component_value_list(f'"{replacement}"')
                changed = True
        if hasattr(token, "content") and token.content is not None:
            changed |= _rewrite_css_tokens(token.content, mapping, document, base)
        if hasattr(token, "arguments") and token.type != "function":
            changed |= _rewrite_css_tokens(token.arguments, mapping, document, base)
    return changed


def rewrite_css(data: bytes, mapping: ResourceMap, document: str) -> bytes:
    """Rewrite CSS ``url()`` and ``@import`` targets without changing other files."""
    text = data.decode("utf-8")
    rules = tinycss2.parse_stylesheet(text, skip_comments=False, skip_whitespace=False)
    changed = _rewrite_css_tokens(rules, mapping, document, document)
    return tinycss2.serialize(rules).encode() if changed else data


def rewrite_xml(data: bytes, mapping: ResourceMap, document: str) -> bytes:
    """Rewrite common EPUB XML references, retaining bytes if nothing changes."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(data, parser)
    base = _base_uri(root, document)
    changed = False
    for element in root.iter():
        for name in ("href", "src", XLINK_HREF):
            value = element.get(name)
            is_base_href = etree.QName(element).localname == "base" and name == "href"
            if value is not None and not is_base_href:
                replacement = mapping.rewrite(document, value, base_uri=base)
                if replacement != value:
                    element.set(name, replacement)
                    changed = True
        srcset = element.get("srcset")
        if srcset is not None:
            replacement = _rewrite_srcset(srcset, mapping, document, base)
            if replacement != srcset:
                element.set("srcset", replacement)
                changed = True
        if etree.QName(element).localname == "style" and element.text:
            replacement = rewrite_css(element.text.encode(), mapping, document).decode()
            if replacement != element.text:
                element.text = replacement
                changed = True
        style = element.get("style")
        if style:
            replacement = rewrite_css(style.encode(), mapping, document).decode()
            if replacement != style:
                element.set("style", replacement)
                changed = True
    if not changed:
        return data
    return etree.tostring(root, xml_declaration=data.startswith(b"<?xml"), encoding="utf-8")
