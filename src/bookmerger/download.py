"""Bounded, content-based download handling for source books."""

from __future__ import annotations

import io
import stat
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit, urlunsplit

import httpx
from lxml import etree


class DownloadError(RuntimeError):
    """A source could not be safely downloaded or identified."""


@dataclass(frozen=True)
class DownloadLimits:
    """Limits that bound untrusted downloads and ZIP expansion."""

    max_download_bytes: int = 100 * 1024 * 1024
    max_zip_entries: int = 10_000
    max_zip_uncompressed_bytes: int = 500 * 1024 * 1024


@dataclass(frozen=True)
class DownloadedSource:
    """A validated source stored under a caller-controlled temporary directory."""

    url: str
    path: Path
    format: str


DEFAULT_LIMITS = DownloadLimits()


def _safe_url(url: str) -> str:
    """Return a diagnostic URL without credentials or query parameters."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "source"
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


def _error(url: str, message: str) -> DownloadError:
    return DownloadError(f"{_safe_url(url)}: {message}")


def _xml_root(data: bytes, url: str) -> etree._Element:
    if b"<!DOCTYPE" in data.upper():
        raise _error(url, "XML declarations are not supported")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        recover=False,
        huge_tree=False,
    )
    try:
        return etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as error:
        raise _error(url, "unsupported file format") from error


def _is_fb2(data: bytes, url: str) -> bool:
    root = _xml_root(data, url)
    return etree.QName(root).localname == "FictionBook"


def _is_safe_zip_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and "\0" not in name
        and "\\" not in name
        and not path.is_absolute()
        and not any(part in {"", ".", ".."} for part in path.parts)
        and not (path.parts and ":" in path.parts[0])
    )


def _validate_zip(
    archive: zipfile.ZipFile, url: str, limits: DownloadLimits
) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > limits.max_zip_entries:
        raise _error(url, "ZIP has too many entries")
    names: set[str] = set()
    total = 0
    files: list[zipfile.ZipInfo] = []
    for info in infos:
        if not _is_safe_zip_name(info.filename):
            raise _error(url, "ZIP contains an unsafe path")
        normalized = PurePosixPath(info.filename).as_posix()
        if normalized in names:
            raise _error(url, "ZIP contains duplicate paths")
        names.add(normalized)
        if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
            raise _error(url, "ZIP contains a symbolic link")
        total += info.file_size
        if total > limits.max_zip_uncompressed_bytes:
            raise _error(url, "ZIP expands beyond the allowed size")
        if not info.is_dir():
            files.append(info)
    return files


def _classify(data: bytes, url: str, limits: DownloadLimits) -> tuple[str, bytes]:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        if _is_fb2(data, url):
            return "fb2", data
        raise _error(url, "unsupported file format")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = _validate_zip(archive, url, limits)
            names = {info.filename for info in files}
            if (
                "mimetype" in names
                and archive.read("mimetype") == b"application/epub+zip"
                and "META-INF/container.xml" in names
            ):
                _xml_root(archive.read("META-INF/container.xml"), url)
                return "epub", data
            if len(files) == 1:
                extracted = archive.read(files[0])
                if _is_fb2(extracted, url):
                    return "fb2", extracted
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        if isinstance(error, DownloadError):
            raise
        raise _error(url, "invalid ZIP archive") from error
    raise _error(url, "ZIP must be an EPUB or contain one FB2 file")


class Downloader:
    """Download sources with bounded retries and no trust in response filenames."""

    def __init__(
        self,
        *,
        limits: DownloadLimits = DEFAULT_LIMITS,
        timeout: float = 20.0,
        max_redirects: int = 3,
        retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        self.limits = limits
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.retries = retries
        self.client = client

    def resource(self, url: str, temporary: Path) -> tuple[bytes, str, str]:
        """Fetch an embedded resource with the same limits as source downloads."""
        if urlsplit(url).scheme not in {"http", "https"}:
            raise _error(url, "only HTTP(S) resources are supported")
        try:
            return self._fetch(url, temporary)
        finally:
            temporary.unlink(missing_ok=True)

    def download_all(self, urls: Sequence[str], directory: Path) -> tuple[DownloadedSource, ...]:
        directory.mkdir(parents=True, exist_ok=True)
        downloaded: list[DownloadedSource] = []
        try:
            for index, url in enumerate(urls, 1):
                downloaded.append(self.download(url, directory, index))
            return tuple(downloaded)
        except Exception:
            for source in downloaded:
                source.path.unlink(missing_ok=True)
            raise

    def download(self, url: str, directory: Path, index: int = 1) -> DownloadedSource:
        try:
            parts = urlsplit(url)
        except ValueError as error:
            raise _error(url, "invalid source URL") from error
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise _error(url, "only absolute HTTP(S) URLs are supported")
        temporary = directory / f"source-{index:04d}.download"
        output: Path | None = None
        try:
            data, _, _ = self._fetch(url, temporary)
            format_name, contents = _classify(data, url, self.limits)
            output = directory / f"source-{index:04d}.{format_name}"
            output.write_bytes(contents)
            return DownloadedSource(url, output, format_name)
        except Exception:
            if output is not None:
                output.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def _fetch(self, url: str, temporary: Path) -> tuple[bytes, str, str]:
        owns_client = self.client is None
        client = self.client or httpx.Client(
            verify=True,
            follow_redirects=True,
            max_redirects=self.max_redirects,
            timeout=httpx.Timeout(self.timeout),
        )
        try:
            for attempt in range(self.retries + 1):
                try:
                    with client.stream("GET", url) as response:
                        if response.status_code in {408, 429} or response.status_code >= 500:
                            if attempt < self.retries:
                                continue
                            raise _error(url, f"server returned HTTP {response.status_code}")
                        response.raise_for_status()
                        length = response.headers.get("content-length")
                        if length is not None and int(length) > self.limits.max_download_bytes:
                            raise _error(url, "download exceeds the allowed size")
                        size = 0
                        with temporary.open("wb") as target:
                            for chunk in response.iter_bytes():
                                size += len(chunk)
                                if size > self.limits.max_download_bytes:
                                    raise _error(url, "download exceeds the allowed size")
                                target.write(chunk)
                    return (
                        temporary.read_bytes(),
                        response.headers.get("content-type", "").split(";", 1)[0].strip(),
                        str(response.url),
                    )
                except DownloadError:
                    raise
                except (
                    httpx.ConnectError,
                    httpx.ReadError,
                    httpx.ReadTimeout,
                    httpx.RemoteProtocolError,
                ):
                    if attempt == self.retries:
                        raise _error(url, "download failed") from None
            raise AssertionError("unreachable")
        except (httpx.HTTPError, ValueError) as error:
            raise _error(url, "download failed") from error
        finally:
            if owns_client:
                client.close()


@contextmanager
def temporary_downloads(
    urls: Sequence[str], *, limits: DownloadLimits = DEFAULT_LIMITS, timeout: float = 20.0
) -> Iterator[tuple[DownloadedSource, ...]]:
    """Download ordered sources into a private directory removed on exit."""
    with TemporaryDirectory(prefix="bookmerger-") as name:
        yield Downloader(limits=limits, timeout=timeout).download_all(urls, Path(name))
