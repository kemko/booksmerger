"""Lossless EPUB package reading and per-book staging."""

from __future__ import annotations

import mimetypes
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from lxml import etree

from bookmerger.download import DEFAULT_LIMITS, Downloader, _validate_zip
from bookmerger.references import ResourceMap, resolve_uri, rewrite_css, rewrite_xml

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"


def write_epub(directory: Path, output: Path) -> None:
    """Write a deterministic EPUB container from a prepared directory."""
    container = directory / "META-INF" / "container.xml"
    container.parent.mkdir(parents=True, exist_ok=True)
    container.write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<container version="1.0" '
        b'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        b'<rootfiles><rootfile full-path="EPUB/package.opf" '
        b'media-type="application/oebps-package+xml"/>'
        b"</rootfiles></container>"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        mimetype = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
        mimetype.compress_type = zipfile.ZIP_STORED
        mimetype.extra = b""
        archive.writestr(mimetype, b"application/epub+zip")
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            name = path.relative_to(directory).as_posix()
            if name == "mimetype":
                continue
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.extra = b""
            archive.writestr(
                entry, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
            )


class EpubError(RuntimeError):
    """An EPUB package cannot be read without losing required structure."""


@dataclass(frozen=True)
class ManifestItem:
    id: str
    href: str
    media_type: str
    properties: tuple[str, ...] = ()
    fallback: str | None = None
    media_overlay: str | None = None


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
    media_metadata: tuple[bytes, ...] = ()


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
        (
            item.get("property") or item.get("name") or etree.QName(item).localname,
            item.get("content") or "".join(item.itertext()).strip(),
        )
        for item in node
        if isinstance(item.tag, str)
        and (item.get("content") or "".join(item.itertext()).strip())
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
            _validate_zip(archive, "EPUB", DEFAULT_LIMITS)
            if "META-INF/encryption.xml" in archive.namelist():
                encryption = _parse(
                    archive.read("META-INF/encryption.xml"), "invalid encryption.xml"
                )
                if encryption.xpath("//*[local-name()='EncryptedData']"):
                    raise EpubError("DRM and obfuscated fonts are unsupported")
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
            item.get("fallback"),
            item.get("media-overlay"),
        )
        for item in manifest_node.findall(f"{{{OPF_NS}}}item")
    )
    if not all(item.id and item.href and item.media_type for item in manifest):
        raise EpubError("OPF manifest has an incomplete item")
    ids = {item.id for item in manifest}
    if len(ids) != len(manifest):
        raise EpubError("OPF manifest has duplicate IDs")
    if any(
        ref not in ids for item in manifest for ref in (item.fallback, item.media_overlay) if ref
    ):
        raise EpubError("OPF manifest refers to a missing fallback or media overlay")
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
    media_metadata = tuple(
        etree.tostring(item)
        for item in root.findall(f"{{{OPF_NS}}}metadata/{{{OPF_NS}}}meta")
        if item.get("property", "").startswith("media:")
    )
    return EpubPackage(opf_path, manifest, spine, navigation, metadata, cover, media_metadata)


def _xml_type(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() in {
        ".xhtml",
        ".html",
        ".svg",
        ".smil",
        ".ncx",
        ".opf",
    }


def stage_epub(
    path: Path, directory: Path, number: int, *, downloader: Downloader | None = None
) -> StagedBook:
    """Copy one EPUB under ``books/NNNN`` while retaining its path topology.

    References remain valid because every archive path moves by the same prefix;
    the rewrite pass handles percent-encoded/base-URI references when necessary.
    """
    package = read_package(path)
    prefix = f"books/{number:04d}"
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        pending = [(name, archive.read(name)) for name in names]
    resources = ResourceMap.under(names, prefix).resources
    remote_items: list[ManifestItem] = []
    total = sum(len(data) for _, data in pending)
    downloader = downloader or Downloader()

    def fetch(url: str) -> str:
        nonlocal total
        if url in resources:
            return resources[url]
        if len(pending) >= downloader.limits.max_zip_entries:
            raise EpubError("too many embedded resources")
        data, content_type, final_url = downloader.resource(
            url, directory / f"remote-{number:04d}.download"
        )
        if final_url in resources:
            resources[url] = resources[final_url]
            return resources[url]
        total += len(data)
        if total > downloader.limits.max_zip_uncompressed_bytes:
            raise EpubError("embedded resources exceed expanded size limit")
        declared = next((item.media_type for item in package.manifest if item.href == url), None)
        media_type = (
            declared
            or (content_type if content_type != "application/octet-stream" else None)
            or mimetypes.guess_type(urlsplit(final_url).path)[0]
        )
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            media_type = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            media_type = "image/jpeg"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            media_type = "image/gif"
        if not media_type:
            raise EpubError("cannot determine embedded resource MIME type")
        suffix = mimetypes.guess_extension(media_type) or ".bin"
        folder = "_remote"
        destination = f"{prefix}/{folder}/{len(remote_items):04d}{suffix}"
        while destination in resources.values():
            folder += "_"
            destination = f"{prefix}/{folder}/{len(remote_items):04d}{suffix}"
        resources[url] = destination
        resources[final_url] = destination
        pending.append((final_url, data))
        remote_items.append(
            ManifestItem(f"remote-{number:04d}-{len(remote_items)}", destination, media_type)
        )
        return destination

    mapping = ResourceMap(resources, fetch)
    for item in package.manifest:
        if urlsplit(item.href).scheme in {"http", "https"}:
            fetch(item.href)
    for name, data in pending:
        target = directory / mapping.resources[name]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix.lower() == ".css":
            data = rewrite_css(data, mapping, name)
        elif _xml_type(target.name):
            try:
                data = rewrite_xml(data, mapping, name)
            except etree.XMLSyntaxError:
                if PurePosixPath(name).suffix.lower() not in {".html", ".svg"}:
                    raise EpubError(f"invalid XML resource: {name}") from None
        target.write_bytes(data)
    unique = {item.id: f"book-{number:04d}-{item.id}" for item in package.manifest}
    staged_package = EpubPackage(
        mapping.resources[package.opf_path],
        tuple(
            ManifestItem(
                unique[item.id],
                mapping.resources[_item_path(package, item.href)],
                item.media_type,
                tuple(value for value in item.properties if value != "remote-resources"),
                unique[item.fallback] if item.fallback else None,
                unique[item.media_overlay] if item.media_overlay else None,
            )
            for item in package.manifest
        )
        + tuple(
            item
            for item in remote_items
            if item.href
            not in {resources[_item_path(package, original.href)] for original in package.manifest}
        ),
        tuple(
            SpineItem(unique[item.idref], item.linear, item.properties) for item in package.spine
        ),
        tuple(mapping.resources[_item_path(package, href)] for href in package.navigation),
        package.metadata,
        mapping.resources[_item_path(package, package.cover)] if package.cover else None,
        package.media_metadata,
    )
    return StagedBook(number, prefix, staged_package, mapping)


def _item_path(package: EpubPackage, href: str) -> str:
    resolved = resolve_uri(package.opf_path, href)
    return resolved[0] if resolved else href
