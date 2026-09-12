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

EpubMerge is a direct runtime dependency from the official GitHub archive for
upstream v3.3.0, commit `8cc76632722a94abc2ad08ff0f5cbda83ffb96eb`. The full-SHA
URL and archive SHA-256 are recorded in `pyproject.toml` and `uv.lock`,
respectively. The package imports as `epubmerge`; its supported API for this
project is `epubmerge.epubmerge.doMerge`, which was installed and imported in an
isolated Python 3.12 environment without Calibre. Its only declared runtime
dependency is `six`; this is resolved and pinned in `uv.lock`.

The selected upstream archive reports version 3.3.0. Its `setup.py` metadata
and installed wheel label the license Apache, while `LICENSE` and the
`epubmerge.py` header state GPL v3. This conflict is unresolved, so distribution
of this project must be treated as GPL v3 unless upstream supplies a correction.
The public repository and its v3.3.0 tag are the available maintenance evidence;
no support SLA, security policy, or independently verified vulnerability
advisory status was available during evaluation. The pinned archive reduces
source drift but does not replace upstream security maintenance.

EbookLib is not used. EpubMerge is the only merger; input validation reads ZIP
and XML metadata without unpacking or rewriting source content.
