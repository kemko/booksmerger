"""EPUB package reading and validation for EpubMerge."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from lxml import etree

from bookmerger.download import DEFAULT_LIMITS, DownloadError, _validate_zip
from bookmerger.references import resolve_uri

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
# https://www.w3.org/TR/epub-33/#sec-reserved-prefixes
RESERVED_PREFIXES = {
    "a11y": "http://www.idpf.org/epub/vocab/package/a11y/#",
    "dcterms": "http://purl.org/dc/terms/",
    "marc": "http://id.loc.gov/vocabulary/",
    "media": "http://www.idpf.org/epub/vocab/overlays/#",
    "onix": "http://www.editeur.org/ONIX/book/codelists/current.html#",
    "rendition": "http://www.idpf.org/vocab/rendition/#",
    "schema": "http://schema.org/",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}
XML_MEDIA_TYPES = {
    "application/xhtml+xml",
    "image/svg+xml",
    "application/smil+xml",
    "application/x-dtbncx+xml",
    "application/oebps-package+xml",
}


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
    """Source metadata retained for title generation and merge options."""

    title: str
    contributors: tuple[Contributor, ...]
    languages: tuple[str, ...]
    title_is_fallback: bool = False


@dataclass(frozen=True)
class EpubPackage:
    opf_path: str
    manifest: tuple[ManifestItem, ...]
    spine: tuple[SpineItem, ...]
    navigation: tuple[str, ...]
    metadata: BookMetadata


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
    roles: dict[str, list[str]] = {}
    for item in node.findall(f"{{{OPF_NS}}}meta"):
        if item.get("property") == "role" and item.get("refines"):
            role = "".join(item.itertext()).strip()
            if role:
                roles.setdefault(item.get("refines").removeprefix("#"), []).append(role)
    contributors: list[Contributor] = []
    for name in ("creator", "contributor"):
        for item in node.findall(f"{{{DC_NS}}}{name}"):
            value = "".join(item.itertext()).strip()
            if value:
                default_role = "aut" if name == "creator" else "oth"
                person_roles = roles.get(item.get("id", "")) or [
                    item.get(f"{{{OPF_NS}}}role") or default_role
                ]
                contributors.extend(
                    Contributor(value, role, item.get("id")) for role in dict.fromkeys(person_roles)
                )
    titles = _text_items(node, "title")
    return BookMetadata(
        titles[0] if titles else "Untitled",
        tuple(contributors),
        _text_items(node, "language"),
        title_is_fallback=not titles,
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
    except DownloadError as error:
        raise EpubError(str(error)) from error
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
    rendition = {}
    allowed = {
        "rendition:layout": {"reflowable", "pre-paginated"},
        "rendition:orientation": {"auto", "landscape", "portrait"},
        "rendition:spread": {"auto", "none", "landscape", "both"},
        "rendition:flow": {"auto", "paginated", "scrolled-continuous", "scrolled-doc"},
    }
    for meta in root.findall(f"{{{OPF_NS}}}metadata/{{{OPF_NS}}}meta"):
        prop = meta.get("property", "")
        if not prop.startswith("rendition:"):
            continue
        value = (meta.text or "").strip()
        if prop not in allowed or value not in allowed[prop] or meta.get("refines"):
            raise EpubError(f"unsupported rendition setting: {prop}")
        if prop in rendition:
            raise EpubError(f"duplicate rendition setting: {prop}")
        rendition[prop] = value
    progression = spine_node.get("page-progression-direction", "default")
    if progression not in {"default", "ltr", "rtl"}:
        raise EpubError("invalid page progression direction")
    spine = tuple(
        SpineItem(
            item.get("idref", ""),
            item.get("linear", "yes") != "no",
            _parts(item.get("properties"))
            + tuple(
                f"{prop}-{value}"
                for prop, value in rendition.items()
                if not any(token.startswith(prop + "-") for token in _parts(item.get("properties")))
            ),
        )
        for item in spine_node.findall(f"{{{OPF_NS}}}itemref")
    )
    if not all(item.idref in ids for item in spine):
        raise EpubError("OPF spine refers to a missing manifest item")
    prefix_tokens = root.get("prefix", "").split()
    prefixes: dict[str, str] = {}
    try:
        for key, uri in zip(prefix_tokens[::2], prefix_tokens[1::2], strict=True):
            if not key.endswith(":") or ":" in key[:-1] or key[:-1] in prefixes:
                raise ValueError("invalid prefix declaration")
            key = key[:-1]
            etree.QName(key)
            if key in RESERVED_PREFIXES and uri != RESERVED_PREFIXES[key]:
                raise EpubError(f"overridden reserved OPF prefix is unsupported: {key}")
            prefixes[key] = uri
    except ValueError as error:
        raise EpubError("invalid OPF prefix declaration") from error
    for item in (*manifest, *spine):
        for prop in item.properties:
            if (
                ":" in prop
                and prop.split(":", 1)[0] not in prefixes.keys() | RESERVED_PREFIXES.keys()
            ):
                raise EpubError(f"undeclared OPF property prefix: {prop}")
    navigation = tuple(
        item.href
        for item in sorted(manifest, key=lambda item: "nav" not in item.properties)
        if "nav" in item.properties or item.media_type == "application/x-dtbncx+xml"
    )
    metadata = _metadata(root)
    return EpubPackage(
        opf_path,
        manifest,
        spine,
        navigation,
        metadata,
    )


def validate_merge_input(path: Path) -> EpubPackage:
    """Validate the subset of EPUB EpubMerge consumes without modifying it."""
    package = read_package(path)
    try:
        with zipfile.ZipFile(path) as archive:
            _validate_zip(archive, "EPUB", DEFAULT_LIMITS)
            names = {info.filename for info in archive.infolist() if not info.is_dir()}
            manifest_paths: dict[str, str] = {}
            for item in package.manifest:
                resolved = resolve_uri(package.opf_path, item.href)
                if resolved is None or resolved[1] or resolved[2] or resolved[0] not in names:
                    raise EpubError(f"missing manifest resource: {item.href}")
                if urlsplit(item.href).path.startswith("/"):
                    raise EpubError("root-relative manifest paths are unsupported by EpubMerge")
                # The pinned engine emits decoded paths as hrefs without URI escaping.
                if any(character in resolved[0] for character in "%?#"):
                    raise EpubError(f"unsupported URI characters in manifest path: {item.href}")
                manifest_paths[item.id] = resolved[0]
            if not package.spine:
                raise EpubError("OPF spine is empty")
            if any(item.idref not in manifest_paths for item in package.spine):
                raise EpubError("OPF spine refers to a missing manifest item")
            _validate_ncx(archive, names, package, manifest_paths)
    except DownloadError as error:
        raise EpubError(str(error)) from error
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise EpubError(f"cannot read EPUB {path}") from error
    return package


def _validate_ncx(
    archive: zipfile.ZipFile,
    names: set[str],
    package: EpubPackage,
    manifest_paths: dict[str, str],
) -> None:
    candidates = [
        (item, manifest_paths[item.id])
        for item in package.manifest
        if item.media_type == "application/x-dtbncx+xml"
    ]
    if len(candidates) != 1:
        raise EpubError("EPUB must have exactly one NCX navigation document")
    ncx_item, ncx_path = candidates[0]
    # Unlike other resources, the pinned engine reads NCX hrefs without decoding.
    if "%" in ncx_item.href:
        raise EpubError("encoded NCX paths are unsupported by EpubMerge")
    # The pinned engine resolves NCX targets relative to the OPF directory.
    if PurePosixPath(ncx_path).parent != PurePosixPath(package.opf_path).parent:
        raise EpubError("NCX must share the OPF directory for EpubMerge")
    root = _parse(archive.read(ncx_path), "invalid NCX navigation document")
    if root.tag != f"{{{NCX_NS}}}ncx":
        raise EpubError("invalid NCX navigation document")
    # The pinned engine compares raw tag names when copying NCX navigation.
    if any(element.prefix for element in root.iter(f"{{{NCX_NS}}}*")):
        raise EpubError("namespace-prefixed NCX elements are unsupported by EpubMerge")
    nav_maps = root.findall(f"{{{NCX_NS}}}navMap")
    if len(nav_maps) != 1:
        raise EpubError("NCX must have one navMap")
    if not nav_maps[0].findall(f"{{{NCX_NS}}}navPoint"):
        raise EpubError("NCX navMap is empty")
    for point in nav_maps[0].iter(f"{{{NCX_NS}}}navPoint"):
        labels = point.findall(f"{{{NCX_NS}}}navLabel")
        contents = point.findall(f"{{{NCX_NS}}}content")
        if (
            len(labels) != 1
            or not "".join(labels[0].itertext()).strip()
            or len(contents) != 1
            or not contents[0].get("src")
        ):
            raise EpubError("NCX navPoint is incomplete")
        src = contents[0].get("src")
        target = resolve_uri(ncx_path, src)
        if target is None or target[0] not in names:
            raise EpubError("NCX refers to a missing or external resource")
        if urlsplit(src).path.startswith("/"):
            raise EpubError("root-relative NCX targets are unsupported by EpubMerge")
