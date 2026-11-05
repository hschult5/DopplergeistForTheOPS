# Run with Docker

## Local checkout with Docker Compose

The repository's [compose.yaml](../compose.yaml) builds your local checkout and
runs the browser application at **http://127.0.0.1:8765**. It enables public data
downloads and allows four hours per job, matching the local OPS installation.
The default upload limit remains 100 MiB. Run these commands from the repository
root in PowerShell or a shell with Docker Compose available.

If the earlier standalone `dopplergeist-ops` container is running, let any active
analysis finish before stopping it to release the port and job-store lock:

```powershell
docker stop --time 30 dopplergeist-ops
```

The Compose file reuses the external volumes `dopplergeist-ops-jobs` and
`dopplergeist-ops-cache`, preserving existing uploads, results and downloads.
They already exist for the OPS installation. On a fresh installation, create them
first (these commands also safely reuse existing volumes):

```powershell
docker volume create dopplergeist-ops-jobs
docker volume create dopplergeist-ops-cache
docker compose up --build -d
```

For routine operation:

```powershell
docker compose ps
docker compose logs --tail 50 -f
docker compose stop
docker compose start
```

After changing application code, run `docker compose up --build -d` again.
The image contains a build-time snapshot; local edits are not mounted live.
Recreating the service can interrupt an active job, so finish or cancel it first.
`docker compose down` removes the Compose container and network, but retains the
external data volumes. Do not run the old standalone container alongside Compose:
both use the same port and persistent job directory. Keep the old container
stopped; no migration or deletion is required.

## Published image or standalone Docker

Pull the published `main` image from GitHub Container Registry:

```bash
docker pull ghcr.io/1337isnot1337/dopplergeist:main
```

The published image targets `linux/amd64`. Other architectures have not been
validated. To build from a repository checkout instead:

```bash
docker build --tag dopplergeist:local .
```

The multi-stage image builds the four native extensions with GCC. The runtime
contains Python 3.12, FFmpeg, the installed Python dependencies and browser assets.
It runs as UID/GID `10001`, with the repository license and bundled notices
preserved in the installed package. Recordings, jobs, caches and historical
evidence are excluded from the build context.

## Start the application

```bash
docker run --detach --init --name dopplergeist \
  --publish 127.0.0.1:8765:8765 \
  --mount type=volume,src=dopplergeist-jobs,dst=/data/jobs \
  --mount type=volume,src=dopplergeist-cache,dst=/home/dopplergeist/.cache/dopplergeist \
  ghcr.io/1337isnot1337/dopplergeist:main
```

Open `http://127.0.0.1:8765`. Docker creates the named volumes when needed.
For a local build, replace the image name with `dopplergeist:local`.
The jobs volume preserves uploads, results and checkpoints. The cache volume
preserves downloaded flight archives, indexes, terrain and geoid data.
Existing bind-mount directories must be writable by UID/GID `10001`.

The default command is:

```bash
dopplergeist serve --host 0.0.0.0 --port 8765 --jobs /data/jobs
```

The service listens on the container's interfaces; the published host port stays
on loopback. Host networking is unnecessary. The unauthenticated local service's
Host and Origin checks remain enabled. Use the loopback publication shown above.
Normal non-container `serve` still defaults to `127.0.0.1`.

## Enable data downloads or use existing data

The default container has online acquisition disabled. Audio intake and discovery
remain available; historical search needs configured data and a defensible
recording-start window. To permit public downloads, append the following command
after the image name in the `docker run` command above:

```bash
serve --host 0.0.0.0 --port 8765 --jobs /data/jobs --online --max-runtime 14400
```

Downloads can require hours and tens of gigabytes. The default upload limit is
100 MiB; add `--max-upload-mb 250`, for example, when a larger recording is needed.
The [local workflow](PRODUCT.md) explains timing evidence and search limits.

For an offline provider configuration, mount the JSON file read-only and append
`--config /config/operator.json` to the complete `serve` command. For example,
add this mount to `docker run`:

```bash
--mount type=bind,src=/absolute/path/operator.json,dst=/config/operator.json,readonly
```

Configuration paths must refer to paths inside the container. Mount any existing
indexes and terrain at those paths. If the configuration changes `cache_root`,
mount a persistent writable volume at that location as well.

## Stop, restart and change ports

```bash
docker stop --time 30 dopplergeist
docker start dopplergeist
```

The image uses SIGINT for normal shutdown so the service closes its job manager
and stops workers. Completed jobs remain readable after restart. Resume a stopped
job explicitly through the UI when its checkpoint is compatible. Named volumes
survive container removal; reuse the same volume names when replacing the image.
The [backup procedure](PRODUCT.md) requires a stopped, consistent job directory.

Keep host and container port numbers equal because the local Host check uses the
service port. For port `8876`, use `--publish 127.0.0.1:8876:8876` and append the
complete command `serve --host 0.0.0.0 --port 8876 --jobs /data/jobs` to the image
name. A mapping such as `8876:8765` will not pass the existing Host check.

## Image versions and publication

The Docker workflow publishes `ghcr.io/1337isnot1337/dopplergeist` after the image
passes its runtime, HTTP, license and volume checks. A separate publishing job
loads that validated image and checks its image ID before pushing it to GHCR.
The image is transferred within the same workflow run; publication uses that
image without rebuilding it.

| Tag | Source |
| --- | --- |
| `main` | A successful push to `main`, or a manual run on `main` |
| `vMAJOR.MINOR.PATCH` | The matching Git tag, such as `v0.1.0` |
| `vMAJOR.MINOR.PATCH-PRERELEASE` | The matching prerelease tag, such as `v0.2.0-rc.1` |
| `sha-FULL_COMMIT_ID` | The complete source commit ID for every published image |

Immediately before pushing `main`, the workflow reads the current public Git
branch. If that branch has moved since the image was built, the run keeps its
commit tag and skips `main`, with the reason in the workflow summary. An
unreadable branch ref fails publication without updating `main`. This also
applies to reruns of older builds.

Version tags use SemVer release/prerelease syntax without `+build` metadata.
Image labels record the source repository, commit, version and
`PolyForm-Noncommercial-1.0.0` license. A revision tag identifies the source
commit. To retain an exact built image, use the registry digest recorded in the
workflow summary: `ghcr.io/1337isnot1337/dopplergeist@sha256:DIGEST`.

Maintainers can publish `main` through **Actions → Docker build, local service
and GHCR → Run workflow**, selecting `main`. Other manually selected branches
run validation only. Pull requests and forks run without package-write
permission and cannot publish through this workflow. Publication uses the
repository's generated `GITHUB_TOKEN`; no registry password secret is needed.

GitHub makes a newly published GHCR package private by default. After the first
successful publication, a package administrator must set its visibility to
**Public** to enable the anonymous pull command above. The image's source label
links it to this repository. If an existing package rejects `GITHUB_TOKEN`
writes, grant this repository access under the package's **Manage Actions
access** settings. These package settings are separate from workflow token
permissions. See GitHub's [Container registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

## Verification

The Docker workflow builds the image and checks native imports, FFmpeg, GIS/audio
libraries, packaged browser assets and licenses. It checks actual HTTP access
through a bridge-published loopback port, the existing Host/Origin checks, clean
shutdown, and non-root writes that persist across containers in both volumes.
These checks generate no audio, flight tracks or scientific job results.
Publication eligibility and tag rules are also checked with
`python -m unittest discover -s tests -p test_container_publish_metadata.py`.
