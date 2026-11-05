# PyPI package and releases

DOPPLERGEIST is published on [PyPI](https://pypi.org/project/dopplergeist/) as `dopplergeist`. The first published version is `0.1.1` (November 4, 2026). The Python package and command-line program both use the name `dopplergeist`.

## Install

Requirements: **Python 3.11+** and **FFmpeg**. Intel Macs require Python 3.11–3.13; Python 3.14 on Intel macOS is not supported. Install `ffmpeg` and `ffprobe` separately and make sure that both commands are on your `PATH`.

For a standalone command-line installation, use `pipx`:

```bash
pipx install dopplergeist
dopplergeist --help
```

To use the package in an existing Python environment instead:

```bash
python -m pip install dopplergeist
dopplergeist --help
```

The wheel provides the Python package, CLI, and local browser application. It does **not** bundle the FFmpeg executables or aircraft and terrain data. See the [Quickstart](../README.md#quickstart) and [Setup](SETUP.md) guides.

## Maintainer: publish a new release

The project publishes with the [Python distribution and PyPI](../.github/workflows/python-package.yml) GitHub Actions workflow. A GitHub release with a `v<project.version>` tag triggers publication. The upload job authenticates to PyPI through GitHub's trusted-publishing OIDC identity. It does not need a long-lived PyPI API token.

The original trusted-publisher setup uses these identifiers:

- PyPI project: `dopplergeist`
- GitHub owner/repository: `1337isnot1337/DOPPLERGEIST`
- Workflow filename: `python-package.yml`
- GitHub environment: `pypi`

For each new release:

1. Update the version in `pyproject.toml`. Use a version that PyPI does not already have.
2. Merge the intended code and tests into `main`.
3. Run the [Python distribution and PyPI](https://github.com/1337isnot1337/DOPPLERGEIST/actions/workflows/python-package.yml) workflow manually on `main`. A manual run builds packages but does **not** publish them.
4. After the checks pass, create a [GitHub release](https://github.com/1337isnot1337/DOPPLERGEIST/releases) with the matching `v<project.version>` tag.
5. Make sure that GitHub Actions succeeds. Then verify the new version on [PyPI](https://pypi.org/project/dopplergeist/) and install it in a clean environment.

The workflow builds 19 CPython 3.11–3.14 wheels for supported Linux, macOS, and Windows platforms, plus a source archive. Intel macOS wheels cover Python 3.11–3.13. It validates the package names and version before upload.

PyPI does not allow replacement of an uploaded distribution file. If a release is already on PyPI, use a new version and tag. If publication stops before any upload, fix the cause and rerun the failed workflow.

## Packaging notes

- `ffmpeg` and `ffprobe` are system dependencies. The PyPI wheel does not contain them.
- Optional native C accelerators use platform-specific wheels. The source archive supports builds with a suitable compiler.
- Distribution files include the browser application's assets and the PolyForm Noncommercial and MIT-legacy notices.
- PyPI publication does not grant commercial-use rights. See [commercial licensing](COMMERCIAL-LICENSING.md).
