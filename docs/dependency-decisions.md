# Dependency decisions

Dependencies are pinned in `pyproject.toml` after testing with Python 3.12 and
an audit of the resolved third-party requirements with pip-audit.
HTTPX (BSD) supplies the HTTP client required for bounded streaming downloads;
lxml (BSD) supplies hardened XML parsing; tinycss2 (BSD) preserves CSS token
structure while rewriting links. Their current supported Python versions cover
3.12, and `uv` resolves their small, maintained dependency graphs.

EbookLib 0.20 is pinned only as a test dependency to document its evaluation;
it is not used by the application. Its writer rebuilds the OPF paths and drops
an unreferenced NCX (verified by `tests/test_ebooklib_evaluation.py`), while this project must retain
unknown markup, namespace prefixes, byte-identical raster resources, and the
original manifest/spine structure. Later EPUB work will use `zipfile` for
copying and `lxml` only for the addressed XML edits. The preservation test
locks in representative EPUB2 and EPUB3 documents, CSS, PNG, and SVG inputs.
