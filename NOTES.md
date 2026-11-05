# DOPPLERGEIST code notes

Keep essential code answers from our discussions in this file. Append new topics
and update existing explanations when the implementation changes.

## Flight data and ADSB.lol API (2026-11-04)

### Current historical-data source

DOPPLERGEIST downloads daily historical archives from the appropriate yearly
ADSB.lol repository, for example:

- Repository: https://github.com/adsblol/globe_history_2026
- Downloads: https://github.com/adsblol/globe_history_2026/releases
- Implementation: [`FlightCache.acquire_day()`](src/dopplergeist/data_access.py),
  around line 904 at the revision inspected for this discussion.

The release-metadata request, around line 933, is:

```python
response = self.session.get(
    f"https://api.github.com/repos/adsblol/globe_history_{day.year}/releases/tags/{tag}",
    timeout=60,
)
```

The default tag is `vYYYY.MM.DD-planes-readsb-staging-0`. Release metadata
provides the archive download links. The program builds a local flight index
and reuses it for later jobs. `--online` permits acquisition; offline jobs need
cached data or supplied indexes. Initial acquisition can be large and slow.

Daily publication does not mean one position per day: the archives contain
trajectories with multiple timestamped observations throughout the day.

### Calling the live API

Use `GET /v2/point/{lat}/{lon}/{radius}` to collect aircraft around a region.
The radius is in nautical miles, with a documented maximum of 250 nm.

`/v2/ladd` selects only aircraft on the **Limiting Aircraft Data Displayed**
list. It does not return all aircraft and would omit most potential flyovers.
`/v2/hex/{icao_hex}` is useful when inspecting an already-known aircraft;
automatic discovery must not depend on knowing the correct aircraft beforehand.

Example PowerShell request for aircraft within 100 nautical miles of Berlin:

```powershell
$data = Invoke-RestMethod "https://api.adsb.lol/v2/point/52.52/13.405/100"
$data.ac | Select-Object hex, flight, lat, lon, alt_geom, alt_baro, gs, track, seen_pos
```

This is a live snapshot, not a request for an arbitrary historical time.

Official references:

- Endpoints: https://api.adsb.lol/docs
- Public API access: https://www.adsb.lol/docs/open-data/api
- Dynamic rate limits: https://github.com/adsblol/api#rate-limits
- Feeder-only whole-network access: https://www.adsb.lol/docs/feeders-only/re-api

### Connecting live updates to DOPPLERGEIST

This is an integration approach, not a feature implemented by the example above:

1. Collect snapshots periodically, within provider limits. Back off on HTTP 429
   and follow `Retry-After` when supplied. Do not assume a guaranteed polling rate.
2. Save the complete responses and collection UTC; group observations by aircraft
   `hex` identifier to accumulate trajectories. Preserve observation age/time
   information so stale positions are not assigned fresh timestamps.
3. Add a format adapter to normalize live fields into the existing flight index.
   Preserve units, altitude datums, missing values and trajectory gaps.
4. Use the daily archives for older recordings or to backfill missing periods.

Changing only the URL in `acquire_day()` will not work: that method consumes
daily archives, while the live API returns snapshot JSON. A live collector cannot
recover positions from before collection began.

For Doppler fitting, aim to preserve observations every few seconds where the
source and access limits allow; one sample per minute can miss useful flyover
motion. This is an engineering recommendation, not a validated accuracy guarantee.

One point query covers a region. Use it when independent context supports that
regional search area; do not center collection on hidden true recorder coordinates.
Nationwide unknown-location search needs wider collection coverage. The feeder-only
re-api documents whole-world `?all` or `?all_with_pos` snapshots twice per minute;
access is restricted to the contributing station's IP address.

A defensible recording UTC window is still required. Upload time and file
modification time do not establish when the audio was recorded.

### Other sources discussed

- [OpenSky REST API](https://openskynetwork.github.io/opensky-api/rest.html):
  anonymous latest states at 10-second resolution; authenticated states at
  5-second resolution with up to one hour of history, subject to quotas.
- [ADS-B Exchange](https://www.adsbexchange.com/data-products/): commercial live
  API/streaming products advertise updates as often as every 250 ms.

Provider access policies and quotas can change; check the linked documentation
before implementing a collector.

## Audio input (2026-11-04)

The automatic workflow takes a recorded file, such as an MP3, through the CLI
or browser upload. These entry points process a file rather than a live microphone
stream.

```powershell
dopplergeist analyze "C:\recordings\flyover.mp3" --jobs jobs
```

For browser upload, start the local application and open http://127.0.0.1:8765:

```powershell
dopplergeist serve --jobs jobs --port 8765
```

Both commands assume the installed `dopplergeist` executable is on PATH.
FFmpeg and ffprobe must also be available on PATH.

### Internal path

`CLI / upload -> automatic.analyze() -> intake_recording() -> discover_events()`

- [`automatic.py`](src/dopplergeist/automatic.py), around lines 129-134, passes
  the request's `audio_path` into intake and then runs automatic event discovery.
- [`intake.py`](src/dopplergeist/intake.py): `probe_media()` (around line 33)
  inspects the actual file using ffprobe. `intake_recording()` (around line 97)
  decodes the complete first audio stream with FFmpeg. It averages input channels
  into mono and resamples to **4,000 Hz** by default, emitting **32-bit
  little-endian floating-point PCM**. Users do not need to preconvert to 4 kHz.
- Decoded samples are stored in `intake/recording.f32`, with recording metadata
  in `intake/recording.json` and decode diagnostics in `intake/intake.json` inside
  the job output. Disk-backed chunk reads avoid keeping the complete decoded
  recording in RAM.
- [`discovery.py`](src/dopplergeist/discovery.py): `discover_events()` scans the
  recording in overlapping chunks for moving frequency tracks and groups related
  harmonics into flyover proposals. The automatic workflow does not require
  manually selected event windows or frequency bands.
- [`audio.py`](src/dopplergeist/audio.py) also contains `decode()` and spectral
  measurement helpers used by the configured research workflow. Whole-recording
  automatic intake starts in `intake.py`.

The default 4 kHz representation can contain frequencies below the 2 kHz Nyquist
limit; it does not preserve the recording's full high-frequency content.

### Upload API

[`service.py`](src/dopplergeist/service.py) accepts `POST /api/jobs` with raw file
bytes as `Content-Type: application/octet-stream`, an explicit `Content-Length`,
an optional `X-Filename`, and JSON request metadata in `X-Dopplergeist-Request`.
This route does not accept multipart form data. The service's default upload
limit is 100 MiB, configurable with `serve --max-upload-mb`.

### Recording time

The first decoded sample is audio-relative time zero. To match historical
flights, supply a defensible UTC window for that first sample using `--start-utc`,
`--end-utc`, and `--time-source`, or the corresponding browser fields. Without
this evidence, acoustic discovery can run, but the automatic job returns
`needs_input / recording_time_required` before flight search. File modification
time, upload time and generic creation tags are not accepted recording clocks.

## Jupyter: live aircraft around a point

Run this cell to fetch one live snapshot. Change latitude, longitude and radius
as needed; the radius is in nautical miles (maximum 250).

```python
import requests

lat, lon, radius_nm = 52.52, 13.405, 100  # Berlin
response = requests.get(
    f"https://api.adsb.lol/v2/point/{lat}/{lon}/{radius_nm}", timeout=30
)
response.raise_for_status()
aircraft = response.json().get("ac", [])
aircraft[:5]  # Display the first five aircraft in Jupyter
```

This requests current positions only and does not start continuous collection.

### Where `requests` comes from

`requests` is a third-party Python library for HTTP requests. It is not part of
Python's standard library. `import requests` loads it from the notebook kernel's
Python environment; `requests.get(...)` sends an HTTP GET request to the API.

If the import raises `ModuleNotFoundError`, install it in a separate Jupyter cell:

```python
%pip install requests
```

`%pip` installs into the current notebook kernel's environment. Then rerun the
API cell (restart the kernel if needed).
