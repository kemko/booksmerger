"""Pinned, safe FB2 conversion through fb2cng's ``fbc`` executable."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

import httpx
from lxml import etree


class FbcError(RuntimeError):
    """fb2cng cannot be installed or did not produce a usable EPUB."""


@dataclass(frozen=True)
class ReleaseAsset:
    """A hash-pinned fbc archive for one platform."""

    url: str
    sha256: str


@dataclass(frozen=True)
class FbcRelease:
    """The bundled fb2cng release manifest."""

    version: str
    assets: dict[str, ReleaseAsset]


@dataclass(frozen=True)
class FB2Image:
    """An unmodified binary image embedded by an FB2 document."""

    id: str
    media_type: str
    data: bytes


@dataclass(frozen=True)
class FB2Metadata:
    """Metadata needed when fbc omits fields from an EPUB package."""

    title: str | None
    translators: tuple[str, ...]


def _release() -> FbcRelease:
    raw = json.loads(resources.files("bookmerger.data").joinpath("fb2cng-release.json").read_text())
    return FbcRelease(
        raw["version"],
        {name: ReleaseAsset(**asset) for name, asset in raw["assets"].items()},
    )


RELEASE = _release()


def _platform_key(system: str | None = None, machine: str | None = None) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    operating_system = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(system)
    architecture = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine
    )
    if not operating_system or not architecture:
        raise FbcError(f"fb2cng {RELEASE.version} does not support {system}/{machine}")
    key = f"{operating_system}-{architecture}"
    if key not in RELEASE.assets:
        raise FbcError(f"fb2cng {RELEASE.version} does not support {system}/{machine}")
    return key


def _cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "bookmerger"


def _binary_name() -> str:
    return "fbc.exe" if platform.system().lower() == "windows" else "fbc"


def _safe_members(archive: zipfile.ZipFile, binary_name: str) -> Iterator[zipfile.ZipInfo]:
    for member in archive.infolist():
        path = PurePosixPath(member.filename)
        if (
            not member.filename
            or "\\" in member.filename
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or (member.external_attr >> 16) & 0o170000 == 0o120000
        ):
            raise FbcError("fb2cng archive has an unsafe path")
        if not member.is_dir() and path.name == binary_name:
            yield member


@contextmanager
def _install_lock(path: Path, timeout: float = 30.0) -> Iterator[None]:
    lock = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise FbcError("timed out waiting for another fb2cng installation") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)
        lock.unlink(missing_ok=True)


class FbcInstaller:
    """Find or install one hash-pinned fbc binary without administrator access."""

    def __init__(
        self,
        cache_root: Path | None = None,
        fetch: Callable[[str], bytes] | None = None,
    ) -> None:
        self.cache_root = cache_root or _cache_root()
        self.fetch = fetch or self._download

    def find(self) -> Path:
        path_binary = shutil.which("fbc")
        if path_binary:
            path = Path(path_binary)
            if self._compatible(path):
                return path
        key = _platform_key()
        cached = self.cache_root / "fbc" / RELEASE.version / key / _binary_name()
        if cached.exists() and self._compatible(cached):
            return cached
        return self._install(cached, RELEASE.assets[key])

    @staticmethod
    def _compatible(binary: Path) -> bool:
        try:
            completed = subprocess.run(
                [str(binary), "--version"], capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0 and f"version {RELEASE.version}" in (
            completed.stdout + completed.stderr
        )

    @staticmethod
    def _download(url: str) -> bytes:
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=30.0) as response:
                response.raise_for_status()
                return b"".join(response.iter_bytes())
        except httpx.HTTPError as error:
            raise FbcError("could not download fb2cng; check network access") from error

    def _install(self, target: Path, asset: ReleaseAsset) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _install_lock(target):
            if target.exists() and self._compatible(target):
                return target
            try:
                archive_data = self.fetch(asset.url)
            except FbcError:
                raise
            except Exception as error:
                raise FbcError("could not download fb2cng; check network access") from error
            if hashlib.sha256(archive_data).hexdigest() != asset.sha256:
                raise FbcError("downloaded fb2cng archive failed SHA-256 verification")
            try:
                with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
                    matches = list(_safe_members(archive, _binary_name()))
                    if len(matches) != 1:
                        raise FbcError("fb2cng archive does not contain exactly one fbc executable")
                    binary = archive.read(matches[0])
            except zipfile.BadZipFile as error:
                raise FbcError("downloaded fb2cng archive is invalid") from error
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(binary)
            temporary.chmod(0o700)
            os.replace(temporary, target)
            if not self._compatible(target):
                target.unlink(missing_ok=True)
                raise FbcError(
                    f"installed fbc is incompatible with required version {RELEASE.version}"
                )
            return target


def _fb2_root(path: Path) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
    )
    try:
        return etree.parse(str(path), parser).getroot()
    except (OSError, etree.XMLSyntaxError) as error:
        raise FbcError(f"cannot read FB2 {path}: {error}") from error


def fb2_images(path: Path) -> tuple[FB2Image, ...]:
    """Extract source image bytes before an external converter can alter them."""
    images: list[FB2Image] = []
    for binary in _fb2_root(path).xpath("//*[local-name()='binary']"):
        image_id = binary.get("id")
        media_type = binary.get("content-type")
        if not image_id or not media_type or binary.text is None:
            continue
        try:
            data = base64.b64decode(binary.text, validate=False)
        except ValueError as error:
            raise FbcError(f"FB2 binary {image_id!r} is not base64") from error
        images.append(FB2Image(image_id, media_type, data))
    return tuple(images)


def _person(element: etree._Element) -> str:
    values = [
        " ".join(part.itertext()).strip()
        for part in element.xpath(
            "./*[local-name()='first-name' or local-name()='middle-name' "
            "or local-name()='last-name' or local-name()='nickname']"
        )
    ]
    return " ".join(value for value in values if value)


def fb2_metadata(path: Path) -> FB2Metadata:
    root = _fb2_root(path)
    title = root.xpath(
        "string(//*[local-name()='title-info']/*[local-name()='book-title'][1])"
    ).strip()
    translators = tuple(
        name
        for translator in root.xpath("//*[local-name()='title-info']/*[local-name()='translator']")
        if (name := _person(translator))
    )
    return FB2Metadata(title or None, translators)


CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"


def _opf_path(archive: zipfile.ZipFile) -> str:
    try:
        container = etree.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find(f".//{{{CONTAINER_NS}}}rootfile")
    except (KeyError, etree.XMLSyntaxError) as error:
        raise FbcError("fbc output has no valid EPUB container") from error
    if rootfile is None or not rootfile.get("full-path"):
        raise FbcError("fbc output has no package document")
    return rootfile.get("full-path")


def _restore_epub(output: Path, images: tuple[FB2Image, ...], metadata: FB2Metadata) -> None:
    """Restore source image bytes and translator metadata in fbc's EPUB."""
    with zipfile.ZipFile(output) as source:
        opf_name = _opf_path(source)
        try:
            opf = etree.fromstring(source.read(opf_name))
        except (KeyError, etree.XMLSyntaxError) as error:
            raise FbcError("fbc output has an invalid package document") from error
        opf_directory = PurePosixPath(opf_name).parent
        manifest = opf.find(f"{{{OPF_NS}}}manifest")
        if manifest is None:
            raise FbcError("fbc output has no manifest")
        replacements: dict[str, bytes] = {}
        available = list(manifest)
        image_items = [
            item for item in available if item.get("media-type", "").startswith("image/")
        ]
        for image in images:
            candidates = [
                item
                for item in image_items
                if item in available
                and source.read((opf_directory / unquote(item.get("href", ""))).as_posix())
                == image.data
            ]
            if len(candidates) != 1:
                candidates = [
                    item
                    for item in image_items
                    if item in available
                    and PurePosixPath(unquote(item.get("href", ""))).name == image.id
                ]
            if not candidates:
                candidates = [
                    item
                    for item in image_items
                    if item in available
                    and PurePosixPath(unquote(item.get("href", ""))).stem
                    in {image.id, PurePosixPath(image.id).stem}
                ]
            if not candidates:
                candidates = [
                    item for item in available if item.get("media-type") == image.media_type
                ]
            if not candidates and len(images) == len(image_items):
                candidates = [item for item in image_items if item in available]
            if len(candidates) != 1:
                raise FbcError(f"cannot safely match converted FB2 image {image.id!r}")
            item = candidates[0]
            name = (opf_directory / item.get("href", "")).as_posix()
            if name not in source.namelist():
                raise FbcError(f"fbc output is missing image {image.id!r}")
            replacements[name] = image.data
            item.set("media-type", image.media_type)
            available.remove(item)
        package_metadata = opf.find(f"{{{OPF_NS}}}metadata")
        if package_metadata is not None:
            translator_ids = {
                item.get("refines", "").removeprefix("#")
                for item in package_metadata
                if item.tag == f"{{{OPF_NS}}}meta"
                and item.get("property") == "role"
                and "".join(item.itertext()).strip() == "trl"
            }
            translators = {
                tuple(sorted(" ".join(item.itertext()).casefold().split()))
                for item in package_metadata
                if item.tag == f"{{{DC_NS}}}contributor"
                and (item.get("id") in translator_ids or item.get(f"{{{OPF_NS}}}role") == "trl")
            }
            for translator in metadata.translators:
                normalized = tuple(sorted(translator.casefold().split()))
                if normalized not in translators:
                    contributor = etree.SubElement(package_metadata, f"{{{DC_NS}}}contributor")
                    contributor_id = f"bookmerger-translator-{len(translators)}"
                    contributor.set("id", contributor_id)
                    contributor.text = translator
                    role = etree.SubElement(package_metadata, f"{{{OPF_NS}}}meta")
                    role.set("refines", f"#{contributor_id}")
                    role.set("property", "role")
                    role.set("scheme", "marc:relators")
                    role.text = "trl"
                    translators.add(normalized)
        replacements[opf_name] = etree.tostring(opf, xml_declaration=True, encoding="utf-8")
        temporary = output.with_suffix(".restored.epub")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as restored:
            for info in source.infolist():
                restored.writestr(info, replacements.get(info.filename, source.read(info.filename)))
    os.replace(temporary, output)


class FB2Converter:
    """Convert one FB2 in a private work directory with a pinned fbc release."""

    def __init__(self, installer: FbcInstaller | None = None, config: Path | None = None) -> None:
        self.installer = installer or FbcInstaller()
        self.config = config or Path(resources.files("bookmerger.data").joinpath("fb2cng.yaml"))

    def convert(self, source: Path, output: Path, timeout: float = 120.0) -> FB2Metadata:
        if not source.is_file():
            raise FbcError(f"FB2 source does not exist: {source}")
        binary = self.installer.find()
        images = fb2_images(source)
        metadata = fb2_metadata(source)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="bookmerger-fbc-", dir=output.parent) as work_name:
            work = Path(work_name).resolve()
            converted = work / "converted.epub"
            try:
                completed = subprocess.run(
                    [
                        str(binary),
                        "--config",
                        str(self.config),
                        "convert",
                        "--to",
                        "epub3",
                        "--output-file",
                        str(converted),
                        "--overwrite",
                        str(source.resolve()),
                    ],
                    cwd=work,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise FbcError("fb2cng conversion timed out") from error
            if completed.returncode:
                detail = (completed.stderr or completed.stdout).strip()
                raise FbcError(f"fb2cng conversion failed: {detail or 'unknown error'}")
            if not converted.is_file():
                raise FbcError("fb2cng did not create an EPUB")
            _restore_epub(converted, images, metadata)
            output.parent.mkdir(parents=True, exist_ok=True)
            os.replace(converted, output)
        return metadata
