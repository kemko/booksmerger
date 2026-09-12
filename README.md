# bookmerger

`bookmerger` creates one EPUB 3.3 collection from ordered direct HTTP(S) links to FB2,
EPUB 2/3, or a ZIP containing exactly one FB2 file. Repeating a link deliberately
repeats that work in the collection.

## Installation

Python 3.12 or newer is required. From a checkout, install the locked project with
[uv](https://docs.astral.sh/uv/):

```sh
uv sync --all-groups
uv run bookmerger --help
```

Or install the package with `python -m pip install .`. The command is:

```sh
bookmerger --title "Collection title" --output collection.epub URL1 URL2
```

Use `--input-file sources.txt` instead of positional URLs when the list is long.
The UTF-8 file contains one URL per non-empty line; its order and duplicates are
preserved. `--input-file` and positional URLs cannot be combined.

## FB2 converter

FB2 input requires the pinned `fb2cng` 1.7.0 `fbc` executable. A compatible `fbc`
in `PATH` is used first. Otherwise bookmerger downloads the official, SHA-256-checked
release once and stores it in `$XDG_CACHE_HOME/bookmerger/fbc/1.7.0/<os>-<arch>/`
(`~/.cache/bookmerger/...` when `XDG_CACHE_HOME` is unset). No administrator rights
are needed. Later FB2 conversions reuse that cached binary, so they do not download
the converter again; source URLs still need network access for every build.

Supported converter binaries are macOS, Linux, and Windows on amd64 and arm64.
Incompatible PATH or cached converters are skipped; bookmerger installs the pinned
release automatically. Unsupported platforms and installation failures stop the build.

## Output and limits

The result is an EPUB 3.3 ZIP. `mimetype` is its first uncompressed entry; all other
entries use DEFLATE level 9 in stable order. Each source book stays under its own
`books/0001/`, `books/0002/`, and so on. The collection adds a title page, visible
contents, per-work bibliographic page, and combined navigation while retaining source
documents and their original links.

Raster images are copied byte-for-byte. SVG remains SVG, including text and linked
elements; only paths affected by moving resources are changed. Source styles,
annotations, footnotes and return links, ordinary embedded fonts, media, language,
and direction are retained. Required HTTP(S) resources, including nested CSS imports,
are downloaded with the source limits and included for offline reading. External
hyperlinks remain external. Collection styles apply only to new collection pages.

Creators, translators, and other contributors are merged in first-seen order with
their roles. Languages, subjects, and publisher data are retained. Work-specific
ISBNs, rights, series, and other publication details remain in the bibliography;
the collection does not claim a work's ISBN.

Downloads are limited to 100 MiB per source. ZIP input is limited to 10,000 entries
and 500 MiB expanded size. Catalog pages, authenticated sources, HTML, unsafe or
ambiguous ZIPs, DRM, and obfuscated fonts are unsupported. Source layout, orientation,
spread, and flow defaults are retained per spine item. Conflicting page progression
directions or media-overlay settings that cannot apply to one collection are rejected.
Other package rendition settings, including the deprecated rendition viewport, are
rejected; supporting them requires a source fixture and a preservation test.
A source error stops the build and leaves an existing output unchanged.
Bookmerger writes to a temporary file
beside the requested output, validates it, then publishes it atomically. Existing
outputs require `--overwrite` to be replaced.

To extend an unsupported format or construction, provide a real input sample and a
regression test that demonstrates the expected result.

## Reproducible local example

The regression test creates small EPUB 2 and EPUB 3 books, serves them locally, and
runs this command unchanged (with `BASE_URL` set to that temporary server):

```sh
bookmerger --title "Example collection" --output example.epub "$BASE_URL/book2.epub" "$BASE_URL/book3.epub"
```

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
