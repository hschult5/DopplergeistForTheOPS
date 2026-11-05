# DOPPLERGEIST

![DOPPLERGEIST — aircraft Doppler audio geolocation](https://raw.githubusercontent.com/1337isnot1337/DOPPLERGEIST/main/docs/assets/dopplergeist-banner.svg)

**DOPPLERGEIST** is a Python CLI and local web application for aircraft Doppler audio geolocation. It detects candidate flyover signals in recordings, retrieves historical ADS-B flight tracks, models acoustic propagation, and ranks possible recording locations.

![Automatic MP3 flyover analysis and ranked aircraft search](https://raw.githubusercontent.com/1337isnot1337/DOPPLERGEIST/main/docs/assets/livermore-automatic-mp3.png)

The illustrated real-recording experiment ranked a location 476 m from an *inferred* reference within a constrained search. The reference was not surveyed ground truth; independent blind geolocation accuracy has not been established. [Read the experiment](https://github.com/1337isnot1337/DOPPLERGEIST/blob/main/docs/LIVERMORE_DEMO.md).

## Install

Requires **Python 3.11+**. Intel Macs require Python 3.11–3.13; Python 3.14 on Intel macOS is not supported. Install **FFmpeg** separately so that both `ffmpeg` and `ffprobe` are on your `PATH`.

For the CLI:

```sh
pipx install dopplergeist
dopplergeist --help
```

For a Python environment:

```sh
python -m pip install dopplergeist
```

## Use

Detect candidate acoustic events without flight-data downloads:

```sh
dopplergeist discover recording.mp3 --output results/discovery
```

Launch the locally hosted upload interface:

```sh
dopplergeist serve --jobs jobs --online --port 8765
```

Open `http://127.0.0.1:8765`. For historical flight matching, a **defensible UTC window for the first audio sample** is required; the recording upload timestamp is not sufficient. Geographic searches can consume substantial time, storage, and downloaded aircraft/terrain data.

For CLI analysis with an independently supported recording clock:

```sh
dopplergeist analyze recording.mp3 --jobs jobs \
  --start-utc "2026-01-01T00:00:00Z" \
  --end-utc "2026-01-01T00:10:00Z" \
  --time-source "replace with independently supported clock evidence" \
  --online
```

## Documentation

- [Source code and full README](https://github.com/1337isnot1337/DOPPLERGEIST)
- [Installation and reproducibility](https://github.com/1337isnot1337/DOPPLERGEIST/blob/main/docs/SETUP.md)
- [How the geolocation pipeline works](https://github.com/1337isnot1337/DOPPLERGEIST/blob/main/docs/METHODOLOGY.md)
- [Validation and limitations](https://github.com/1337isnot1337/DOPPLERGEIST/blob/main/docs/CONFIDENCE.md)
- [Issues and feature requests](https://github.com/1337isnot1337/DOPPLERGEIST/issues)

## License

**PolyForm Noncommercial License 1.0.0.** Noncommercial uses are permitted under the terms of that license; commercial uses that are not otherwise authorized need a separate agreement with the applicable copyright holders. Earlier MIT-licensed releases retain the permissions originally granted for those versions. [Commercial licensing details](https://github.com/1337isnot1337/DOPPLERGEIST/blob/main/docs/COMMERCIAL-LICENSING.md).
