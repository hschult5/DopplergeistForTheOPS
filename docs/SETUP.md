# Installation and reproducibility

## Requirements

- Python 3.11 or newer. Intel Macs require Python 3.11–3.13. Fresh Linux environments were checked with Python 3.12.15
  and 3.14.8 for this checkpoint; CI also exercises the declared Python 3.11 minimum.
- `ffmpeg` and `ffprobe` on PATH. The MP3 smoke tests use ffmpeg's `libmp3lame`
  encoder. Install the normal ffmpeg package for your operating system.
- GitHub CLI (`gh`) with access to this repository when
  downloading the checkpoint assets. Local/offline runs do not require GitHub access.

## Install the CLI without a checkout

After the initial PyPI release has been published, the shortest supported installation is:

```bash
pipx install dopplergeist
dopplergeist --version
dopplergeist --help
```

Or install the Python distribution into an existing environment with `python -m pip install dopplergeist`. The [PyPI publication guide](PYPI.md) describes the first-release setup and status. FFmpeg and ffprobe must be installed separately.

Until that first PyPI upload, or to install the latest source commit directly, use `pipx` with the GitHub repository:

```bash
pipx install 'git+https://github.com/1337isnot1337/DOPPLERGEIST.git'
dopplergeist --version
dopplergeist --help
```

Install `pipx` and Git using your operating system's package manager first.
The audio commands also require `ffmpeg` and `ffprobe` on `PATH`. This installs
the console command without cloning the project into your working directory.
The local upload workflow and its recording-time/data requirements are described
in [PRODUCT.md](PRODUCT.md). For development, pinned environments, and checkpoint
reproduction, use the checkout-based instructions below.

Nix users can enter the development shell first with `nix develop`. It supplies
Python 3.12, ffmpeg, GitHub CLI, and native build libraries; install the Python
dependencies into a virtual environment using the commands below.

Create an isolated environment from the repository root:

```bash
python -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/dopplergeist --version
.venv/bin/dopplergeist --help
```

Do not use `--system-site-packages` for a clean-install check. Production
dependencies are declared in `pyproject.toml`; browser assets are included in the
package. The historical pytest suite generates synthetic inputs and should not be
run under the current real-world-only policy in [AGENTS.md](../AGENTS.md#real-world-data-only).

## Pinned Python 3.14 reproduction

`docs/reproducibility/py314-linux.lock` records all installed runtime/test dependency
versions from the fresh Linux Python 3.14 environment. It is a platform/version
snapshot, not a claim that those same package versions support Python 3.11.

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r docs/reproducibility/py314-linux.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/dopplergeist --help
```

The original experiment environment is separately recorded in
`evidence/runtime.json`. A fresh resolver selected pandas 3.1.0 rather than the
original 2.3.3. Its default datetime resolution is microseconds, so dividing raw
datetime integers by 1e9 produces incorrect seconds. The feature benchmark now
uses the existing explicit `unix_time` conversion instead of assuming a unit.

## Restore only needed artifacts

The restoration utility uses the committed manifest and validates bundle and
individual-file hashes. Existing identical files are reused; conflicting local
files are preserved and cause a clear error. A new checkout is the simplest
destination. The source YouTube MP4 is excluded.

```bash
python scripts/restore_artifacts.py --list
```

Current evaluation uses real recordings. For the historical regional real
reference replay (which is not independently timed field validation):

```bash
python scripts/restore_artifacts.py --bundle data-source --bundle data-adsb-derived --bundle data-terrain
.venv/bin/dopplergeist run data/source/reproduce-local.json --output results/new-reference-replay
```

Use `--cache PATH` to reuse a downloaded asset cache, and `--destination PATH` to
restore into another clean checkout. `--all` downloads/restores the full checkpoint,
including the optional raw ADS-B archive parts. The ordinary regional replay does
not require those raw parts. See [ARTIFACTS.md](ARTIFACTS.md) for provenance and
manual restoration details.

## Checks and evidence

CI installs into clean Python 3.11 and 3.14 runners, compiles the source, checks
CLI startup and verifies packaged browser assets. It does not run the historical
synthetic-generating pytest suite or establish a national benchmark.

The clean-install replay receipt and complete short synthetic replay artifacts
are in [evidence/packaging-20261102](../evidence/packaging-20261102). The restored
known-time minute again recovered `true-A` with 40.8744 m error. This is a selected
synthetic event replay, not automatic or independent real localization evidence.
The source data were fetched through the same authenticated release-download path
documented above and checked against the manifest.
