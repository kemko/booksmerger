"""Bounded, content-based download handling for source books."""

from __future__ import annotations

import hashlib
import io
import logging
import lzma
import os
import re
import stat
import time
import warnings
import zipfile
import zlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx
from lxml import etree

LOGGER = logging.getLogger(__name__)


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


def _default_cache_directory() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME")
    return Path(cache_home) if cache_home else Path.home() / ".cache"


def _safe_url(url: str) -> str:
    """Return a diagnostic URL without credentials or query parameters."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "source"
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


def _flibusta_download_url(url: str, parts: SplitResult) -> str:
    """Return Flibusta's download endpoint for a supported book page URL."""
    if (
        parts.hostname is None
        or parts.hostname.casefold() != "flibusta.is"
        or re.fullmatch(r"/b/[0-9]+/?", parts.path) is None
    ):
        return url
    path_start = len(parts.scheme) + 3 + len(parts.netloc)
    return (
        url[:path_start]
        + parts.path.rstrip("/")
        + "/download"
        + url[path_start + len(parts.path) :]
    )


def _safe_detail(detail: object) -> str:
    """Remove URLs, including credentials and query strings, from diagnostic text."""
    return re.sub(
        r"(?i)(?:https?|ftp)://\S+",
        lambda match: _safe_url(match.group()),
        str(detail),
    )


def _error(url: str, message: str) -> DownloadError:
    return DownloadError(f"{_safe_url(url)}: {message}")


def _network_error(url: str, error: httpx.HTTPError) -> DownloadError:
    detail = _safe_detail(error).strip()
    lowered = detail.casefold()
    if isinstance(error, httpx.TimeoutException):
        kind = type(error).__name__.removesuffix("Timeout").casefold() or "network"
        return _error(url, f"{kind} timeout")
    if isinstance(error, httpx.TooManyRedirects):
        return _error(url, "too many redirects")
    if any(marker in lowered for marker in ("ssl", "tls", "certificate")):
        reason = "TLS error"
    elif any(
        marker in lowered
        for marker in ("dns", "getaddrinfo", "name resolution", "nodename nor servname")
    ):
        reason = "DNS lookup failed"
    else:
        reason = f"network error ({type(error).__name__})"
    return _error(url, f"{reason}: {detail or 'no further detail'}")


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
                if archive.testzip() is not None:
                    raise _error(url, "invalid ZIP archive")
                return "epub", data
            if len(files) == 1:
                extracted = archive.read(files[0])
                if _is_fb2(extracted, url):
                    return "fb2", extracted
    except (
        OSError,
        zipfile.BadZipFile,
        RuntimeError,
        EOFError,
        UnicodeDecodeError,
        zlib.error,
        lzma.LZMAError,
    ) as error:
        if isinstance(error, DownloadError):
            raise
        raise _error(url, "invalid ZIP archive") from error
    raise _error(url, "ZIP must be an EPUB or contain one FB2 file")


def _validate(data: bytes, url: str, limits: DownloadLimits) -> tuple[str, bytes]:
    if len(data) > limits.max_download_bytes:
        raise _error(url, "download exceeds the allowed size")
    return _classify(data, url, limits)


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
        cache_directory: Path | None = None,
    ) -> None:
        self.limits = limits
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.retries = retries
        self.client = client
        self.cache_directory = (
            cache_directory or _default_cache_directory() / "bookmerger" / "sources"
        )

    def resource(self, url: str, temporary: Path) -> tuple[bytes, str, str]:
        """Fetch an embedded resource with the same limits as source downloads."""
        if urlsplit(url).scheme not in {"http", "https"}:
            raise _error(url, "only HTTP(S) resources are supported")
        LOGGER.info("Downloading external resource %s", _safe_url(url))
        try:
            return self._fetch(url, temporary)
        finally:
            temporary.unlink(missing_ok=True)

    def download_all(self, urls: Sequence[str], directory: Path) -> tuple[DownloadedSource, ...]:
        directory.mkdir(parents=True, exist_ok=True)
        downloaded: list[DownloadedSource] = []
        try:
            total = len(urls)
            for index, url in enumerate(urls, 1):
                started = time.monotonic()
                LOGGER.info("Downloading book %d/%d: %s", index, total, _safe_url(url))
                try:
                    downloaded.append(self.download(url, directory, index))
                except DownloadError as error:
                    raise DownloadError(f"book {index}/{total}: {error}") from error
                LOGGER.info(
                    "Finished downloading book %d/%d in %.2fs",
                    index,
                    total,
                    time.monotonic() - started,
                )
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
        request_url = _flibusta_download_url(url, parts)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f"source-{index:04d}.download"
        output: Path | None = None
        try:
            cached = self._read_cache(url)
            if cached is None:
                LOGGER.info("Source cache miss: %s", _safe_url(url))
                data, _, _ = self._fetch(request_url, temporary)
                format_name, contents = _validate(data, url, self.limits)
                self._write_cache(url, data)
            else:
                LOGGER.info("Source cache hit: %s", _safe_url(url))
                format_name, contents = cached
            output = directory / f"source-{index:04d}.{format_name}"
            output.write_bytes(contents)
            LOGGER.info("Downloaded book %d: %s, %d bytes", index, format_name, len(contents))
            return DownloadedSource(url, output, format_name)
        except Exception as error:
            if output is not None:
                output.unlink(missing_ok=True)
            if isinstance(error, OSError):
                detail = _safe_detail(error) or type(error).__name__
                raise _error(url, f"file operation failed: {detail}") from error
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def _cache_path(self, url: str) -> Path:
        key = url.partition("#")[0]
        return self.cache_directory / hashlib.sha256(key.encode()).hexdigest()

    def _warn_cache(self, url: str, error: OSError) -> None:
        warnings.warn(
            f"cannot use source cache for {_safe_url(url)}: {_safe_detail(error)}",
            RuntimeWarning,
            stacklevel=3,
        )

    def _read_cache(self, url: str) -> tuple[str, bytes] | None:
        path = self._cache_path(url)
        try:
            with path.open("rb") as cached:
                data = cached.read(self.limits.max_download_bytes + 1)
        except FileNotFoundError:
            return None
        except OSError as error:
            self._warn_cache(url, error)
            return None
        try:
            return _validate(data, url, self.limits)
        except DownloadError:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                self._warn_cache(url, error)
            return None

    def _write_cache(self, url: str, data: bytes) -> None:
        temporary: Path | None = None
        try:
            self.cache_directory.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="wb", prefix=".source-", dir=self.cache_directory, delete=False
            ) as target:
                temporary = Path(target.name)
                target.write(data)
            os.replace(temporary, self._cache_path(url))
            temporary = None
        except OSError as error:
            self._warn_cache(url, error)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as error:
                    self._warn_cache(url, error)

    def _fetch(self, url: str, temporary: Path) -> tuple[bytes, str, str]:
        LOGGER.debug(
            "Fetching %s (download limit %d bytes, retries %d)",
            _safe_url(url),
            self.limits.max_download_bytes,
            self.retries,
        )
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
                            error = _error(url, f"server returned HTTP {response.status_code}")
                            if attempt == self.retries:
                                raise error
                            self._retry(attempt, error)
                            continue
                        response.raise_for_status()
                        length = response.headers.get("content-length")
                        if length is not None:
                            try:
                                declared_size = int(length)
                            except ValueError as error:
                                raise _error(url, "invalid Content-Length header") from error
                            if declared_size < 0:
                                raise _error(url, "invalid Content-Length header")
                            if declared_size > self.limits.max_download_bytes:
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
                except httpx.TransportError as error:
                    if attempt == self.retries:
                        raise _network_error(url, error) from None
                    self._retry(attempt, _network_error(url, error))
            raise AssertionError("unreachable")
        except httpx.TooManyRedirects as error:
            raise _network_error(url, error) from None
        except httpx.HTTPStatusError as error:
            raise _error(url, f"server returned HTTP {error.response.status_code}") from None
        except httpx.HTTPError as error:
            raise _network_error(url, error) from None
        except OSError as error:
            detail = _safe_detail(error) or type(error).__name__
            raise _error(url, f"file operation failed: {detail}") from error
        finally:
            if owns_client:
                client.close()

    def _retry(self, attempt: int, error: DownloadError) -> None:
        LOGGER.info("Retry %d/%d after %s", attempt + 1, self.retries, error)


@contextmanager
def temporary_downloads(
    urls: Sequence[str], *, limits: DownloadLimits = DEFAULT_LIMITS, timeout: float = 20.0
) -> Iterator[tuple[DownloadedSource, ...]]:
    """Download ordered sources into a private directory removed on exit."""
    with TemporaryDirectory(prefix="bookmerger-") as name:
        yield Downloader(limits=limits, timeout=timeout).download_all(urls, Path(name))
