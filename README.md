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
files only; externally referenced images and CSS are still fetched while preparing
each EPUB.

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
Custom OPF property prefixes are retained per book. Redefining reserved OPF prefixes
is unsupported; supporting that requires vocabulary-aware metadata conversion and a
preservation test.
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
bookmerger "$BASE_URL/book2.epub" "$BASE_URL/book3.epub"
```

It creates `Сборник — Author 2, Author 3 — Fixture 2, Fixture 3.epub`; a later
run with the same URLs can use the source cache without contacting the server.

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
