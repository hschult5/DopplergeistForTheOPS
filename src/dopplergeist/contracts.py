"""Version-one JSON contracts for the automatic workflow.

These are plain dictionaries, independent of a web framework. Validators return
copies and normalize absolute timestamps to UTC. Legacy research configurations
are unchanged. See docs/INTERFACES.md for semantics and producer ownership.
"""
from copy import deepcopy
from datetime import datetime, timezone
import math
from pathlib import PurePosixPath
from typing import Literal, NotRequired, TypedDict

from .geography import profile_for_search


class RecordingTimeWindow(TypedDict):
    start_utc: str
    end_utc: str
    source: Literal["user", "recording_metadata"]
    source_detail: NotRequired[str]


class SearchArea(TypedDict):
    country: NotRequired[Literal["US", "DE", "ES", "FR", "IT"]]
    coverage: NotRequired[Literal["conus", "germany", "spain", "france", "italy"]]
    bounds_wsen: NotRequired[list[float]]


class AnalysisRequest(TypedDict):
    schema_version: Literal[1]
    audio_path: str
    time_window: NotRequired[RecordingTimeWindow | None]
    search: NotRequired[SearchArea]


class RecordingMetadata(TypedDict):
    source_sha256: str
    duration_s: float
    sample_rate_hz: int
    channels: int
    source_format: str
    time_window: NotRequired[RecordingTimeWindow | None]


class RidgePoint(TypedDict):
    time_s: float
    frequency_hz: float
    weight: NotRequired[float]


class DetectedEvent(TypedDict):
    event_id: str
    start_s: float
    end_s: float
    band_hz: list[float]
    ridge: list[RidgePoint]
    harmonic_group_id: NotRequired[str]
    cpa_receive_s: NotRequired[float]
    f0_range_hz: NotRequired[list[float]]
    speed_range_mps: NotRequired[list[float]]
    slant_range_m: NotRequired[list[float]]
    quality: NotRequired[dict[str, float]]


class LocationUncertainty(TypedDict):
    radius_m: float
    method: str
    kind: Literal["sensitivity", "calibrated"]
    coverage_probability: NotRequired[float]
    calibration_reference: NotRequired[str]


class LocationCandidate(TypedDict):
    lat: float
    lon: float
    score: float
    score_kind: str
    event_ids: list[str]
    flight_ids: list[str]
    recording_start_utc: str
    uncertainty: NotRequired[LocationUncertainty]


JobStatus = Literal["queued", "running", "needs_input", "succeeded", "inconclusive", "failed", "cancelled"]


class CoverageReport(TypedDict):
    search_complete: bool
    data_status: Literal["available", "partial", "unavailable"]
    limitations: list[str]
    requested_area: NotRequired[SearchArea]
    missing_data: NotRequired[list[str]]


class JobResult(TypedDict):
    schema_version: Literal[1]
    job_id: str
    status: JobStatus
    stage: NotRequired[str]
    progress: NotRequired[float]
    recording: NotRequired[RecordingMetadata]
    coverage: NotRequired[CoverageReport]
    events: NotRequired[list[DetectedEvent]]
    candidates: NotRequired[list[LocationCandidate]]
    reason_code: NotRequired[str]
    message: NotRequired[str]
    required_fields: NotRequired[list[str]]
    artifacts: NotRequired[dict[str, str]]


def _record(value, required, optional=()):
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing or extra:
        raise ValueError(f"Missing fields: {sorted(missing)}; unknown fields: {sorted(extra)}")
    return deepcopy(value)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _number(value, name, low=-math.inf, high=math.inf):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be finite and between {low} and {high}")


def _interval(value, name, low=0):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{name} must contain two endpoints")
    for number in value:
        _number(number, name, low)
    if value[0] >= value[1]:
        raise ValueError(f"{name} must be strictly increasing")


def utc_timestamp(value):
    """Parse an explicit ISO timestamp; naive timestamps and epoch guesses fail."""
    _text(value, "UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Timestamp must be ISO 8601 with an explicit timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp requires an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _utc(value):
    return utc_timestamp(value).isoformat().replace("+00:00", "Z")


def validate_request(value):
    request = _record(value, ("schema_version", "audio_path"), ("time_window", "search"))
    if type(request["schema_version"]) is not int or request["schema_version"] != 1:
        raise ValueError("Unsupported schema_version")
    _text(request["audio_path"], "audio_path")
    window = request.get("time_window")
    if window is not None:
        window = _record(window, ("start_utc", "end_utc", "source"), ("source_detail",))
        window["start_utc"], window["end_utc"] = _utc(window["start_utc"]), _utc(window["end_utc"])
        if utc_timestamp(window["end_utc"]) < utc_timestamp(window["start_utc"]):
            raise ValueError("Recording-start window is reversed")
        if window["source"] not in ("user", "recording_metadata"):
            raise ValueError("Time source must be user or recording_metadata")
        if "source_detail" in window:
            _text(window["source_detail"], "source_detail")
        request["time_window"] = window
    search = _record(request.get("search", {"coverage": "conus"}), (), ("country", "coverage", "bounds_wsen"))
    if "country" not in search and "coverage" not in search:
        raise ValueError("Search must specify country or coverage")
    for name in ("country", "coverage"):
        if name in search:
            _text(search[name], name)
    profile = profile_for_search(search)
    search["coverage"] = profile.coverage
    # Keep old saved CONUS requests identical when no country was supplied.
    if "country" in search:
        search["country"] = profile.country
    if "bounds_wsen" in search:
        bounds = search["bounds_wsen"]
        if not isinstance(bounds, list) or len(bounds) != 4:
            raise ValueError("bounds_wsen must contain west, south, east, north")
        for i, number in enumerate(bounds):
            _number(number, "bounds_wsen", -180 if i % 2 == 0 else -90, 180 if i % 2 == 0 else 90)
        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            raise ValueError("Bounds must be increasing; antimeridian crossing is unsupported")
        west, south, east, north = profile.bounds_wsen
        if not (west <= bounds[0] < bounds[2] <= east and south <= bounds[1] < bounds[3] <= north):
            name = "CONUS" if profile.coverage == "conus" else profile.name
            raise ValueError(f"bounds_wsen must stay within {name}: west {west:g}, south {south:g}, east {east:g}, north {north:g}")
    request["search"] = search
    return request


def validate_event(value):
    optional = ("harmonic_group_id", "cpa_receive_s", "f0_range_hz", "speed_range_mps", "slant_range_m", "quality")
    event = _record(value, ("event_id", "start_s", "end_s", "band_hz", "ridge"), optional)
    _text(event["event_id"], "event_id")
    _interval([event["start_s"], event["end_s"]], "event time")
    _interval(event["band_hz"], "band_hz", low=1e-12)
    if "harmonic_group_id" in event:
        _text(event["harmonic_group_id"], "harmonic_group_id")
    if "cpa_receive_s" in event:
        _number(event["cpa_receive_s"], "cpa_receive_s", 0)
    for name in ("f0_range_hz", "speed_range_mps", "slant_range_m"):
        if name in event:
            _interval(event[name], name, low=1e-12)
    if not isinstance(event["ridge"], list) or len(event["ridge"]) < 3:
        raise ValueError("A measured ridge requires at least three points")
    previous = -math.inf
    for point in event["ridge"]:
        _record(point, ("time_s", "frequency_hz"), ("weight",))
        _number(point["time_s"], "ridge time_s", event["start_s"], event["end_s"])
        _number(point["frequency_hz"], "ridge frequency_hz", *event["band_hz"])
        _number(point.get("weight", 1), "ridge weight", 0)
        if point["time_s"] <= previous:
            raise ValueError("Ridge times must be strictly increasing")
        previous = point["time_s"]
    quality = event.get("quality", {})
    if not isinstance(quality, dict):
        raise ValueError("quality must be a mapping of diagnostic names to numbers")
    for name, number in quality.items():
        _text(name, "quality name")
        _number(number, name)
    return event


def validate_recording(value):
    recording = _record(value, ("source_sha256", "duration_s", "sample_rate_hz", "channels", "source_format"), ("time_window",))
    checksum = recording["source_sha256"]
    if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
        raise ValueError("source_sha256 must be a lowercase SHA256 hex digest")
    _number(recording["duration_s"], "duration_s", 1e-12)
    for name in ("sample_rate_hz", "channels"):
        if type(recording[name]) is not int or recording[name] <= 0:
            raise ValueError(f"{name} must be a positive integer")
    _text(recording["source_format"], "source_format")
    if "time_window" in recording:
        request = validate_request({"schema_version": 1, "audio_path": "metadata-validation", "time_window": recording["time_window"]})
        recording["time_window"] = request["time_window"]
    return recording


def validate_candidate(value):
    candidate = _record(value, ("lat", "lon", "score", "score_kind", "event_ids", "flight_ids", "recording_start_utc"), ("uncertainty",))
    _number(candidate["lat"], "lat", -90, 90)
    _number(candidate["lon"], "lon", -180, 180)
    _number(candidate["score"], "score")
    _text(candidate["score_kind"], "score_kind")
    for name in ("event_ids", "flight_ids"):
        if not isinstance(candidate[name], list) or not candidate[name]:
            raise ValueError(f"{name} must be a nonempty list")
        for item in candidate[name]:
            _text(item, name)
    if len(candidate["event_ids"]) != len(candidate["flight_ids"]) or len(set(candidate["event_ids"])) != len(candidate["event_ids"]):
        raise ValueError("Each distinct event must map to exactly one flight segment")
    candidate["recording_start_utc"] = _utc(candidate["recording_start_utc"])
    if "uncertainty" in candidate:
        uncertainty = _record(candidate["uncertainty"], ("radius_m", "method", "kind"), ("coverage_probability", "calibration_reference"))
        _number(uncertainty["radius_m"], "radius_m", 0)
        _text(uncertainty["method"], "uncertainty method")
        if uncertainty["kind"] == "calibrated":
            _number(uncertainty.get("coverage_probability"), "coverage_probability", 0, 1)
            _text(uncertainty.get("calibration_reference"), "calibration_reference")
        elif uncertainty["kind"] != "sensitivity" or "coverage_probability" in uncertainty or "calibration_reference" in uncertainty:
            raise ValueError("Uncalibrated sensitivity must not carry a probability")
        candidate["uncertainty"] = uncertainty
    return candidate


def validate_result(value):
    optional = ("stage", "progress", "recording", "coverage", "events", "candidates", "reason_code", "message", "required_fields", "artifacts")
    result = _record(value, ("schema_version", "job_id", "status"), optional)
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise ValueError("Unsupported schema_version")
    _text(result["job_id"], "job_id")
    status = result["status"]
    if status not in ("queued", "running", "needs_input", "succeeded", "inconclusive", "failed", "cancelled"):
        raise ValueError("Unknown job status")
    for name in ("stage", "reason_code", "message"):
        if name in result:
            _text(result[name], name)
    if "progress" in result:
        _number(result["progress"], "progress", 0, 1)
    if "recording" in result:
        result["recording"] = validate_recording(result["recording"])
    if "coverage" in result:
        coverage = _record(result["coverage"], ("search_complete", "data_status", "limitations"), ("requested_area", "missing_data"))
        if type(coverage["search_complete"]) is not bool or coverage["data_status"] not in ("available", "partial", "unavailable"):
            raise ValueError("Coverage must state search completion and data availability")
        for name in ("limitations", "missing_data"):
            if not isinstance(coverage.get(name, []), list):
                raise ValueError(f"{name} must be a list")
            for description in coverage.get(name, []):
                _text(description, name)
        if "requested_area" in coverage:
            coverage["requested_area"] = validate_request({"schema_version": 1, "audio_path": "coverage-validation", "search": coverage["requested_area"]})["search"]
        result["coverage"] = coverage
    for name, validator in (("events", validate_event), ("candidates", validate_candidate)):
        if not isinstance(result.get(name, []), list):
            raise ValueError(f"{name} must be a list")
        if name in result:
            result[name] = [validator(item) for item in result[name]]
    ids = [event["event_id"] for event in result.get("events", [])]
    if len(set(ids)) != len(ids):
        raise ValueError("Event IDs must be unique")
    if "events" in result:
        if any(set(candidate["event_ids"]) - set(ids) for candidate in result.get("candidates", [])):
            raise ValueError("Candidate references an absent event")
    if status == "succeeded" and not result.get("candidates"):
        raise ValueError("A successful location result requires a candidate")
    candidates = result.get("candidates", [])
    if len({candidate["score_kind"] for candidate in candidates}) > 1:
        raise ValueError("Ranked candidates must use comparable score kinds")
    if any(left["score"] < right["score"] for left, right in zip(candidates, candidates[1:])):
        raise ValueError("Candidates must be ranked in descending score order")
    if status in ("needs_input", "inconclusive", "failed"):
        _text(result.get("reason_code"), "reason_code")
        _text(result.get("message"), "message")
    if "required_fields" in result:
        if not isinstance(result["required_fields"], list):
            raise ValueError("required_fields must be a list")
        for name in result["required_fields"]:
            _text(name, "required field")
    if status == "needs_input" and not result.get("required_fields"):
        raise ValueError("needs_input must identify required_fields")
    artifacts = result.get("artifacts", {})
    if not isinstance(artifacts, dict):
        raise ValueError("artifacts must map names to job-relative paths")
    for name, path in artifacts.items():
        _text(name, "artifact name")
        _text(path, "artifact path")
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts or "\\" in path or ":" in path:
            raise ValueError("Artifact paths must stay relative to their job directory")
    return result


def event_measure_settings(value):
    """Map an already discovered event into the unchanged research measure API."""
    event = validate_event(value)
    names = ("start_s", "end_s", "band_hz", "cpa_receive_s", "f0_range_hz", "speed_range_mps", "slant_range_m")
    return {name: event[name] for name in names if name in event}
