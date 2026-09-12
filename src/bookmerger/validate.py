"""Validation for the EPUB assembled by bookmerger."""

from __future__ import annotations

import hashlib
import posixpath
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import tinycss2
from lxml import etree

from bookmerger.epub import XML_MEDIA_TYPES
from bookmerger.references import SVG_NS, SVG_URL_ATTRIBUTES, _rewrite_css_tokens, _rewrite_srcset

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
EPUB_NS = "http://www.idpf.org/2007/ops"
XLINK_NS = "http://www.w3.org/1999/xlink"


class ValidationError(RuntimeError):
    """The generated EPUB has a broken package or reference."""


_XML_SUFFIXES = {".ncx", ".opf", ".smil", ".svg", ".xhtml", ".html"}
_RASTER_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}


def _raster_media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _xml(data: bytes, name: str) -> etree._Element:
    try:
        return etree.fromstring(
            data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        )
    except etree.XMLSyntaxError as error:
        raise ValidationError(f"invalid XML: {name}") from error


def _archive_path(document: str, value: str) -> tuple[str, str] | None:
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or value.startswith("data:"):
        return None
    path = unquote(parts.path)
    if not path:
        return document, unquote(parts.fragment)
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(document), path))
    if resolved in {".", ".."} or resolved.startswith("../") or resolved.startswith("/"):
        raise ValidationError(f"reference escapes EPUB: {value}")
    return resolved, unquote(parts.fragment)


def _check_reference(
    document: str, value: str, names: set[str], identifiers: dict[str, set[str]]
) -> None:
    target = _archive_path(document, value)
    if target is None:
        return
    name, fragment = target
    if name not in names:
        raise ValidationError(f"missing resource {name} referenced by {document}")
    if fragment and fragment not in identifiers.get(name, set()):
        raise ValidationError(f"missing anchor #{fragment} referenced by {document}")


def _check_documents(
    contents: dict[str, bytes], names: set[str], stylesheets: dict[str, bytes]
) -> None:
    roots = {name: _xml(data, name) for name, data in contents.items() if name in names}
    identifiers: dict[str, set[str]] = {}
    for name, root in roots.items():
        ids = [value for value in root.xpath("//@id") if value]
        if len(ids) != len(set(ids)):
            raise ValidationError(f"duplicate IDs in {name}")
        identifiers[name] = set(ids)
    for name, data in stylesheets.items():
        rules, _ = tinycss2.parse_stylesheet_bytes(data)

        def check_stylesheet_url(value: str, document: str = name) -> str:
            _check_reference(document, value, names, identifiers)
            return value

        _rewrite_css_tokens(rules, check_stylesheet_url)
    attributes = ("href", "src", "poster", "data")
    for name, root in roots.items():

        def check_css_url(value: str, document: str = name) -> str:
            _check_reference(document, value, names, identifiers)
            return value

        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            for attribute in attributes:
                if value := element.get(attribute):
                    _check_reference(name, value, names, identifiers)
            if value := element.get(f"{{{XLINK_NS}}}href"):
                _check_reference(name, value, names, identifiers)
            if value := element.get("srcset"):
                _rewrite_srcset(value, check_css_url)
            if value := element.get("style"):
                _rewrite_css_tokens(tinycss2.parse_component_value_list(value), check_css_url)
            if etree.QName(element).localname == "style" and element.text:
                _rewrite_css_tokens(tinycss2.parse_stylesheet(element.text), check_css_url)
            if etree.QName(element).namespace == SVG_NS:
                for attribute in SVG_URL_ATTRIBUTES:
                    if value := element.get(attribute):
                        _rewrite_css_tokens(
                            tinycss2.parse_component_value_list(value), check_css_url
                        )


def _opf_path(archive: zipfile.ZipFile) -> str:
    try:
        root = _xml(archive.read("META-INF/container.xml"), "META-INF/container.xml")
    except KeyError as error:
        raise ValidationError("EPUB has no container.xml") from error
    rootfile = root.find(f".//{{{CONTAINER_NS}}}rootfile")
    if rootfile is None or not rootfile.get("full-path"):
        raise ValidationError("EPUB container has no package document")
    return rootfile.get("full-path")


def _check_package(archive: zipfile.ZipFile, names: set[str]) -> dict[str, str]:
    opf_path = _opf_path(archive)
    if opf_path not in names:
        raise ValidationError("EPUB package document is missing")
    root = _xml(archive.read(opf_path), opf_path)
    manifest = root.find(f"{{{OPF_NS}}}manifest")
    spine = root.find(f"{{{OPF_NS}}}spine")
    if manifest is None or spine is None:
        raise ValidationError("OPF has no manifest or spine")
    items = list(manifest.findall(f"{{{OPF_NS}}}item"))
    ids = [item.get("id") for item in items]
    if not ids or None in ids or len(ids) != len(set(ids)):
        raise ValidationError("OPF manifest IDs are not unique")
    item_ids = set(ids)
    nav_paths: set[str] = set()
    media_types = {opf_path: "application/oebps-package+xml"}
    for item in items:
        href = item.get("href", "")
        target = _archive_path(opf_path, href)
        if target is None or target[0] not in names:
            raise ValidationError(f"missing manifest resource: {href}")
        media_types[target[0]] = item.get("media-type", "")
        expected = _raster_media_type(archive.read(target[0]))
        if expected and item.get("media-type") != expected:
            raise ValidationError(f"incorrect MIME type for {href}")
        if "nav" in (item.get("properties") or "").split():
            nav_paths.add(target[0])
    if not nav_paths:
        raise ValidationError("OPF has no navigation document")
    for nav_path in nav_paths:
        nav = _xml(archive.read(nav_path), nav_path)
        if not nav.xpath(
            "//*[local-name()='nav' and "
            "contains(concat(' ', normalize-space(@epub:type), ' '), ' toc ')]",
            namespaces={"epub": EPUB_NS},
        ):
            raise ValidationError("navigation document has no table of contents")
    for itemref in spine.findall(f"{{{OPF_NS}}}itemref"):
        if itemref.get("idref") not in item_ids:
            raise ValidationError("OPF spine refers to a missing manifest item")
    return media_types


def validate_epub(path: Path, source_directory: Path | None = None) -> None:
    """Validate EPUB topology, links, IDs, MIME types, and copied images."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos or infos[0].filename != "mimetype":
                raise ValidationError("mimetype must be the first ZIP entry")
            if infos[0].compress_type != zipfile.ZIP_STORED or infos[0].extra:
                raise ValidationError("mimetype must be stored without ZIP extra fields")
            if archive.read("mimetype") != b"application/epub+zip":
                raise ValidationError("invalid EPUB mimetype")
            names = {info.filename for info in infos}
            if len(names) != len(infos):
                raise ValidationError("EPUB has duplicate ZIP paths")
            media_types = _check_package(archive, names)
            xml_names = {
                name
                for name in names
                if media_types.get(name) in XML_MEDIA_TYPES
                or (
                    name not in media_types
                    and PurePosixPath(name).suffix.lower() in _XML_SUFFIXES
                    and archive.read(name).lstrip().startswith(b"<")
                )
            }
            css_names = {
                name
                for name in names
                if media_types.get(name) == "text/css"
                or (name not in media_types and PurePosixPath(name).suffix.lower() == ".css")
            }
            _check_documents(
                {name: archive.read(name) for name in xml_names},
                names,
                {name: archive.read(name) for name in css_names},
            )
            if source_directory:
                for source in source_directory.rglob("*"):
                    if not source.is_file():
                        continue
                    data = source.read_bytes()
                    if source.suffix.lower() not in _RASTER_SUFFIXES and not _raster_media_type(
                        data
                    ):
                        continue
                    name = source.relative_to(source_directory).as_posix()
                    if (
                        name in names
                        and hashlib.sha256(data).digest()
                        != hashlib.sha256(archive.read(name)).digest()
                    ):
                        raise ValidationError(f"image bytes changed: {name}")
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise ValidationError(f"cannot validate EPUB {path}") from error
