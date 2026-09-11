"""Collection-only EPUB documents, navigation, and merged metadata."""

from __future__ import annotations

from datetime import date
from importlib import resources
from pathlib import Path, PurePosixPath
from urllib.parse import urlunsplit
from uuid import UUID, uuid4

from lxml import etree

from bookmerger.epub import Contributor, ManifestItem, SpineItem, StagedBook
from bookmerger.references import resolve_uri

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"


class CollectionError(RuntimeError):
    """Collection front matter or navigation cannot be built safely."""


def _xhtml(title: str) -> tuple[etree._Element, etree._Element]:
    root = etree.Element(f"{{{XHTML_NS}}}html", nsmap={None: XHTML_NS, "epub": EPUB_NS})
    head = etree.SubElement(root, f"{{{XHTML_NS}}}head")
    etree.SubElement(head, f"{{{XHTML_NS}}}title").text = title
    link = etree.SubElement(head, f"{{{XHTML_NS}}}link")
    link.set("rel", "stylesheet")
    link.set("href", "styles/collection.css")
    return root, etree.SubElement(root, f"{{{XHTML_NS}}}body")


def _write_xhtml(path: Path, root: etree._Element) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(etree.tostring(root, xml_declaration=True, encoding="utf-8"))


def _relative(target: str) -> str:
    return (PurePosixPath("..") / target).as_posix()


def _normal(value: str) -> str:
    return " ".join(value.casefold().split())


def merged_contributors(books: tuple[StagedBook, ...]) -> tuple[Contributor, ...]:
    """Preserve first occurrence unless name, role, and source ID exactly repeat."""
    result: list[Contributor] = []
    seen: set[tuple[str, str, str]] = set()
    for book in books:
        for person in book.package.metadata.contributors:
            key = (_normal(person.name), person.role.casefold(), person.identifier or "")
            if key not in seen:
                seen.add(key)
                result.append(person)
    return tuple(result)


def _navigation(
    book: StagedBook, directory: Path, nav_type: str = "toc"
) -> list[tuple[str, str, list[object]]]:
    """Read the source NAV/NCX tree after staging, preserving every depth."""
    for nav_path in book.package.navigation:
        path = directory / nav_path
        if not path.exists():
            continue
        try:
            root = etree.fromstring(path.read_bytes())
        except etree.XMLSyntaxError as error:
            raise CollectionError(f"invalid navigation in book {book.number}") from error
        if etree.QName(root).namespace == NCX_NS:
            if nav_type != "toc":
                continue

            def ncx(
                node: etree._Element, document: str = nav_path
            ) -> tuple[str, str, list[object]]:
                label = "".join(
                    node.xpath("./n:navLabel/n:text/text()", namespaces={"n": NCX_NS})
                ).strip()
                source = node.xpath("string(./n:content/@src)", namespaces={"n": NCX_NS})
                children = [ncx(item, document) for item in node.findall(f"{{{NCX_NS}}}navPoint")]
                return label or source, _mapped_target(book, document, source), children

            nodes = root.findall(f".//{{{NCX_NS}}}navMap/{{{NCX_NS}}}navPoint")
            return [ncx(node) for node in nodes]
        navs = root.xpath(
            "//*[local-name()='nav' and "
            "contains(concat(' ', normalize-space(@epub:type), ' '), $type)]",
            namespaces={"epub": EPUB_NS},
            type=f" {nav_type} ",
        )
        if not navs:
            continue

        def xhtml(item: etree._Element, document: str = nav_path) -> tuple[str, str, list[object]]:
            anchor = item.xpath("./x:a[1]", namespaces={"x": XHTML_NS})
            if not anchor:
                return "", "", []
            link = anchor[0]
            source = link.get("href", "")
            children = item.xpath("./x:ol/x:li", namespaces={"x": XHTML_NS})
            descendants = [xhtml(child, document) for child in children]
            return (
                "".join(link.itertext()).strip() or source,
                _mapped_target(book, document, source),
                descendants,
            )

        return [xhtml(item) for item in navs[0].xpath("./x:ol/x:li", namespaces={"x": XHTML_NS})]
    return []


def _mapped_target(book: StagedBook, document: str, source: str) -> str:
    original = next(
        (name for name, staged in book.resource_map.resources.items() if staged == document),
        None,
    )
    if original is None:
        return source
    resolved = resolve_uri(original, source)
    if resolved is None:
        return source
    path, query, fragment = resolved
    target = book.resource_map.resources.get(path)
    if target is None:
        return source
    return urlunsplit(("", "", _relative(target), query, fragment))


def _tree(parent: etree._Element, nodes: list[tuple[str, str, list[object]]]) -> None:
    listing = etree.SubElement(parent, f"{{{XHTML_NS}}}ol")
    for label, target, children in nodes:
        item = etree.SubElement(listing, f"{{{XHTML_NS}}}li")
        anchor = etree.SubElement(item, f"{{{XHTML_NS}}}a", href=target)
        anchor.text = label
        if children:
            _tree(item, children)  # type: ignore[arg-type]


def _auxiliary_navigation(
    body: etree._Element, books: tuple[StagedBook, ...], directory: Path, nav_type: str
) -> None:
    trees = [(book, _navigation(book, directory, nav_type)) for book in books]
    trees = [(book, tree) for book, tree in trees if tree]
    if not trees:
        return
    nav = etree.SubElement(body, f"{{{XHTML_NS}}}nav")
    nav.set(f"{{{EPUB_NS}}}type", nav_type)
    etree.SubElement(nav, f"{{{XHTML_NS}}}h2").text = nav_type.replace("-", " ").title()
    listing = etree.SubElement(nav, f"{{{XHTML_NS}}}ol")
    for book, tree in trees:
        for label, target, _ in tree:
            item = etree.SubElement(listing, f"{{{XHTML_NS}}}li")
            anchor = etree.SubElement(item, f"{{{XHTML_NS}}}a", href=target)
            anchor.text = f"{book.package.metadata.title}: {label}"
            if nav_type == "landmarks":
                anchor.set(f"{{{EPUB_NS}}}type", "bodymatter")


def _book_page(book: StagedBook, directory: Path) -> tuple[str, bool]:
    """Create title/metadata front matter and return its path and cover state."""
    name = f"book-{book.number:04d}.xhtml"
    root, body = _xhtml(book.package.metadata.title)
    body.set("class", "book-divider")
    etree.SubElement(body, f"{{{XHTML_NS}}}h1").text = book.package.metadata.title
    for contributor in book.package.metadata.contributors:
        paragraph = etree.SubElement(body, f"{{{XHTML_NS}}}p")
        paragraph.text = f"{contributor.role}: {contributor.name}"
    if book.package.metadata.publisher:
        etree.SubElement(body, f"{{{XHTML_NS}}}p").text = book.package.metadata.publisher
    _write_xhtml(directory / "EPUB" / name, root)
    cover_in_spine = book.package.cover in {
        next((item.href for item in book.package.manifest if item.id == spine.idref), "")
        for spine in book.package.spine
    }
    return name, bool(book.package.cover and not cover_in_spine)


def _cover_page(book: StagedBook, directory: Path) -> str | None:
    if not book.package.cover:
        return None
    name = f"cover-{book.number:04d}.xhtml"
    root, body = _xhtml(f"Cover: {book.package.metadata.title}")
    body.set("class", "cover")
    image = etree.SubElement(body, f"{{{XHTML_NS}}}img")
    image.set("src", _relative(book.package.cover))
    image.set("alt", f"Cover: {book.package.metadata.title}")
    _write_xhtml(directory / "EPUB" / name, root)
    return name


def _unique(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and _normal(value) not in seen:
            seen.add(_normal(value))
            result.append(value)
    return tuple(result)


def build_collection(
    title: str,
    books: tuple[StagedBook, ...],
    directory: Path,
    *,
    identifier: UUID | None = None,
    published: date | None = None,
) -> Path:
    """Write collection-only EPUB files beside staged source resources."""
    if not books:
        raise CollectionError("a collection needs at least one book")
    epub = directory / "EPUB"
    (epub / "styles").mkdir(parents=True, exist_ok=True)
    (epub / "styles" / "collection.css").write_text(
        resources.files("bookmerger.data").joinpath("collection.css").read_text(), encoding="utf-8"
    )
    identifier = identifier or uuid4()
    published = published or date.today()
    contributors = merged_contributors(books)
    languages = _unique(
        [language for book in books for language in book.package.metadata.languages]
    )
    subjects = _unique([subject for book in books for subject in book.package.metadata.subjects])
    title_root, title_body = _xhtml(title)
    title_body.set("class", "title-page")
    etree.SubElement(title_body, f"{{{XHTML_NS}}}h1").text = title
    etree.SubElement(title_body, f"{{{XHTML_NS}}}p").text = f"Collection, {published.isoformat()}"
    _write_xhtml(epub / "title.xhtml", title_root)
    bibliography_root, bibliography_body = _xhtml("Bibliography")
    bibliography_body.set("class", "bibliography")
    etree.SubElement(bibliography_body, f"{{{XHTML_NS}}}h1").text = "Bibliography"
    listing = etree.SubElement(bibliography_body, f"{{{XHTML_NS}}}dl")
    for book in books:
        etree.SubElement(listing, f"{{{XHTML_NS}}}dt").text = book.package.metadata.title
        facts = [f"{name}: {value}" for name, value in book.package.metadata.details]
        detail = "; ".join(facts) or "Source metadata retained with this work."
        etree.SubElement(listing, f"{{{XHTML_NS}}}dd").text = detail
    _write_xhtml(epub / "bibliography.xhtml", bibliography_root)
    nav_root, nav_body = _xhtml("Contents")
    nav = etree.SubElement(nav_body, f"{{{XHTML_NS}}}nav")
    nav.set(f"{{{EPUB_NS}}}type", "toc")
    etree.SubElement(nav, f"{{{XHTML_NS}}}h1").text = "Contents"
    outer = etree.SubElement(nav, f"{{{XHTML_NS}}}ol")
    source_items: list[ManifestItem] = []
    spine: list[SpineItem] = [SpineItem("title", True), SpineItem("toc", True)]
    additions = [
        ManifestItem("title", "title.xhtml", "application/xhtml+xml"),
        ManifestItem("toc", "toc.xhtml", "application/xhtml+xml"),
        ManifestItem("nav", "nav.xhtml", "application/xhtml+xml", ("nav",)),
        ManifestItem("bibliography", "bibliography.xhtml", "application/xhtml+xml"),
        ManifestItem("collection-css", "styles/collection.css", "text/css"),
    ]
    for book in books:
        front, needs_cover = _book_page(book, directory)
        if needs_cover and (cover := _cover_page(book, directory)):
            cover_id = f"cover-page-{book.number:04d}"
            additions.append(ManifestItem(cover_id, cover, "application/xhtml+xml"))
            spine.append(SpineItem(cover_id, True))
        front_id = f"front-{book.number:04d}"
        additions.append(ManifestItem(front_id, front, "application/xhtml+xml"))
        spine.append(SpineItem(front_id, True))
        node = etree.SubElement(outer, f"{{{XHTML_NS}}}li")
        anchor = etree.SubElement(node, f"{{{XHTML_NS}}}a", href=front)
        anchor.text = book.package.metadata.title
        tree = _navigation(book, directory)
        if tree:
            _tree(node, tree)
        source_manifest = tuple(
            ManifestItem(
                item.id,
                item.href,
                item.media_type,
                tuple(
                    property
                    for property in item.properties
                    if property not in {"cover-image", "nav"}
                ),
            )
            for item in book.package.manifest
        )
        source_items.extend(
            item for item in source_manifest if item.media_type != "application/x-dtbncx+xml"
        )
        source_ids = {item.id for item in source_items}
        spine.extend(item for item in book.package.spine if item.idref in source_ids)
        extras = [
            item
            for item in source_manifest
            if item.id in source_ids
            and (
                item.href in book.package.navigation
                or item.id in {spine.idref for spine in book.package.spine if not spine.linear}
            )
        ]
        if extras:
            extra_list = next(iter(node.findall(f"{{{XHTML_NS}}}ol")), None)
            if extra_list is None:
                extra_list = etree.SubElement(node, f"{{{XHTML_NS}}}ol")
            for item in extras:
                extra = etree.SubElement(extra_list, f"{{{XHTML_NS}}}li")
                link = etree.SubElement(extra, f"{{{XHTML_NS}}}a", href=_relative(item.href))
                link.text = (
                    "Original contents"
                    if item.href in book.package.navigation
                    else "Supplementary content"
                )
                if item.href in book.package.navigation:
                    spine.append(SpineItem(item.id, False))
    _auxiliary_navigation(nav_body, books, directory, "page-list")
    _auxiliary_navigation(nav_body, books, directory, "landmarks")
    spine.append(SpineItem("bibliography", True))
    _write_xhtml(epub / "nav.xhtml", nav_root)
    _write_xhtml(epub / "toc.xhtml", nav_root)
    return _write_opf(
        epub / "package.opf",
        title,
        identifier,
        published,
        contributors,
        languages,
        subjects,
        additions + source_items,
        spine,
    )


def _write_opf(
    path: Path,
    title: str,
    identifier: UUID,
    published: date,
    contributors: tuple[Contributor, ...],
    languages: tuple[str, ...],
    subjects: tuple[str, ...],
    manifest: list[ManifestItem],
    spine: list[SpineItem],
) -> Path:
    package = etree.Element(
        f"{{{OPF_NS}}}package",
        nsmap={None: OPF_NS, "dc": DC_NS, "dcterms": "http://purl.org/dc/terms/"},
    )
    package.set("version", "3.0")
    package.set("unique-identifier", "bookmerger-id")
    metadata = etree.SubElement(package, f"{{{OPF_NS}}}metadata")
    item = etree.SubElement(metadata, f"{{{DC_NS}}}identifier", id="bookmerger-id")
    item.text = f"urn:uuid:{identifier}"
    etree.SubElement(metadata, f"{{{DC_NS}}}title").text = title
    etree.SubElement(metadata, f"{{{DC_NS}}}date").text = published.isoformat()
    modified = etree.SubElement(metadata, f"{{{OPF_NS}}}meta", property="dcterms:modified")
    modified.text = f"{published.isoformat()}T00:00:00Z"
    for language in languages:
        etree.SubElement(metadata, f"{{{DC_NS}}}language").text = language
    for subject in subjects:
        etree.SubElement(metadata, f"{{{DC_NS}}}subject").text = subject
    for index, person in enumerate(contributors, 1):
        name = "creator" if person.role == "aut" else "contributor"
        contributor = etree.SubElement(metadata, f"{{{DC_NS}}}{name}", id=f"contributor-{index}")
        contributor.text = person.name
        role = etree.SubElement(metadata, f"{{{OPF_NS}}}meta")
        role.set("refines", f"#contributor-{index}")
        role.set("property", "role")
        role.set("scheme", "marc:relators")
        role.text = person.role
    manifest_node = etree.SubElement(package, f"{{{OPF_NS}}}manifest")
    for item in manifest:
        href = _relative(item.href) if item.href.startswith("books/") else item.href
        node = etree.SubElement(
            manifest_node,
            f"{{{OPF_NS}}}item",
            id=item.id,
            href=href,
            **{"media-type": item.media_type},
        )
        if item.properties:
            node.set("properties", " ".join(item.properties))
    spine_node = etree.SubElement(package, f"{{{OPF_NS}}}spine")
    for item in spine:
        node = etree.SubElement(spine_node, f"{{{OPF_NS}}}itemref", idref=item.idref)
        if not item.linear:
            node.set("linear", "no")
    path.write_bytes(etree.tostring(package, xml_declaration=True, encoding="utf-8"))
    return path
