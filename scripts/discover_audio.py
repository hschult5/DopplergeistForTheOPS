"""Run blind full-file intake/discovery into a fresh diagnostic directory.

No annotations, coordinates, flight IDs or per-case frequency/time hints are read.
Evaluate its output against withheld labels in a separate process.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

from dopplergeist.intake import intake_recording
from dopplergeist.resource_usage import process_peak_rss_kib


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio")
    parser.add_argument("--output", required=True)
    parser.add_argument("--plot-summary", action="store_true",
                        help="render the recording-level ridge overview plot")
    parser.add_argument("--plot-event-details", action="store_true",
                        help="render one spectrogram PNG per discovered event (slower)")
    args = parser.parse_args()
    from dopplergeist import discovery
    settings = discovery.DiscoverySettings()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    began = time.monotonic()
    recording = intake_recording(args.audio, output / "intake")
    events = discovery.discover_events(recording, output_dir=output / "discovery",
                             plot_summary=args.plot_summary,
                             plot_event_details=args.plot_event_details)
    root = Path(__file__).resolve().parents[1]
    sources = sorted((root / "src" / "dopplergeist").glob("*.py"))
    code_hash = hashlib.sha256()
    for path in sources:
        code_hash.update(path.name.encode())
        code_hash.update(path.read_bytes())
    diagnostics = json.loads((output / "discovery/discovery.json").read_text())
    result = {"recording": recording.metadata, "time_status": recording.time_status,
              "settings": diagnostics["settings"], "sequence_settings": diagnostics["sequence_settings"],
              "events_found": len(events),
              "events": [{k: event[k] for k in ("event_id", "start_s", "end_s", "band_hz", "quality")} for event in events],
              "runtime_s": time.monotonic() - began, "peak_rss_kib": process_peak_rss_kib(),
              "code_sha256": code_hash.hexdigest(),
              "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
              "working_tree": subprocess.check_output(["git", "status", "--short"], cwd=root, text=True).strip(),
              "scope": "Blind acoustic discovery only; no aircraft association or location estimate"}
    (output / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
