# Dependency decisions

`pyproject.toml` declares minimum tested dependency versions with `>=`, allowing
future releases. Runtime and development versions are locked in `uv.lock` for
reproducible installs; build-system requirements are resolved separately by uv.
Use `uv lock --upgrade` and `uv sync --all-groups --locked` to update dependencies,
then run the development checks in `README.md` before committing the lockfile.
CI installs the committed versions with `uv sync --all-groups --frozen`.

HTTPX (BSD) supplies the HTTP client required for bounded streaming downloads;
lxml (BSD) supplies hardened XML parsing; tinycss2 (BSD) preserves CSS token
structure while rewriting links. Their current supported Python versions cover
3.12, and `uv` resolves their small, maintained dependency graphs.

EbookLib is included only as a test dependency to document its evaluation;
it is not used by the application. Its writer rebuilds the OPF paths and drops
an unreferenced NCX (verified by `tests/test_ebooklib_evaluation.py`), while this project must retain
unknown markup, namespace prefixes, byte-identical raster resources, and the
original manifest/spine structure. Later EPUB work will use `zipfile` for
copying and `lxml` only for the addressed XML edits. The preservation test
locks in representative EPUB2 and EPUB3 documents, CSS, PNG, and SVG inputs.
