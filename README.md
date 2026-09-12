# bookmerger

`bookmerger` creates one EPUB 2 collection with
[EpubMerge](https://github.com/JimmXinu/EpubMerge) from ordered HTTP(S) links to FB2,
EPUB 2/3, or a ZIP containing exactly one FB2 file, served directly or through
redirects. FB2 is converted to EPUB 2 first. Every EPUB input must have exactly one
valid NCX in the same archive directory as its OPF package document; EPUB 3 with
such an NCX is accepted, but nav-only EPUB 3 is rejected. Repeating
a link deliberately repeats that work in the collection. A Flibusta book link such as
`https://flibusta.is/b/656901` (including a trailing slash) is requested as
`https://flibusta.is/b/656901/download`. Other HTML pages are unsupported.

## Installation

Python 3.12 or newer is required. From a checkout, install the locked project with
[uv](https://docs.astral.sh/uv/):

```sh
uv sync --all-groups
uv run bookmerger --help
```

Or install the package with `python -m pip install .`. The command is:

```sh
bookmerger URL1 URL2
```

Use `--input-file sources.txt` instead of positional URLs when the list is long.
The UTF-8 file contains one URL per non-empty line; its order and duplicates are
preserved. `--input-file` and positional URLs cannot be combined.

`--title` optionally sets both the collection title and the EPUB filename. The file
is saved as `<title>.epub` in the current directory; unsafe filename characters are
replaced and its UTF-8 stem is limited to 200 bytes, but the full title remains
inside the EPUB. Without `--title`, bookmerger derives
`Сборник — Автор 1, Автор 2 и др. — Произведение 1, Произведение 2 и др.` from source
metadata: one or two distinct authors and work titles are shown, then the first two
followed by `и др.`. Only authors are used (not translators or editors). An existing
output needs `--overwrite`, including an automatically named one.

The `--output` option has been removed. Use `--title "My collection"` to create
`My collection.epub`, and run from the desired output directory. The filename
cannot be set independently of the collection title.

Progress is logged to stderr at INFO level. Use `--verbose` for DEBUG diagnostics:

```sh
bookmerger --verbose URL1 URL2
bookmerger --title "My collection" URL1 URL2
```

## FB2 converter

FB2 input requires the pinned `fb2cng` 1.7.0 `fbc` executable. A compatible `fbc`
in `PATH` is used first. Otherwise bookmerger downloads the official, SHA-256-checked
release once and stores it in `$XDG_CACHE_HOME/bookmerger/fbc/1.7.0/<os>-<arch>/`
(`~/.cache/bookmerger/...` when `XDG_CACHE_HOME` is unset). No administrator rights
are needed. Later FB2 conversions reuse that cached binary, so they do not download
the converter again.

Supported converter binaries are macOS, Linux, and Windows on amd64 and arm64.
Incompatible PATH or cached converters are skipped; bookmerger installs the pinned
release automatically. Unsupported platforms and installation failures stop the build.

Downloaded source books are cached in `$XDG_CACHE_HOME/bookmerger/sources/`
(`~/.cache/bookmerger/sources/` when unset), so later builds reuse them even from
another working directory. The cache has no automatic refresh: remove an entry or
the `sources` directory to fetch the URL again. It covers source FB2, EPUB, and ZIP
files only. External links, images, and CSS stay external and are not downloaded;
offline reading is guaranteed only for resources already embedded in an input EPUB.

## Output and limits

The result is the EPUB 2 structure written by EpubMerge: its NCX contains the source
books in input order, with each source table of contents nested below its book. It has
no bookmerger title page, visible contents, bibliography, or added stylesheet. Source
files are copied under EpubMerge's numbered book directories. `mimetype` remains the
first uncompressed ZIP entry.

The collection title and input languages are passed to EpubMerge. Source authors help
derive the automatic collection title and filename. EpubMerge also collects authors
into the merged metadata; other book-specific contributors, roles,
subjects, publisher, identifiers, rights, series, and EPUB 3-specific metadata are
not guaranteed in the merged metadata. EPUB 3 inputs are processed through their NCX,
so EPUB 3 navigation and other EPUB 3-only features are not preserved as EPUB 3
features in the EPUB 2 result. Source XHTML, styles, images, fonts, media, footnotes,
and local links are passed to EpubMerge without bookmerger rewriting; compatibility is
limited to what EpubMerge preserves.

Downloads are limited to 100 MiB per source. ZIP input is limited to 10,000 entries
and 500 MiB expanded size. Catalog pages, authenticated sources, HTML, unsafe or
ambiguous ZIPs, DRM, obfuscated fonts, missing or ambiguous NCX documents,
namespace-prefixed NCX elements, and NCX
targets outside the EPUB are unsupported. A source error stops the build and leaves an
existing output unchanged.
NCX files outside the OPF directory are rejected because the pinned EpubMerge resolves
their targets relative to the OPF. Supporting that layout requires an engine update
and a navigation preservation regression.
Root-relative manifest paths and NCX targets are also rejected: the pinned engine
interprets them relative to the OPF directory and can select a different chapter.
Supporting them requires an engine fix and a resource-target preservation regression.
NCX targets with slashes in their query or fragment are rejected because the engine
normalizes the whole URI as a path and can redirect navigation to another chapter.
Supporting these suffixes requires an engine fix and a navigation preservation regression.
Percent-encoded NCX manifest paths are also rejected: the engine reads them without
decoding and can select a different archive entry. Supporting them requires an engine
fix and a regression proving that the validated NCX supplies the merged navigation.
Manifest resource paths containing literal `%`, `?`, or `#` in archive names are
rejected because the pinned engine emits them without URI escaping, which can
silently redirect the spine or TOC to another resource. Encoded spaces and Unicode
names remain supported for resources other than NCX. Supporting these reserved
characters requires an engine update and a resource-target preservation regression.
Bookmerger writes to a temporary file
beside the requested output, validates it, then publishes it atomically. Existing
outputs require `--overwrite` to be replaced.
Reference validation accounts for inherited `xml:base`, including SVG and inline CSS.
Bases that leave local resources missing after merging cause validation to fail.

To extend an unsupported format or construction, provide a real input sample and a
regression test that demonstrates the expected result.

## Reproducible local example

The regression test creates two small compatible EPUB 2 books, serves them locally,
and runs this command unchanged (with `BASE_URL` set to that temporary server):

```sh
bookmerger "$BASE_URL/book2.epub" "$BASE_URL/book3.epub"
```

It creates `Сборник — Author 2 — Fixture 2.epub`; a later run with the same URLs can
use the source cache without contacting the server.

Run it with:

```sh
uv run pytest -q tests/test_readme_example.py
```


## Development checks

The full suite requires Java and [EPUBCheck 5.2.1](https://github.com/w3c/epubcheck/releases/tag/v5.2.1).
Download and unpack its release ZIP, then set `EPUBCHECK` to the absolute path of
`epubcheck.jar`. The pinned fbc is found or installed automatically.

```sh
FBC_INTEGRATION=1 EPUBCHECK=/absolute/path/epubcheck.jar uv run pytest --cov=bookmerger --cov-report=term-missing --cov-fail-under=80
uv run ruff check .
uv run ruff format --check .
```

Without `FBC_INTEGRATION` and `EPUBCHECK`, their external-tool checks are skipped.
CI sets both variables and requires those checks to pass.
