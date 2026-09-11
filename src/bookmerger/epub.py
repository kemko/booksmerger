"""Lossless EPUB package reading and per-book staging."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from lxml import etree

from bookmerger.references import ResourceMap, rewrite_css, rewrite_xml

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"


class EpubError(RuntimeError):
    """An EPUB package cannot be read without losing required structure."""


@dataclass(frozen=True)
class ManifestItem:
    id: str
    href: str
    media_type: str
    properties: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpineItem:
    idref: str
    linear: bool
    properties: tuple[str, ...] = ()


@dataclass(frozen=True)
class Contributor:
    """A named EPUB contributor and its MARC relator role."""

    name: str
    role: str
    identifier: str | None = None


@dataclass(frozen=True)
class BookMetadata:
    """Metadata retained from a source package for collection front matter."""

    title: str
    contributors: tuple[Contributor, ...]
    languages: tuple[str, ...]
    subjects: tuple[str, ...]
    publisher: str | None
    identifiers: tuple[str, ...]
    rights: tuple[str, ...]
    details: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class EpubPackage:
    opf_path: str
    manifest: tuple[ManifestItem, ...]
    spine: tuple[SpineItem, ...]
    navigation: tuple[str, ...]
    metadata: BookMetadata
    cover: str | None


@dataclass(frozen=True)
class StagedBook:
    number: int
    prefix: str
    package: EpubPackage
    resource_map: ResourceMap


def _parse(data: bytes, message: str) -> etree._Element:
    try:
        return etree.fromstring(
            data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        )
    except etree.XMLSyntaxError as error:
        raise EpubError(message) from error


def _rootfile(archive: zipfile.ZipFile) -> str:
    try:
        root = _parse(archive.read("META-INF/container.xml"), "invalid container.xml")
        item = root.find(f".//{{{CONTAINER_NS}}}rootfile")
    except KeyError as error:
        raise EpubError("EPUB has no container.xml") from error
    if item is None or not item.get("full-path"):
        raise EpubError("EPUB container has no package document")
    return item.get("full-path")


def _parts(value: str | None) -> tuple[str, ...]:
    return tuple((value or "").split())


def _text_items(metadata: etree._Element, name: str) -> tuple[str, ...]:
    return tuple(
        text
        for item in metadata.findall(f"{{{DC_NS}}}{name}")
        if (text := "".join(item.itertext()).strip())
    )


def _metadata(root: etree._Element) -> BookMetadata:
    node = root.find(f"{{{OPF_NS}}}metadata")
    if node is None:
        raise EpubError("OPF has no metadata")
    roles = {
        item.get("refines", "").removeprefix("#"): "".join(item.itertext()).strip()
        for item in node.findall(f"{{{OPF_NS}}}meta")
        if item.get("property") == "role" and item.get("refines")
    }
    contributors: list[Contributor] = []
    for name in ("creator", "contributor"):
        for item in node.findall(f"{{{DC_NS}}}{name}"):
            value = "".join(item.itertext()).strip()
            if value:
                role = roles.get(item.get("id", "")) or item.get(f"{{{OPF_NS}}}role")
                default_role = "aut" if name == "creator" else "oth"
                contributors.append(Contributor(value, role or default_role, item.get("id")))
    titles = _text_items(node, "title")
    details = tuple(
        (item.get("property", etree.QName(item).localname), "".join(item.itertext()).strip())
        for item in node
        if "".join(item.itertext()).strip()
        and not (item.tag == f"{{{OPF_NS}}}meta" and item.get("property") == "role")
    )
    return BookMetadata(
        titles[0] if titles else "Untitled",
        tuple(contributors),
        _text_items(node, "language"),
        _text_items(node, "subject"),
        next(iter(_text_items(node, "publisher")), None),
        _text_items(node, "identifier"),
        _text_items(node, "rights"),
        details,
    )


def read_package(path: Path) -> EpubPackage:
    """Read package topology without rebuilding its documents or resources."""
    try:
        with zipfile.ZipFile(path) as archive:
            opf_path = _rootfile(archive)
            root = _parse(archive.read(opf_path), "invalid OPF package document")
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise EpubError(f"cannot read EPUB {path}") from error
    manifest_node = root.find(f"{{{OPF_NS}}}manifest")
    spine_node = root.find(f"{{{OPF_NS}}}spine")
    if manifest_node is None or spine_node is None:
        raise EpubError("OPF has no manifest or spine")
    manifest = tuple(
        ManifestItem(
            item.get("id", ""),
            item.get("href", ""),
            item.get("media-type", ""),
            _parts(item.get("properties")),
        )
        for item in manifest_node.findall(f"{{{OPF_NS}}}item")
    )
    if not all(item.id and item.href and item.media_type for item in manifest):
        raise EpubError("OPF manifest has an incomplete item")
    ids = {item.id for item in manifest}
    spine = tuple(
        SpineItem(
            item.get("idref", ""),
            item.get("linear", "yes") != "no",
            _parts(item.get("properties")),
        )
        for item in spine_node.findall(f"{{{OPF_NS}}}itemref")
    )
    if not all(item.idref in ids for item in spine):
        raise EpubError("OPF spine refers to a missing manifest item")
    navigation = tuple(
        item.href
        for item in manifest
        if "nav" in item.properties or item.media_type == "application/x-dtbncx+xml"
    )
    metadata = _metadata(root)
    cover_id = next(
        (
            item.get("content")
            for item in root.findall(f".//{{{OPF_NS}}}meta")
            if item.get("name") == "cover"
        ),
        None,
    )
    cover = next(
        (item.href for item in manifest if "cover-image" in item.properties or item.id == cover_id),
        None,
    )
    return EpubPackage(opf_path, manifest, spine, navigation, metadata, cover)


def _xml_type(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() in {
        ".xhtml",
        ".html",
        ".svg",
        ".smil",
        ".ncx",
        ".opf",
    }


def stage_epub(path: Path, directory: Path, number: int) -> StagedBook:
    """Copy one EPUB under ``books/NNNN`` while retaining its path topology.

    References remain valid because every archive path moves by the same prefix;
    the rewrite pass handles percent-encoded/base-URI references when necessary.
    """
    package = read_package(path)
    prefix = f"books/{number:04d}"
    with zipfile.ZipFile(path) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        mapping = ResourceMap.under(names, prefix)
        for name in names:
            target = directory / mapping.resources[name]
            target.parent.mkdir(parents=True, exist_ok=True)
            data = archive.read(name)
            if PurePosixPath(name).suffix.lower() == ".css":
                data = rewrite_css(data, mapping, name)
            elif _xml_type(name):
                data = rewrite_xml(data, mapping, name)
            target.write_bytes(data)
    unique = {item.id: f"book-{number:04d}-{item.id}" for item in package.manifest}
    staged_package = EpubPackage(
        mapping.resources[package.opf_path],
        tuple(
            ManifestItem(
                unique[item.id],
                mapping.resources[_item_path(package, item.href)],
                item.media_type,
                item.properties,
            )
            for item in package.manifest
        ),
        tuple(
            SpineItem(unique[item.idref], item.linear, item.properties) for item in package.spine
        ),
        tuple(mapping.resources[_item_path(package, href)] for href in package.navigation),
        package.metadata,
        mapping.resources[_item_path(package, package.cover)] if package.cover else None,
    )
    return StagedBook(number, prefix, staged_package, mapping)


def _item_path(package: EpubPackage, href: str) -> str:
    return (PurePosixPath(package.opf_path).parent / href).as_posix()
