#!/usr/bin/env python3
"""Run fresh HTTP jobs for a predeclared real-recording representation matrix.

Prepare freezes configuration, inputs and implementation before any arm runs.
Run preserves failed, inconclusive and unusable arms. Report only compares them.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from importlib import metadata
from importlib.machinery import EXTENSION_SUFFIXES
import inspect
import itertools
import json
import math
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading

import numpy as np
from pyproj import Geod
from scipy import signal as scipy_signal
from scipy.optimize import linear_sum_assignment

import dopplergeist
from dopplergeist.contracts import validate_request
from dopplergeist.discovery import DiscoverySettings
from dopplergeist.intake import IntakeRecording, intake_recording, probe_media
import verify_upload_http as http_verifier


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "docs/verification/metamorphic_protocol_v1.json"
GEOD = Geod(ellps="WGS84")


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.is_file() else default


def write_json(path, value):
    """Atomic progress reports; immutable plans use exclusive creation."""
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def identity(path):
    path = Path(path).absolute()
    resolved = path.resolve(strict=True)
    return {"path": str(path), "resolved_path": str(resolved), "bytes": path.stat().st_size,
            "sha256": http_verifier.sha256(path)}


def parse_utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("UTC endpoints must include a timezone")
    return parsed.astimezone(timezone.utc)


def format_utc(value):
    return value.isoformat().replace("+00:00", "Z")


def shifted_request(request, silence_s):
    result = deepcopy(request)
    window = result.get("time_window")
    if not isinstance(window, dict):
        raise ValueError("The matrix needs a defensible sample-zero UTC window")
    for key in ("start_utc", "end_utc"):
        window[key] = format_utc(parse_utc(window[key]) - timedelta(seconds=silence_s))
    return result


def implementation():
    from dopplergeist import data_access, matching, native_acoustic, native_cpa

    package = Path(dopplergeist.__file__).resolve().parent
    result = {
        "package_files": {p.relative_to(package).as_posix(): http_verifier.sha256(p)
            for p in sorted(package.rglob("*")) if p.is_file() and (
                p.suffix in {".py", ".c"} or any(p.name.endswith(suffix) for suffix in EXTENSION_SUFFIXES))},
        "runner_sha256": http_verifier.sha256(__file__),
        "verifier_sha256": http_verifier.sha256(http_verifier.__file__),
        "python": sys.version, "executable": sys.executable,
        "packages": {name: metadata.version(name) for name in
                     ("numpy", "scipy", "pandas", "pyproj", "rasterio", "pyarrow", "matplotlib")},
        "backends": {"ridge": matching.RIDGE_FIT_BACKEND, "acoustic": native_acoustic.ACOUSTIC_BACKEND,
                     "cpa": native_cpa.CPA_BACKEND,
                     "index_payload": "native" if data_access._native_payload_columns is not None else "python"},
        "environment": {name: value for name, value in sorted(os.environ.items())
                        if name in {"DOPPLERGEIST_CPA_BACKEND", "DOPPLERGEIST_ACOUSTIC_BACKEND",
                            "DOPPLERGEIST_RIDGE_BACKEND",
                            "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                            "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "PYTHONHASHSEED"}},
        "missing_runtime_pins": [],
    }
    for name in ("ffmpeg", "ffprobe"):
        result[name] = subprocess.run([name, "-version"], check=True,
            capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
    try:
        result["packages"]["shapely"] = metadata.version("shapely")
    except metadata.PackageNotFoundError:
        result["packages"]["shapely"] = None
        result["geos_version"] = None
    else:
        import shapely
        result["geos_version"] = getattr(shapely, "geos_version_string", None)
        if not isinstance(result["geos_version"], str) or not result["geos_version"]:
            result["missing_runtime_pins"].append("geos_version")
    return result


def operator_inputs(operator, additional):
    """Pin local data named by the operator, without following active job state."""
    if operator.get("offline", True) is not True:
        raise ValueError("Use an offline operator with frozen historical data and terrain")
    paths = set()
    for value in operator.get("flight_indexes", []):
        paths.add(Path(value).resolve(strict=True))
    ground = operator.get("terrain", {})
    for value in ground.get("rasters", []):
        paths.add(Path(value).resolve(strict=True))
    if ground.get("geoid_raster"):
        paths.add(Path(ground["geoid_raster"]).resolve(strict=True))
    if operator.get("cache_root"):
        cache = Path(operator["cache_root"]).resolve(strict=True)
        paths.update(p.absolute() for p in cache.rglob("*") if p.is_file())
    paths.update(Path(p).resolve(strict=True) for p in additional)
    if not paths:
        raise ValueError("No immutable historical/terrain inputs were supplied")
    return [identity(path) for path in sorted(paths)]


def prepare(audio, request, operator, output, *, source_note, scope_note,
            additional_inputs=(), job_max_runtime_s=None, http_timeout_s=None):
    audio = Path(audio).resolve(strict=True)
    operator = deepcopy(operator)
    for name in ("flight_indexes",):
        if name in operator:
            operator[name] = [str(Path(p).resolve(strict=True)) for p in operator[name]]
    ground = operator.get("terrain", {})
    if "rasters" in ground:
        ground["rasters"] = [str(Path(p).resolve(strict=True)) for p in ground["rasters"]]
    if ground.get("geoid_raster"):
        ground["geoid_raster"] = str(Path(ground["geoid_raster"]).resolve(strict=True))
    if operator.get("cache_root"):
        operator["cache_root"] = str(Path(operator["cache_root"]).resolve(strict=True))
    if set(operator.get("discovery", {})) - {"chunk_s", "overlap_s"}:
        raise ValueError("The upload operator exposes only discovery chunk_s and overlap_s")
    protocol = read_json(PROTOCOL)
    for key, override in (("job_max_runtime_s", job_max_runtime_s),
                          ("http_timeout_s", http_timeout_s)):
        if override is not None:
            if not math.isfinite(override) or override <= 0:
                raise ValueError(f"{key} must be finite and positive")
            protocol["budgets"][key] = float(override)
    if protocol["budgets"]["job_max_runtime_s"] >= protocol["budgets"]["http_timeout_s"]:
        raise ValueError("The client deadline must exceed the job runtime limit")
    if not source_note.strip() or not scope_note.strip():
        raise ValueError("Declare the real source and the evaluation scope")
    request = dict(validate_request(dict(request, audio_path=str(audio))))
    request.pop("audio_path")
    shifted_request(request, 0)
    DiscoverySettings(**operator.get("discovery", {}))
    inputs = operator_inputs(operator, additional_inputs)
    cases = []
    for case in protocol["cases"]:
        settings = dict(protocol["baseline"], **case["changes"])
        arm_operator = deepcopy(operator)
        if "discovery" in settings:
            arm_operator["discovery"] = settings.pop("discovery")
        DiscoverySettings(**arm_operator.get("discovery", {}))
        cases.append({"id": case["id"], "encoding": settings,
                      "request": shifted_request(request, settings["leading_silence_s"]),
                      "operator": arm_operator})
    pinned_implementation = implementation()
    if pinned_implementation["missing_runtime_pins"]:
        raise ValueError("Cannot freeze missing runtime pins: "
                         + ", ".join(pinned_implementation["missing_runtime_pins"]))
    plan = {"schema_version": 1, "prepared_utc": http_verifier.now(),
        "protocol": protocol, "protocol_sha256": http_verifier.sha256(PROTOCOL),
        "source": identity(audio), "source_note": source_note, "scope_note": scope_note,
        "request": request, "operator": operator, "inputs": inputs, "cases": cases,
        "additional_inputs": [str(Path(p).resolve(strict=True)) for p in additional_inputs],
        "implementation": pinned_implementation,
        "source_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
            check=True, capture_output=True, text=True).stdout.strip(),
        "truth_evaluation": {"eligible": False, "evaluated": False,
                             "reason": "No independent truth supplied to this protocol"},
        "national_gate_satisfied": False}
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    with (output / "plan.json").open("x") as stream:
        json.dump(plan, stream, indent=2, allow_nan=False)
        stream.write("\n")
    (output / "plan.sha256").write_text(http_verifier.sha256(output / "plan.json") + "\n")
    return output / "plan.json"


def check_frozen(plan_path, plan):
    expected = plan_path.with_name("plan.sha256").read_text().strip()
    if http_verifier.sha256(plan_path) != expected:
        raise ValueError("The frozen plan changed; prepare a new experiment")
    if implementation() != plan["implementation"]:
        raise ValueError("Implementation, runtime or backend configuration changed after preparation")
    if identity(plan["source"]["path"]) != plan["source"]:
        raise ValueError("The recorded source changed")
    if operator_inputs(plan["operator"], plan["additional_inputs"]) != plan["inputs"]:
        raise ValueError("Pinned data bytes, paths or cache file set changed")


def run_command(command, directory, name, timeout_s, *, env=None):
    write_json(directory / f"{name}-command.json", command)
    with (directory / f"{name}.log").open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
        try:
            returncode = process.wait(timeout=timeout_s)
            if returncode:
                raise subprocess.CalledProcessError(returncode, command)
        except BaseException:
            # Intake starts FFmpeg. Stop this command's whole process group.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise


def preflight_decode(audio, directory, protocol):
    """Use actual intake in a bounded child; HTTP intake is still fresh."""
    command = [sys.executable, "-c",
        "import sys; from dopplergeist.intake import intake_recording; "
        "intake_recording(sys.argv[1], sys.argv[2], sample_rate_hz=int(sys.argv[3]))",
        str(audio), str(directory), str(protocol["pcm_comparison"]["sample_rate_hz"])]
    env = dict(os.environ, PYTHONPATH=str(Path(dopplergeist.__file__).resolve().parent.parent)
               + os.pathsep + os.environ.get("PYTHONPATH", ""))
    run_command(command, directory.parent, directory.name + "-decode",
                protocol["budgets"]["encoding_timeout_s"], env=env)
    return IntakeRecording(directory / "recording.f32", read_json(directory / "recording.json"),
                           read_json(directory / "intake.json"))


def make_canonical(source, directory, protocol):
    probe = probe_media(source)
    audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    channels = int(audio["channels"])
    downmix = "pan=mono|c0=" + "+".join(f"{1/channels:.17g}*c{i}" for i in range(channels))
    path = directory / "canonical.wav"
    command = ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(source),
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-af", downmix, "-ac", "1",
        "-ar", str(protocol["canonical_pcm"]["sample_rate_hz"]),
        "-c:a", "pcm_f32le", "-map_metadata", "-1", "-rf64", "auto", str(path)]
    run_command(command, directory, "canonical", protocol["budgets"]["encoding_timeout_s"])
    reference = preflight_decode(path, directory / "reference-pcm", protocol)
    return path, reference


def encode(canonical, directory, settings, protocol):
    filters = []
    if settings["gain_db"]:
        filters.append(f"volume={10**(settings['gain_db']/20):.17g}:precision=double")
    if settings["leading_silence_s"]:
        samples = round(settings["leading_silence_s"] * protocol["canonical_pcm"]["sample_rate_hz"])
        filters.append(f"adelay={samples}S:all=1")
    if settings["channels"] == 2:
        filters.append("pan=stereo|c0=c0|c1=c0")
    path = directory / "input.mp3"
    command = ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(canonical),
               "-map", "0:a:0", "-vn", "-sn", "-dn"]
    if filters:
        command += ["-af", ",".join(filters)]
    command += ["-ac", str(settings["channels"]), "-ar", str(settings["sample_rate_hz"]),
                "-c:a", "libmp3lame", "-b:a", f"{settings['bitrate_kbps']}k",
                "-write_xing", "1", "-map_metadata", "-1", str(path)]
    run_command(command, directory, "encode", protocol["budgets"]["encoding_timeout_s"])
    return path


def native_decode_metrics(audio, directory, protocol):
    """Scan blocks of a temporary native-rate decode; keep no second large PCM."""
    probe = probe_media(audio)
    stream = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    channels = int(stream["channels"])
    command = ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(audio),
               "-map", "0:a:0", "-f", "f32le", "-c:a", "pcm_f32le", "pipe:1"]
    peak, frames, channel_difference = 0.0, 0, 0.0
    with tempfile.TemporaryFile(dir=directory) as raw, (directory / "native-decode.log").open("wb") as log:
        subprocess.run(command, stdout=raw, stderr=log, check=True,
                       timeout=protocol["budgets"]["encoding_timeout_s"])
        if raw.tell() % (4 * channels):
            raise ValueError("Native decode has an incomplete audio frame")
        raw.seek(0)
        while len(block := np.fromfile(raw, dtype="<f4", count=65536 * channels)):
            if not np.isfinite(block).all():
                raise ValueError("Native decode contains nonfinite samples")
            values = block.reshape(-1, channels)
            peak = max(peak, float(np.max(np.abs(values))))
            if channels == 2:
                channel_difference = max(channel_difference, float(np.max(np.abs(values[:, 0] - values[:, 1]))))
            frames += len(values)
    packets = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
        "-read_intervals", "%+#1", "-show_packets", "-of", "json", str(audio)],
        check=True, capture_output=True, text=True, timeout=10)
    return {"sample_rate_hz": int(stream["sample_rate"]), "channels": channels,
        "sample_count": frames, "duration_s": frames / int(stream["sample_rate"]),
        "absolute_peak": peak, "identical_channel_max_difference": channel_difference if channels == 2 else None,
        "first_packet": json.loads(packets.stdout).get("packets", [])[:1],
        "container_start_time": probe["format"].get("start_time"),
        "container_duration": probe["format"].get("duration"), "decode_command": command}


def compare_pcm(reference_path, variant_path, settings, protocol):
    config, tol = protocol["pcm_comparison"], protocol["tolerances"]
    sr = config["sample_rate_hz"]
    left = np.memmap(reference_path, dtype="<f4", mode="r")
    right = np.memmap(variant_path, dtype="<f4", mode="r")
    padding = round(settings["leading_silence_s"] * sr)
    gain = 10 ** (settings["gain_db"] / 20)
    count = min(len(left), max(0, len(right) - padding))
    if count == 0:
        raise ValueError("No recorded samples remain after the declared padding")
    squared_error, energy = 0.0, 0.0
    for first in range(0, count, config["block_samples"]):
        last = min(count, first + config["block_samples"])
        a = np.asarray(left[first:last], dtype=np.float64)
        b = np.asarray(right[first+padding:last+padding], dtype=np.float64) / gain
        squared_error += float(np.dot(a-b, a-b))
        energy += float(np.dot(a, a))
    duration_error = (len(right) - padding - len(left)) / sr
    nrmse = math.sqrt(squared_error / energy) if energy > 0 else None
    radius = round(config["lag_search_radius_s"] * sr)
    width = min(round(config["lag_window_s"] * sr), count - 2*radius)
    lags = []
    if width > 0:
        for first in np.linspace(radius, count-radius-width, config["lag_windows"]).astype(int):
            a = np.asarray(left[first:first+width], dtype=np.float64)
            a = a - a.mean()
            if float(np.dot(a, a)) <= 1e-14:
                lags.append({"start_s": int(first)/sr, "lag_s": None, "reason": "no_alignment_energy"})
                continue
            b = np.asarray(right[first+padding-radius:first+padding+width+radius], dtype=np.float64) / gain
            values = scipy_signal.correlate(b, a, mode="valid", method="fft")
            sums, squares = np.r_[0.0, np.cumsum(b)], np.r_[0.0, np.cumsum(b*b)]
            variance = squares[width:] - squares[:-width] - (sums[width:] - sums[:-width])**2/width
            normalized = values / np.sqrt(np.maximum(variance, np.finfo(float).tiny))
            lag = (int(np.argmax(normalized)) - radius) / sr
            lags.append({"start_s": int(first)/sr, "lag_s": lag})
    measured = [abs(row["lag_s"]) for row in lags if row["lag_s"] is not None]
    max_lag = max(measured) if measured else None
    return {"reference_samples": len(left), "variant_samples": len(right),
        "compared_samples": count, "padding_samples": padding, "declared_gain": gain,
        "duration_error_s": duration_error, "gain_corrected_nrmse": nrmse,
        "fixed_window_lags": lags, "maximum_absolute_lag_s": max_lag,
        "passed": (abs(duration_error) <= tol["decoded_duration_error_s"]
                   and nrmse is not None and nrmse <= tol["gain_corrected_pcm_nrmse"]
                   and max_lag is not None and max_lag <= tol["residual_alignment_lag_s"])}


class MemorySampler:
    """Sample only this runner's descendants, never another worker's jobs."""
    STOP_TIMEOUT_S = 2.0

    def __init__(self):
        self.stop = threading.Event()
        self.peak = 0
        self.samples = 0
        self.thread_error = None
        self.stop_timed_out = False
        self.skipped_process_reads = 0
        try:
            # /proc can be mounted from an outer PID namespace. os.getpid()
            # then names a different entry in that proc mount.
            self.proc_root_pid = int(Path("/proc/self").resolve(strict=True).name)
        except (OSError, RuntimeError, ValueError):
            self.proc_root_pid = None
        self.traversal = ("task_children" if self.proc_root_pid is not None
            and any(Path(f"/proc/{self.proc_root_pid}/task").glob("*/children"))
            else "ppid_snapshot")
        self.thread = threading.Thread(target=self.sample_guarded, daemon=True)

    def sample_guarded(self):
        try:
            self.sample()
        except BaseException as exc:
            # Instrumentation must preserve an HTTP result even if the sampler
            # fails after observing a positive, incomplete peak.
            self.thread_error = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def process_status(pid, *, include_rss):
        parent, rss = None, None
        with Path(f"/proc/{pid}/status").open() as status:
            for line in status:
                if line.startswith("PPid:"):
                    parent = int(line.split()[1])
                    if not include_rss:
                        break
                elif include_rss and line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                if parent is not None and (rss is not None or not include_rss):
                    break
        return parent, rss

    def parent_snapshot(self):
        # Some proc mounts omit task/*/children. Retain only parent relations
        # during this scan; read RSS only for the selected descendant tree.
        children = {}
        for entry in Path("/proc").iterdir():
            if self.stop.is_set():
                break
            if not entry.name.isdecimal():
                continue
            try:
                parent, _ = self.process_status(int(entry.name), include_rss=False)
                if parent is not None:
                    children.setdefault(parent, []).append(int(entry.name))
            except (OSError, ValueError):
                self.skipped_process_reads += 1
        return children

    def sample(self):
        if self.proc_root_pid is None:
            return
        while not self.stop.is_set():
            snapshot = self.parent_snapshot() if self.traversal == "ppid_snapshot" else None
            pending, visited, total = [(self.proc_root_pid, None)], set(), 0
            while pending and not self.stop.is_set():
                pid, expected_parent = pending.pop()
                if pid in visited:
                    continue
                visited.add(pid)
                try:
                    if pid != self.proc_root_pid:
                        parent, rss = self.process_status(pid, include_rss=True)
                        # A process can disappear, be reparented, or have its PID
                        # reused after the snapshot. Exclude a changed relation.
                        if parent != expected_parent:
                            self.skipped_process_reads += 1
                            continue
                        total += rss or 0
                    if snapshot is not None:
                        pending += [(child, pid) for child in snapshot.get(pid, [])]
                    else:
                        # The service can spawn a worker from any of its threads.
                        for children in Path(f"/proc/{pid}/task").glob("*/children"):
                            if self.stop.is_set():
                                break
                            pending += [(int(x), pid) for x in children.read_text().split()]
                except (OSError, ValueError):
                    self.skipped_process_reads += 1
            self.peak = max(self.peak, total)
            self.samples += 1
            self.stop.wait(0.1)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=self.STOP_TIMEOUT_S)
        self.stop_timed_out = self.thread.is_alive()

    def result(self):
        return {"sampled_descendant_peak_rss_mib": self.peak/(1024**2) if self.peak else None,
                "interval_s": 0.1, "samples": self.samples,
                "thread_error": self.thread_error, "stop_timed_out": self.stop_timed_out,
                "partial_measurement": bool(self.thread_error or self.stop_timed_out),
                "skipped_process_reads": self.skipped_process_reads,
                "traversal": self.traversal,
                "unavailable_reason": ("sampler_error" if self.thread_error else
                    "proc_self_unavailable" if self.proc_root_pid is None
                    else "no_descendant_rss_observed") if not self.peak else None,
                "scope": "Sum of local service and worker descendant RSS; sampled lower bound, not unique physical memory"}


def mapped_events(events, padding):
    result = deepcopy(events)
    for event in result:
        event["start_s"] -= padding
        event["end_s"] -= padding
        for sample in event["ridge"]:
            sample["time_s"] -= padding
    return result


def observe_case(directory, case, receipt):
    job_id = receipt.get("job_id")
    job = directory / "http" / "jobs" / job_id if job_id else None
    final = read_json(directory / "http/result.json", {})
    if not final and job is not None:
        final = read_json(job / "result.json", {})
    discovery = read_json(job / "discovery/discovery.json", {}) if job else {}
    events = final.get("events", discovery.get("events", []))
    search = read_json(job / "search/search-report.json", {}) if job else {}
    refined = read_json(job / "refined-candidates.json", {}) if job else {}
    decision = read_json(job / "decision.json", {}) if job else {}
    evidence = read_json(directory / "http/pipeline-evidence.json", {})
    padding = case["encoding"]["leading_silence_s"]
    candidates = []
    groups = {event["event_id"]: event.get("harmonic_group_id", event["event_id"]) for event in events}
    for i, candidate in enumerate(final.get("candidates", [])):
        row = refined.get("ranked", [])[i] if i < len(refined.get("ranked", [])) else {}
        copied = deepcopy(candidate)
        copied["mapped_recording_start_utc"] = format_utc(
            parse_utc(candidate["recording_start_utc"]) + timedelta(seconds=padding))
        copied["matched_group_count"] = len({groups[e] for e in candidate["event_ids"] if e in groups})
        copied["normalized_score"] = candidate["score"] / max(1, copied["matched_group_count"])
        sensitivity = row.get("sensitivity", {})
        copied["uncertainty_provenance"] = {key: sensitivity.get(key) for key in
            ("kind", "method", "sources", "complete", "boundary_limited")}
        copied["uncertainty_radius_m"] = sensitivity.get("radius_m")
        copied["mode_id"] = row.get("mode_id")
        candidates.append(copied)
    keys = ("status", "tiles_total", "tiles_completed", "tiles_resumed", "unsearched_tiles",
            "grid_points_searched", "refinement_points_searched", "refinement_points_skipped",
            "refinement_stages_resumed", "model_assessment_complete", "model_support_complete",
            "data_scope_complete", "all_event_groups_considered", "event_groups_proposed",
            "alternative_modes_exhaustively_assessed", "elapsed_s", "process_peak_rss_mib",
            "bounded_concurrency")
    return {"job_id": job_id, "job_directory": str(job) if job else None,
        "status": final.get("status"), "reason_code": final.get("reason_code"),
        "stage": final.get("stage"), "accepted_location": final.get("status") == "succeeded",
        "verification_passed": receipt.get("verification_passed", False),
        "full_pipeline_observed": receipt.get("full_pipeline_observed", False),
        "full_pipeline_requirements_met": receipt.get("full_pipeline_requirements_met", False),
        "stages_verified": evidence.get("stages_verified", []),
        "http_elapsed_s": receipt.get("elapsed_s"), "interrupted": receipt.get("interrupted", False),
        "verification_errors": {key: receipt[key] for key in
            ("error", "finalization_error", "requirement_error", "pipeline_evidence_errors") if key in receipt},
        "mapped_events": mapped_events(events, padding),
        "ridge_count": len(events), "group_count": len(set(groups.values())),
        "candidates": candidates, "coverage": final.get("coverage"),
        "work": {key: search.get(key) for key in keys},
        "finite_search_evidence": search.get("finite_search_evidence"),
        "decision_provenance": decision.get("diagnostics"),
        "acquisition": read_json(job / "data-provenance.json", {}) if job else {}}


def assign_pairs(left_count, right_count, eligible):
    """Maximum-cardinality, then minimum-cost one-to-one correspondence."""
    if not left_count or not right_count:
        return []
    penalty = 10 * (left_count + right_count + 1)
    costs = np.full((left_count, right_count+left_count), float(penalty))
    costs[:, :right_count] = 3 * penalty
    for (i, j), row in eligible.items():
        costs[i, j] = row["cost"]
    rows, columns = linear_sum_assignment(costs)
    return [(int(i), int(j)) for i, j in zip(rows, columns) if (i, j) in eligible]


def event_correspondence(left, right, protocol):
    rules = protocol["event_correspondence"]
    eligible = {}
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            first, last = max(a["start_s"], b["start_s"]), min(a["end_s"], b["end_s"])
            overlap = max(0.0, last-first) / min(a["end_s"]-a["start_s"], b["end_s"]-b["start_s"])
            if overlap < rules["minimum_overlap_fraction_of_shorter_event"]:
                continue
            ta, fa = np.array([(r["time_s"], r["frequency_hz"]) for r in a["ridge"]]).T
            tb, fb = np.array([(r["time_s"], r["frequency_hz"]) for r in b["ridge"]]).T
            mask = (ta >= tb[0]) & (ta <= tb[-1])
            if not mask.any():
                continue
            frequency = float(np.median(np.abs(fa[mask] - np.interp(ta[mask], tb, fb))))
            if frequency > rules["maximum_matching_median_frequency_hz"]:
                continue
            eligible[i, j] = {"left_event_id": a["event_id"], "right_event_id": b["event_id"],
                "left_group_id": a.get("harmonic_group_id", a["event_id"]),
                "right_group_id": b.get("harmonic_group_id", b["event_id"]),
                "start_delta_s": b["start_s"]-a["start_s"],
                "end_delta_s": b["end_s"]-a["end_s"],
                "overlap_fraction": overlap, "median_frequency_delta_hz": frequency,
                "compared_ridge_samples": int(mask.sum()),
                "cost": 1-overlap + frequency/rules["maximum_matching_median_frequency_hz"]}
    matches = [eligible[pair] for pair in assign_pairs(len(left), len(right), eligible)]
    left_ids = {row["left_event_id"] for row in matches}
    right_ids = {row["right_event_id"] for row in matches}
    forward, reverse, possible_forward, possible_reverse = {}, {}, {}, {}
    for rows, fwd, rev in ((matches, forward, reverse),
                           (eligible.values(), possible_forward, possible_reverse)):
        for row in rows:
            fwd.setdefault(row["left_group_id"], set()).add(row["right_group_id"])
            rev.setdefault(row["right_group_id"], set()).add(row["left_group_id"])
    return {"matches": matches,
        "unmatched_left": [e["event_id"] for e in left if e["event_id"] not in left_ids],
        "unmatched_right": [e["event_id"] for e in right if e["event_id"] not in right_ids],
        "unmatched_left_groups": sorted({e.get("harmonic_group_id", e["event_id"]) for e in left} - forward.keys()),
        "unmatched_right_groups": sorted({e.get("harmonic_group_id", e["event_id"]) for e in right} - reverse.keys()),
        "split_groups": {key: sorted(value) for key, value in forward.items() if len(value) > 1},
        "merged_groups": {key: sorted(value) for key, value in reverse.items() if len(value) > 1},
        "possible_split_or_ambiguous_groups": {key: sorted(value) for key, value in possible_forward.items() if len(value) > 1},
        "possible_merge_or_ambiguous_groups": {key: sorted(value) for key, value in possible_reverse.items() if len(value) > 1}}


def distance(a, b):
    return float(GEOD.inv(a["lon"], a["lat"], b["lon"], b["lat"])[2])


def associations_equal(a, b, mapping):
    first = list(zip(a["event_ids"], a["flight_ids"]))
    if any(event not in mapping for event, _ in first):
        return False
    return sorted((mapping[event], flight) for event, flight in first) == sorted(zip(b["event_ids"], b["flight_ids"]))


def coverage_state(coverage):
    """Comparable coverage evidence; preserve raw limitations in the report."""
    if not isinstance(coverage, dict):
        return None
    return {key: coverage.get(key) for key in
        ("search_complete", "data_status", "missing_data", "requested_area")}


def completion_state(observation):
    """Completion gates, excluding elapsed time and performance-only counters."""
    work = observation.get("work") or {}
    return {
        "verification_passed": observation.get("verification_passed"),
        "full_pipeline_observed": observation.get("full_pipeline_observed"),
        "full_pipeline_requirements_met": observation.get("full_pipeline_requirements_met"),
        **{key: work.get(key) for key in
           ("status", "unsearched_tiles", "model_assessment_complete",
            "model_support_complete", "data_scope_complete",
            "all_event_groups_considered",
            "alternative_modes_exhaustively_assessed")},
    }


def compare_cases(left, right, protocol):
    a, b = left["observation"], right["observation"]
    tol = protocol["tolerances"]
    correspondence = event_correspondence(a["mapped_events"], b["mapped_events"], protocol)
    mapping = {m["left_event_id"]: m["right_event_id"] for m in correspondence["matches"]}
    coverage_states = [coverage_state(a.get("coverage")), coverage_state(b.get("coverage"))]
    completion_states = [completion_state(a), completion_state(b)]
    result = {"left": left["id"], "right": right["id"], "event_correspondence": correspondence,
        "representation_usable": bool(left.get("usability", {}).get("passed") and right.get("usability", {}).get("passed")),
        "complete_pipelines": bool(a["full_pipeline_requirements_met"] and b["full_pipeline_requirements_met"]),
        "status_transition": [a["status"], b["status"]],
        "reason_transition": [a["reason_code"], b["reason_code"]],
        "group_count_delta": b["group_count"] - a["group_count"],
        "ridge_count_delta": b["ridge_count"] - a["ridge_count"],
        "coverage_equal": a["coverage"] == b["coverage"],
        "coverage_state_transition": coverage_states,
        "completion_state_transition": completion_states,
        "coverage_transition": [a["coverage"], b["coverage"]],
        "work_transition": [a["work"], b["work"]],
        "decision_provenance_equal": a["decision_provenance"] == b["decision_provenance"],
        "diagnostic_candidate_comparison": None, "accepted_location_displacement_m": None,
        "serious_accepted_jump": False, "violations": []}
    checks = {
        "coverage_state_changed": coverage_states[0] != coverage_states[1],
        "work_completion_changed": completion_states[0] != completion_states[1],
        "status_changed": a["status"] != b["status"],
        "reason_changed": a["reason_code"] != b["reason_code"],
        "group_count_changed": a["group_count"] != b["group_count"],
        "ridge_correspondence_incomplete": bool(correspondence["unmatched_left"] or correspondence["unmatched_right"]),
        "group_split_or_merge": bool(correspondence["split_groups"] or correspondence["merged_groups"]),
        "mapped_event_edges": any(max(abs(m["start_delta_s"]), abs(m["end_delta_s"])) > tol["mapped_event_edge_s"]
                                  for m in correspondence["matches"]),
        "mapped_ridge_frequency": any(m["median_frequency_delta_hz"] > tol["median_ridge_frequency_hz"]
                                      for m in correspondence["matches"]),
    }
    if a["candidates"] and b["candidates"]:
        best_a, best_b = a["candidates"][0], b["candidates"][0]
        displacement = distance(best_a, best_b)
        clock_delta = abs((parse_utc(best_b["mapped_recording_start_utc"])
                           - parse_utc(best_a["mapped_recording_start_utc"])).total_seconds())
        same_associations = associations_equal(best_a, best_b, mapping)
        same_units = best_a["score_kind"] == best_b["score_kind"]
        comparable = same_associations and same_units and best_a["matched_group_count"] == best_b["matched_group_count"]
        score_delta = abs(best_a["normalized_score"] - best_b["normalized_score"]) if comparable else None
        result["diagnostic_candidate_comparison"] = {
            "displacement_m": displacement, "within_threshold_m": {
                str(int(limit)): displacement <= limit for limit in tol["displacement_thresholds_m"]},
            "mapped_clock_delta_s": clock_delta, "associated_flights_equal": same_associations,
            "score_units_equal": same_units, "normalized_score_comparable": comparable,
            "normalized_score_delta": score_delta,
            "uncertainty_provenance_equal": best_a["uncertainty_provenance"] == best_b["uncertainty_provenance"],
            "uncertainty_radius_m": [best_a["uncertainty_radius_m"], best_b["uncertainty_radius_m"]]}
        checks.update(associated_flights_changed=not same_associations, score_units_changed=not same_units,
            normalized_score_changed=score_delta is not None and score_delta > tol["normalized_score_per_matched_group"],
            fitted_clock_changed=clock_delta > tol["mapped_clock_s"],
            diagnostic_candidate_jump=displacement > tol["accepted_jump_m"],
            uncertainty_provenance_changed=best_a["uncertainty_provenance"] != best_b["uncertainty_provenance"])
        if a["accepted_location"] and b["accepted_location"]:
            result["accepted_location_displacement_m"] = displacement
            result["serious_accepted_jump"] = displacement > tol["accepted_jump_m"]
        eligible = {}
        for i, first in enumerate(a["candidates"]):
            for j, second in enumerate(b["candidates"]):
                if associations_equal(first, second, mapping):
                    separation = distance(first, second)
                    clock = abs((parse_utc(first["mapped_recording_start_utc"])
                                 - parse_utc(second["mapped_recording_start_utc"])).total_seconds())
                    same_units = first["score_kind"] == second["score_kind"]
                    if separation <= tol["distant_mode_m"] and clock <= tol["mapped_clock_s"] and same_units:
                        eligible[i, j] = {"cost": separation/tol["distant_mode_m"] + clock/tol["mapped_clock_s"],
                            "distance_m": separation, "mapped_clock_delta_s": clock,
                            "score_kind": first["score_kind"],
                            "normalized_score_delta": abs(first["normalized_score"]-second["normalized_score"])
                                if first["matched_group_count"] == second["matched_group_count"] else None,
                            "uncertainty_provenance_equal": first["uncertainty_provenance"] == second["uncertainty_provenance"]}
        pairs = assign_pairs(len(a["candidates"]), len(b["candidates"]), eligible)
        result["retained_mode_correspondence"] = [
            dict(left_index=i, right_index=j, **{key: value for key, value in eligible[i, j].items() if key != "cost"})
            for i, j in pairs]
        result["unmatched_retained_left_indices"] = sorted(set(range(len(a["candidates"]))) - {i for i, _ in pairs})
        result["unmatched_retained_right_indices"] = sorted(set(range(len(b["candidates"]))) - {j for _, j in pairs})
        checks["retained_mode_score_changed"] = any(row["normalized_score_delta"] is not None
            and row["normalized_score_delta"] > tol["normalized_score_per_matched_group"]
            for row in result["retained_mode_correspondence"])
        checks["retained_mode_uncertainty_provenance_changed"] = any(
            not row["uncertainty_provenance_equal"] for row in result["retained_mode_correspondence"])
        distant = []
        for label, source, matched in (("left", a, {i for i, _ in pairs}),
                                       ("right", b, {j for _, j in pairs})):
            for index, candidate in enumerate(source["candidates"]):
                offset = distance(source["candidates"][0], candidate)
                if offset >= tol["distant_mode_m"]:
                    distant.append({"side": label, "candidate_index": index,
                        "distance_from_winner_m": offset, "retained_within_1000m": index in matched})
        result["distant_modes"] = distant
        checks["distant_mode_lost"] = any(not row["retained_within_1000m"] for row in distant)
    elif bool(a["candidates"]) != bool(b["candidates"]):
        checks["candidate_presence_changed"] = True
    result["violations"] = [name for name, failed in checks.items() if failed]
    return result


def build_report(plan_path, plan, state):
    cases = state["cases"]
    observed = [row for row in cases if row.get("observation")]
    pairs = []
    for a, b in itertools.combinations(observed, 2):
        try:
            pairs.append(compare_cases(a, b, plan["protocol"]))
        except Exception as exc:
            pairs.append({"left": a["id"], "right": b["id"],
                "error": f"{type(exc).__name__}: {exc}",
                "violations": ["comparison_error"], "serious_accepted_jump": False})
    completed = [row for row in cases if row["state"] == "finished"]
    passed = [row for row in completed if row.get("observation", {}).get("verification_passed")]
    stages = [row for row in passed if row["observation"]["full_pipeline_observed"]]
    full = [row for row in passed if row["observation"]["full_pipeline_requirements_met"]]
    violations = [pair for pair in pairs if pair["violations"]]
    return {"schema_version": 1, "plan": str(plan_path), "plan_sha256": http_verifier.sha256(plan_path),
        "updated_utc": http_verifier.now(), "source_note": plan["source_note"], "scope_note": plan["scope_note"],
        "declared_case_count": len(plan["cases"]), "finished_cases": len(completed),
        "verified_cases": len(passed), "complete_stage_cases": len(stages), "full_pipeline_cases": len(full),
        "run_state": state["state"], "error": state.get("error"), "cases": cases, "pairwise": pairs,
        "pairs_with_observed_differences": len(violations),
        "serious_accepted_jumps": [p for p in pairs if p["serious_accepted_jump"]],
        "all_complete_usable_and_within_tolerances": (state["state"] == "finished"
            and len(full) == len(plan["cases"])
            and all(row.get("usability", {}).get("passed") for row in cases) and not violations),
        "truth_evaluation": plan["truth_evaluation"], "national_gate_satisfied": False,
        "interpretation": "Inconclusive decisions remain inconclusive. Pairwise agreement is not location accuracy. Early exits do not complete the pipeline requirement."}


def save_report(plan_path, plan, state, output):
    report = build_report(plan_path, plan, state)
    write_json(output / "report.json", report)
    lines = ["# Real recording representation matrix", "", plan["scope_note"], "",
        f"Full pipeline observed in {report['full_pipeline_cases']}/{report['declared_case_count']} declared cases.",
        "No independent truth error was evaluated. National acceptance remains open.", "",
        "| Case | State | Status / reason | Groups / ridges | Usable | Full pipeline | HTTP seconds |",
        "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in state["cases"]:
        obs = row.get("observation", {})
        seconds = obs.get("http_elapsed_s")
        lines.append(f"| {row['id']} | {row['state']} | {obs.get('status')} / {obs.get('reason_code')} | "
            f"{obs.get('group_count')} / {obs.get('ridge_count')} | {row.get('usability', {}).get('passed')} | "
            f"{obs.get('full_pipeline_requirements_met')} | {round(seconds, 2) if seconds is not None else 'unavailable'} |")
    lines += ["", f"Pairs with observed differences: {report['pairs_with_observed_differences']}.",
              f"Serious accepted location jumps: {len(report['serious_accepted_jumps'])}.", "",
              "The JSON report retains mapped ridges, correspondence, diagnostic candidates,",
              "coverage/work transitions, uncertainty provenance, and per-arm reproducers."]
    if state.get("error"):
        lines += ["", "Run error: " + state["error"]]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    return report


def run(plan_path, output, *, selected_case=None):
    plan_path, output = Path(plan_path).resolve(strict=True), Path(output).resolve()
    plan = read_json(plan_path)
    check_frozen(plan_path, plan)
    if "job_max_runtime_s" not in inspect.signature(http_verifier.verify_http).parameters:
        raise ValueError("This runner needs the HTTP verifier's job_max_runtime_s API")
    if selected_case and selected_case not in {case["id"] for case in plan["cases"]}:
        raise ValueError("Unknown case identifier")
    output.mkdir(parents=True, exist_ok=False)
    state = {"state": "running", "runner_pid": os.getpid(), "started_utc": http_verifier.now(),
             "cases": [{"id": case["id"], "state": "pending"} for case in plan["cases"]]}
    write_json(output / "state.json", state)
    save_report(plan_path, plan, state, output)

    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"Received {signal.Signals(signum).name}")

    previous = signal.signal(signal.SIGTERM, interrupted)
    active = None
    try:
        canonical, reference = make_canonical(plan["source"]["path"], output, plan["protocol"])
        write_json(output / "canonical.json", identity(canonical))
        baseline_input = None
        if selected_case and selected_case.startswith("chunks_"):
            baseline = next(case for case in plan["cases"] if case["id"] == "baseline")
            baseline_dir = output / "baseline-encoding"
            baseline_dir.mkdir()
            baseline_input = identity(encode(canonical, baseline_dir, baseline["encoding"], plan["protocol"]))
        for case, row in zip(plan["cases"], state["cases"]):
            if selected_case and case["id"] != selected_case:
                row["state"] = "not_selected"
                continue
            check_frozen(plan_path, plan)
            active = row
            directory = output / case["id"]
            directory.mkdir()
            row.update(state="running", directory=str(directory),
                reproducer=shlex.join([sys.executable, str(Path(__file__).resolve()), "run",
                    "--plan", str(plan_path), "--output", str(directory)+"-reproducer", "--case", case["id"]]))
            write_json(output / "state.json", state)
            try:
                audio = encode(canonical, directory, case["encoding"], plan["protocol"])
                row["encoded_input"] = identity(audio)
                try:
                    decoded = preflight_decode(audio, directory / "preflight", plan["protocol"])
                    native = native_decode_metrics(audio, directory, plan["protocol"])
                    pcm = compare_pcm(reference.samples_path, decoded.samples_path, case["encoding"], plan["protocol"])
                    row["usability"] = {"pcm": pcm, "native_decode": native,
                        "passed": bool(pcm["passed"] and native["absolute_peak"] < plan["protocol"]["tolerances"]["decoded_absolute_peak_exclusive"]
                            and (native["channels"] != 2 or native["identical_channel_max_difference"] == 0.0))}
                except Exception as exc:
                    row["usability"] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
                if case["id"] == "baseline":
                    baseline_input = row["encoded_input"]
                if case["id"].startswith("chunks_"):
                    if baseline_input is None:
                        raise ValueError("Chunk input cannot be verified without the baseline encoding")
                    same_input = row["encoded_input"]["sha256"] == baseline_input["sha256"]
                    row["usability"]["chunk_input_matches_baseline"] = same_input
                    row["usability"]["passed"] = bool(row["usability"]["passed"] and same_input)
                write_json(directory / "representation.json", {"encoding": case["encoding"],
                    "encoded_input": row["encoded_input"], "usability": row["usability"],
                    "request": case["request"], "operator": case["operator"]})
                # Failed usability checks do not remove encoded arms or cause
                # outcome-dependent settings changes. The upload remains fresh.
                memory = MemorySampler()
                try:
                    with memory:
                        receipt = http_verifier.verify_http(audio, directory / "http",
                            request=case["request"], operator=case["operator"], expect_status=None,
                            job_max_runtime_s=plan["protocol"]["budgets"]["job_max_runtime_s"],
                            timeout_s=plan["protocol"]["budgets"]["http_timeout_s"],
                            require_full_pipeline=False)
                finally:
                    row["memory"] = memory.result()
                row["observation"] = observe_case(directory, case, receipt)
                row["state"] = "finished"
                if receipt.get("interrupted"):
                    raise KeyboardInterrupt("HTTP verification was interrupted")
            except Exception as exc:
                row.update(state="error", error=f"{type(exc).__name__}: {exc}")
                if (directory / "http/http-verification.json").is_file() and "observation" not in row:
                    try:
                        row["observation"] = observe_case(directory, case,
                            read_json(directory / "http/http-verification.json"))
                    except Exception as parse_error:
                        row["observation_error"] = f"{type(parse_error).__name__}: {parse_error}"
            finally:
                write_json(output / "state.json", state)
                save_report(plan_path, plan, state, output)
            print(json.dumps({"case": row["id"], "state": row["state"],
                "status": row.get("observation", {}).get("status"),
                "reason": row.get("observation", {}).get("reason_code"),
                "representation_usable": row.get("usability", {}).get("passed")}), flush=True)
            active = None
        check_frozen(plan_path, plan)
        state["state"] = "finished"
    except BaseException as exc:
        state.update(state="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                     error=f"{type(exc).__name__}: {exc}")
        if active is not None and active["state"] == "running":
            active.update(state="interrupted", error=state["error"])
    finally:
        signal.signal(signal.SIGTERM, previous)
        write_json(output / "state.json", state)
        report = save_report(plan_path, plan, state, output)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pre = commands.add_parser("prepare", help="Freeze a protocol before executing its cases")
    pre.add_argument("--audio", type=Path, required=True)
    pre.add_argument("--request", type=Path, required=True)
    pre.add_argument("--operator", type=Path, required=True)
    pre.add_argument("--output", type=Path, required=True)
    pre.add_argument("--source-note", required=True)
    pre.add_argument("--scope-note", required=True)
    pre.add_argument("--frozen-input", type=Path, action="append", default=[])
    pre.add_argument("--job-max-runtime-s", type=float)
    pre.add_argument("--http-timeout-s", type=float)
    execute = commands.add_parser("run", help="Execute fresh jobs; never resume old scientific work")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--case", help="One-arm reproducer; never counts as the full matrix")
    report = commands.add_parser("report", help="Compare preserved observations without executing the pipeline")
    report.add_argument("--plan", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            path = prepare(args.audio, read_json(args.request), read_json(args.operator), args.output,
                source_note=args.source_note, scope_note=args.scope_note, additional_inputs=args.frozen_input,
                job_max_runtime_s=args.job_max_runtime_s, http_timeout_s=args.http_timeout_s)
            print(path)
            return 0
        if args.command == "run":
            result = run(args.plan, args.output, selected_case=args.case)
        else:
            plan_path = args.plan.resolve(strict=True)
            plan = read_json(plan_path)
            if http_verifier.sha256(plan_path) != plan_path.with_name("plan.sha256").read_text().strip():
                raise ValueError("The frozen plan changed")
            if http_verifier.sha256(__file__) != plan["implementation"]["runner_sha256"]:
                raise ValueError("Use the frozen runner to reproduce these comparisons")
            result = save_report(plan_path, plan, read_json(args.output / "state.json"), args.output)
        print(json.dumps({key: result[key] for key in
            ("run_state", "finished_cases", "full_pipeline_cases", "pairs_with_observed_differences",
             "all_complete_usable_and_within_tolerances")}))
        return 0 if result["all_complete_usable_and_within_tolerances"] else 1
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
