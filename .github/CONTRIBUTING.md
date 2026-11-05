# Contributing to DOPPLERGEIST

Thank you for helping improve DOPPLERGEIST. Contributions to code, documentation,
real-recording evaluation, and reproducibility are welcome. Please follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Before starting

- Search the [issues](https://github.com/1337isnot1337/DOPPLERGEIST/issues) and
  [discussions](https://github.com/1337isnot1337/DOPPLERGEIST/discussions) for
  related work. Open an issue to discuss substantial changes or a new approach.
- Read the [README](../README.md) for current capabilities and limitations. For
  data and research changes, see [data sources](../docs/DATA_SOURCES.md),
  [data and search](../docs/DATA_AND_SCALE.md), and the relevant documentation.
- Project agents should also follow [AGENTS.md](../AGENTS.md), including its
  coordination and real-world-data rules.

## Development

DOPPLERGEIST requires Python 3.11 or newer and FFmpeg. From a checkout:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/dopplergeist --help
```

Make a focused change and run checks relevant to it. The standard CI workflow
checks installation, compilation, CLI help, packaged assets and selected
real-input behavior; see [.github/workflows/tests.yml](workflows/tests.yml).
Do not run the entire pytest suite indiscriminately: it includes synthetic-data
tests. The project currently requires real recordings and real-world observations
for empirical evaluation and prohibits new synthetic-data suites or generated
benchmark inputs. Existing unit tests are software checks, not evidence of
real-world localization accuracy.

For empirical work, identify the recording and observation sources, permissions,
clock and location evidence, settings, results, and relevant uncertainty. Report
all outcomes, including failures and inconclusive results. Distinguish regional,
selected, or assisted experiments from independent end-to-end validation; do not
use evaluation truth as an input to an automatic search. Do not publish recordings,
coordinates, credentials, or other sensitive material without permission. Follow
[artifact guidance](../docs/ARTIFACTS.md) and source-specific terms.

## Pull requests

Open a pull request describing the problem, the change, and the checks performed;
link the relevant issue when there is one. Explain limitations and any changes to
interfaces, data, or dependencies. Keep unrelated work separate and respond to
review feedback. Contributions are subject to the repository's
[license](../LICENSE); the [legacy notice](../licenses/MIT-legacy.txt) preserves
previously granted MIT rights, and third-party materials may have separate terms.

For vulnerabilities, use the private process in [SECURITY.md](SECURITY.md), not
a public issue. General questions can go in discussions.
