# Repository Guidelines

## Project Structure & Module Organization

`bookmerger` is a Python 3.12+ CLI that combines ordered FB2 and EPUB sources into
an EPUB 3.3 collection. Source code lives in `src/bookmerger/`: `cli.py` orchestrates
builds, `download.py` handles downloads and caching, `converter.py` integrates
`fb2cng`, and `epub.py`, `references.py`, `collection.py`, and `validate.py` handle
EPUB processing. Packaged CSS and converter configuration live in
`src/bookmerger/data/`. Tests reside in `tests/`, shared fixtures in
`tests/conftest.py`, and sample files in `tests/fixtures/`. Dependency decisions and
completed plans are under `docs/`.

## Build, Test, and Development Commands

- `uv sync --all-groups --frozen`: install dependencies from `uv.lock`, matching CI.
- `uv run bookmerger --help`: inspect CLI options.
- `uv run bookmerger --title "My collection" URL1 URL2`: build a collection as
  `My collection.epub` in the current directory.
- `uv build`: build source and wheel distributions with Hatchling.
- `uv run pytest`: run tests; external-tool checks require the configuration below.
- `uv run ruff check .`: check lint rules and import ordering.
- `uv run ruff format --check .`: verify formatting; omit `--check` to apply it.

## Coding Style & Naming Conventions

Use four-space indentation, a 100-character line limit, and modern Python type
annotations. Follow existing `snake_case` functions and modules, `PascalCase`
classes, and `UPPER_SNAKE_CASE` constants. Ruff configuration in `pyproject.toml`
covers style, imports, upgrades, and common bugs. Keep changes focused and reuse
existing helpers.

## Testing Guidelines

Use pytest with `test_*.py` files and descriptive `test_*` functions. Reuse
`epub_factory`, `edit_epub`, `tmp_path`, and mocked HTTP transports. Add regression
tests for behavior changes and preservation tests when extending supported inputs.
CI requires at least 80% coverage with branch measurement enabled.

For the full suite, install Java and EPUBCheck 5.2.1, then run:

```sh
FBC_INTEGRATION=1 EPUBCHECK=/absolute/path/epubcheck.jar uv run pytest --cov=bookmerger --cov-report=term-missing --cov-fail-under=80
```

The pinned `fbc` converter is discovered or installed automatically. Without these
environment variables, external-tool checks are skipped.

## Commit & Pull Request Guidelines

History commonly uses short imperative subjects with `feat:` or `fix:` prefixes.
Follow that convention for code changes. PR descriptions should explain the
behavior change, link relevant issues, and report tests and lint checks, including
any skipped integration checks. Update `README.md` when CLI behavior changes.

## Data Safety

Preserve download and archive limits, source assets and links, and atomic output
publication. Existing outputs require `--overwrite`; failures must leave them
unchanged. Keep converter versions and checksums pinned.
