# Releasing ForgeMCP

ForgeMCP ships as one Python package. Its wheel contains the compiled frontend
widgets and separate icons; no frontend hosting or npm publication is needed.

## Prepare and verify

1. Update the version in `pyproject.toml` and `src/forgemcp/__init__.py` together.
2. Add release notes at `docs/releases/v<version>.md` and update README install examples.
3. Use an empty output directory, so packages from previous versions cannot be uploaded accidentally.
4. Build both distributions and verify them:

```powershell
.\.venv\Scripts\python.exe -m pip install build twine
.\.venv\Scripts\python.exe -m build
.\.venv\Scripts\python.exe scripts/verify_release.py dist --tag v0.2.1 --checksums
.\.venv\Scripts\python.exe -m twine check --strict dist/*.whl dist/*.tar.gz
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

The default `build` command builds an sdist first, then builds the wheel from that
sdist. This verifies that frontend inputs are present in the source archive.
The wheel-only command in README remains available for quicker development builds.
Both source builds require Node.js; wheel installation does not.

Install the wheel in a fresh environment, then run the isolated smoke check:

```powershell
python -m venv .release-venv
.\.release-venv\Scripts\python.exe -m pip install .\dist\forge_cpp_mcp-0.2.1-py3-none-any.whl
.\.release-venv\Scripts\python.exe -m pip check
.\.release-venv\Scripts\forgemcp.exe --help
.\.release-venv\Scripts\python.exe -I scripts/smoke_install.py
```

On macOS/Linux, use the environment's `bin/python` and `bin/forgemcp` paths.
The smoke check starts the installed server through the SDK's in-process Client,
reads every tool-linked App resource, checks metadata, and performs a workspace read.
It verifies packaging and protocol availability; widget appearance is reviewed manually.

## GitHub release

CI builds the distributions and tests the installed wheel on Windows and Linux
with Python 3.13 and 3.14. It overrides pytest's source-path setting so the tests
exercise the packaged code. The previous standalone Pylint workflow is replaced
by this distribution and service/protocol verification.

After the preparation PR passes CI and is merged, tag the reviewed release commit:

```powershell
git tag -a v0.2.1 <release-commit-sha> -m "ForgeMCP 0.2.1"
git push origin v0.2.1
```

`release.yml` runs the same build/test pipeline, checks that the tag matches the
package and runtime versions, and creates a **draft** GitHub release containing the exact checked
wheel, sdist, and checksums. Review its notes and files, then publish the draft.
The workflow deliberately does not publish to PyPI. If it fails after the draft was
created, inspect that draft before rerunning; `gh release create` does not replace
an existing release. Do not move a published version's tag or replace its packages.

## PyPI: final, manually triggered step

For the first publication, configure a pending Trusted Publisher in your PyPI account:

| Field | Value |
| --- | --- |
| PyPI project name | `forge-cpp-mcp` |
| GitHub owner | `hardened-steel` |
| Repository | `ForgeMCP` |
| Workflow filename | `publish-pypi.yml` |
| Environment | `pypi` |

Create the GitHub environment `pypi`, restrict deployments to `main`, and configure
a required reviewer where supported. No PyPI API token is needed.
See [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).

When the GitHub release is public and the publisher is configured, run:

```powershell
gh workflow run publish-pypi.yml --ref main -f tag=v0.2.1
```

Alternatively, use Actions → Publish to PyPI → Run workflow on `main`.
The workflow rejects drafts, downloads the release's existing wheel and sdist,
verifies their metadata and SHA-256 checksums, then uploads those same files to PyPI
through OIDC. It does not rebuild or publish automatically on tag pushes.
After success, verify `python -m pip install forge-cpp-mcp==0.2.1` in a fresh environment.

GitHub's automatic source ZIP/tar archives are repository snapshots, not the Python
sdist. Use the explicit `.tar.gz` asset when rebuilding the Python distribution.
