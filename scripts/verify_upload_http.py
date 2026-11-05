#!/usr/bin/env python3
"""Verify an actual local MP3 upload without a browser.

Exit zero means HTTP, persistence, stage evidence, and outcome checks passed.
The receipt separately preserves the producer's scientific status. An expected
inconclusive response is never counted as supported localization or accuracy.
With --require-full-pipeline, verification also requires every pipeline stage
and at least one returned candidate; an early exit cannot pass that requirement.
The browser verifier checks rendering separately. Optional runtime, cancellation,
and restart controls exercise actual terminal behavior without replacing workers.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.machinery import EXTENSION_SUFFIXES
import http.client
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

import dopplergeist
from dopplergeist.contracts import utc_timestamp, validate_recording, validate_request, validate_result
from dopplergeist.jobs import TERMINAL, write_json


CANCELLABLE_STAGES = ("intake", "discovery", "acquisition", "search", "refinement", "decision")


def _implementation_files(package):
    """Independently cover Python sources, native C, and loaded-format binaries."""
    return sorted(path for path in package.iterdir()
                  if path.is_file() and (path.suffix in {".py", ".c"}
                      or any(path.name.endswith(suffix) for suffix in EXTENSION_SUFFIXES)))


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("HTTP verification exceeded its declared time budget")
    return value


class VerificationInterrupted(InterruptedError):
    """An outer termination request must unwind through service cleanup."""


class TerminationGuard:
    def __init__(self, receipt):
        self.receipt = receipt
        self.received_monotonic = None
        self.interruptible = False
        self.previous = None
        self.installed = False

    def __enter__(self):
        # Only the main thread can install handlers. The CLI always uses it.
        if threading.current_thread() is threading.main_thread():
            self.previous = signal.signal(signal.SIGTERM, self.handle)
            self.installed = True
        self.receipt["sigterm_cleanup_handler_installed"] = self.installed
        return self

    def __exit__(self, *_):
        if self.installed:
            signal.signal(signal.SIGTERM, self.previous)

    def handle(self, signum, _frame):
        if self.received_monotonic is not None:
            return  # Repeated signals must not interrupt teardown.
        self.received_monotonic = time.monotonic()
        self.receipt.update(interrupted=True, interruption_signal=signal.Signals(signum).name,
                            interruption_received_utc=now(), verification_passed=False,
                            supported_decision=False)
        if self.interruptible:
            self.raise_if_interrupted()

    def raise_if_interrupted(self):
        if self.received_monotonic is not None:
            raise VerificationInterrupted("HTTP verification interrupted by SIGTERM")


def stop_service(process, receipt, termination):
    """Leave time to write a receipt before the outer helper's 5 s kill grace."""
    if process is None:
        return
    service = receipt["service"]
    if process.poll() is None:
        service.update(shutdown_signal="SIGINT", shutdown_requested_utc=now())
        try:
            process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 10
        while process.poll() is None:
            # JobManager allows 3 s to stop its active worker. A SIGTERM can
            # arrive during ordinary shutdown, so reconsider this bound often.
            if termination.received_monotonic is not None:
                deadline = min(deadline, termination.received_monotonic + 3.5)
            left = deadline - time.monotonic()
            if left <= 0:
                service["forced_kill"] = True
                process.kill()
                killed = time.monotonic()
                reap_deadline = killed + 5
                while process.poll() is None:
                    if termination.received_monotonic is not None:
                        reap_deadline = min(reap_deadline, killed + 0.5,
                                            termination.received_monotonic + 4.0)
                    reap_left = reap_deadline - time.monotonic()
                    if reap_left <= 0:
                        raise subprocess.TimeoutExpired(process.args, max(0, reap_deadline - killed))
                    try:
                        process.wait(timeout=min(0.05, reap_left))
                    except subprocess.TimeoutExpired:
                        pass
                break
            try:
                process.wait(timeout=min(0.05, left))
            except subprocess.TimeoutExpired:
                pass
    service.update(returncode=process.returncode, stopped_utc=now())


class LocalClient:
    """Direct loopback HTTP; proxy environment variables are not consulted."""

    def __init__(self, port, deadline):
        self.port, self.deadline = port, deadline

    def request(self, method, path, *, body=None, headers=None, destination=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                timeout=min(30, remaining(self.deadline)))
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            if destination is not None:
                with Path(destination).open("xb") as target:
                    while block := response.read(1024 * 1024):
                        remaining(self.deadline)
                        target.write(block)
                return response.status, None
            payload = response.read(64 * 1024 * 1024 + 1)
            if len(payload) > 64 * 1024 * 1024:
                raise ValueError("HTTP JSON response exceeds the verifier's 64 MiB limit")
            return response.status, json.loads(payload)
        finally:
            connection.close()

    def get(self, path):
        code, value = self.request("GET", path)
        if code != 200:
            raise RuntimeError(f"GET {path} returned HTTP {code}")
        return value


def wait_for_service(process, log_path, deadline):
    while True:
        if process.poll() is not None:
            raise RuntimeError("Upload service exited during startup; inspect service.log")
        match = re.search(r"DOPPLERGEIST: http://127\.0\.0\.1:(\d+) ", log_path.read_text())
        if match:
            return LocalClient(int(match.group(1)), deadline)
        time.sleep(min(0.05, remaining(deadline)))


def poll_job(client, job_id, history_path, *, interval_s, cancel_at_stage=None, cancellation=None):
    cancellation = {} if cancellation is None else cancellation
    with history_path.open("x") as history:
        previous = None
        while True:
            value = client.get(f"/api/jobs/{job_id}")
            validate_result(value)
            if value["job_id"] != job_id:
                raise ValueError("Polled result belongs to a different job")
            if value != previous:
                history.write(json.dumps({"observed_utc": now(), "result": value}, allow_nan=False) + "\n")
                history.flush()
                previous = value
            if value["status"] in TERMINAL:
                if cancel_at_stage is not None and not cancellation:
                    raise ValueError(f"Job ended before cancellation stage {cancel_at_stage!r} was observed")
                return value
            if (cancel_at_stage is not None and not cancellation
                    and value["status"] == "running" and value.get("stage") == cancel_at_stage):
                cancellation.update(observed_status=value["status"], observed_stage=value["stage"],
                                    requested_utc=now())
                code, reply = client.request("POST", f"/api/jobs/{job_id}/cancel")
                cancellation["http_status"] = code
                if code != 200:
                    raise ValueError(f"Cancellation returned HTTP {code}")
                validate_result(reply)
                if reply["job_id"] != job_id:
                    raise ValueError("Cancellation returned a different job")
                cancellation["response_status"] = reply["status"]
            time.sleep(min(interval_s, remaining(client.deadline)))


def inspect_restart(output, command, environment, final, source_sha256, deadline,
                    termination, report, *, repeat_cancel=False):
    """Restart only this verifier's service and read its completed job again."""
    directory = output / "jobs" / final["job_id"]
    result_digest = sha256(directory / "result.json")
    process = None
    report.update(passed=False, service={"shutdown_signal": None, "forced_kill": False, "returncode": None})
    try:
        with (output / "restart-service.log").open("xb") as log:
            try:
                termination.interruptible = False
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                           env=environment, start_new_session=True)
                report["service"]["pid"] = process.pid
                termination.interruptible = True
                termination.raise_if_interrupted()
                client = wait_for_service(process, output / "restart-service.log", deadline)
                report["service"]["port"] = client.port
                job_path = f"/api/jobs/{final['job_id']}"
                if client.get(job_path) != final:
                    raise ValueError("Restart changed the completed API result")
                if repeat_cancel:
                    code, cancelled = client.request("POST", job_path + "/cancel")
                    if code != 200 or cancelled != final:
                        raise ValueError("Repeated cancellation changed the completed result")
                    report["terminal_cancel_idempotent"] = True
                code, _ = client.request("GET", job_path + "/artifacts/input.mp3",
                                         destination=output / "downloaded-original-after-restart.mp3")
                if code != 200 or sha256(output / "downloaded-original-after-restart.mp3") != source_sha256:
                    raise ValueError("Restart changed the original MP3 download")
                code, _ = client.request("GET", job_path + "/artifacts/result.json",
                                         destination=output / "downloaded-result-after-restart.json")
                if code != 200 or sha256(output / "downloaded-result-after-restart.json") != result_digest:
                    raise ValueError("Restart changed the saved result bytes")
                if client.get(job_path) != final:
                    raise ValueError("Terminal API result changed during restart readback")
                report.update(terminal_readback_verified=True, original_download_verified=True,
                              saved_result_bytes_verified=True)
            finally:
                termination.interruptible = False
                stop_service(process, report, termination)
        if report["service"]["returncode"] != 0 or report["service"]["forced_kill"]:
            raise ValueError("Restarted service did not stop cleanly")
        if sha256(directory / "result.json") != result_digest or sha256(directory / "input.mp3") != source_sha256:
            raise ValueError("Restart shutdown changed completed artifacts")
        report["passed"] = True
    finally:
        termination.interruptible = True
        termination.raise_if_interrupted()


def inspect_job(directory, final, wire, operator, original, implementation):
    """Check actual persisted evidence without changing it or opening truth."""
    read = lambda path: json.loads((directory / path).read_text())
    input_record, stored_request = read("input.json"), read("request.json")
    if input_record != {"sha256": sha256(original), "bytes": original.stat().st_size, "path": "input.mp3"}:
        raise ValueError("Persisted input receipt does not describe the submitted bytes")
    if sha256(directory / "input.mp3") != input_record["sha256"]:
        raise ValueError("Persisted original differs from the submitted MP3")
    uploaded = Path(stored_request.pop("audio_path"))
    if uploaded != directory / "input.mp3" or stored_request != wire:
        raise ValueError("Persisted request differs from the submitted request")
    if read("operator.json") != operator:
        raise ValueError("Persisted operator configuration differs from the submitted configuration")
    if read("result.json") != final:
        raise ValueError("Persisted terminal result differs from the HTTP result")
    provenance = read("provenance.json")
    if (provenance.get("source_sha256") != input_record["sha256"]
            or provenance.get("implementation_files") != implementation):
        raise ValueError("Automatic provenance does not identify the submitted input and executed source")
    if final.get("recording", {}).get("source_sha256", input_record["sha256"]) != input_record["sha256"]:
        raise ValueError("Decoded recording identifies a different source")
    return {"original_preserved": True, "request_preserved": True,
            "operator_preserved": True, "result_preserved": True,
            "automatic_provenance_verified": True}


def complete_finite_clock_assessment(assessment, *, allow_legacy=False):
    """Validate a finite winner under the recorded trial-count convention."""
    if (not isinstance(assessment, dict) or assessment.get("complete") is not True
            or not all(type(assessment.get(key)) is int and assessment[key] >= 0
                       for key in ("scheduled_trials", "evaluated_trials", "rejected_trials", "unknown_trials"))
            or assessment["unknown_trials"] != 0 or assessment["evaluated_trials"] == 0):
        return False
    semantics = assessment.get("trial_count_semantics")
    if semantics == "disjoint_evaluated_rejected_unknown":
        reasons = assessment.get("reasons")
        return (assessment["scheduled_trials"] == assessment["evaluated_trials"] + assessment["rejected_trials"]
                and assessment.get("empty_clock_domain") is False
                and isinstance(reasons, list) and all(isinstance(reason, str) for reason in reasons))
    if semantics is not None or not allow_legacy:
        return False
    # Historical #86 records count rejected trials inside evaluated_trials.
    return (assessment["scheduled_trials"] == assessment["evaluated_trials"]
            and assessment["rejected_trials"] <= assessment["evaluated_trials"]
            and assessment.get("unknown_reasons") == {}
            and assessment.get("empty_clock_domain") is False)


def bounded_finite_clock_assessment(detail, max_groups):
    """Check persisted association ceilings without recomputing physical fits."""
    if not isinstance(detail, dict):
        return False
    kind = "matched_group_support_minus_normalized_residual"
    assessment, indices = detail.get("scoring_assessment"), detail.get("event_indices")
    if (not isinstance(assessment, dict) or detail.get("score_kind") != kind
            or assessment.get("complete") is not False
            or assessment.get("unknown_score_bound_kind") != kind
            or assessment.get("unknown_score_bound_method") not in
               ("nonnegative_loss_and_validated_event_fits", "group_count_ceiling",
                "nonnegative_loss_validated_fits_and_acoustic_dual",
                "nonnegative_loss_validated_fits_and_acoustic_floor_dual")
            or not isinstance(indices, list) or not indices
            or any(type(index) is not int or not 0 <= index < max_groups for index in indices)
            or indices != sorted(set(indices))):
        return False
    names = ("scheduled_trials", "evaluated_trials", "rejected_trials", "unknown_trials",
             "bounded_unknown_trials", "bounded_unknown_rejections")
    if not all(type(assessment.get(name)) is int and assessment[name] >= 0 for name in names):
        return False
    unknown, bounded, rejected = (assessment[name] for name in names[3:])
    if (not 0 < unknown == bounded or rejected > bounded
            or assessment.get("trial_count_semantics") != "disjoint_evaluated_rejected_unknown"
            or assessment.get("empty_clock_domain") is not False
            or assessment["scheduled_trials"] != sum(assessment[name] for name in names[1:4])):
        return False
    method = assessment["unknown_score_bound_method"]
    dual = assessment.get("acoustic_bound_rejected_trials", 0)
    floor_dual = assessment.get("acoustic_floor_pruned_trials", 0)
    if type(dual) is not int or type(floor_dual) is not int:
        return False
    if method == "nonnegative_loss_validated_fits_and_acoustic_floor_dual":
        if not (0 < floor_dual <= bounded-rejected and 0 <= dual <= rejected):
            return False
    elif floor_dual != 0:
        return False
    elif method == "nonnegative_loss_validated_fits_and_acoustic_dual":
        if not 0 < dual <= rejected:
            return False
    elif dual != 0:
        return False
    upper = assessment.get("unknown_score_upper_bound")
    if method == "group_count_ceiling":
        return (detail.get("finite_best") is False
                and assessment["evaluated_trials"] == assessment["rejected_trials"] == 0
                and unknown == assessment["scheduled_trials"] and rejected == 0
                and type(upper) in (int, float) and math.isfinite(upper) and upper == len(indices))
    return (upper is None if rejected == bounded else
            type(upper) in (int, float) and math.isfinite(upper) and 0 <= upper <= len(indices))


def model_assessment_accounted_for(search):
    """Require explicit model accounting when the producer reports that contract."""
    if not any(name in search for name in ("model_support", "model_support_complete", "model_assessment_complete")):
        return True  # Historical reports predate this separate coverage contract.
    support = search.get("model_support")
    names = ("unsupported_cpa_track_evaluations", "unclassified_score_candidates",
             "evaluated_trials", "rejected_trials", "unknown_trials", "unreported_trial_envelopes")
    if (not isinstance(support, dict)
            or not all(type(support.get(name)) is int and support[name] >= 0 for name in names)):
        return False
    if "model_assessment_complete" in search and search["model_assessment_complete"] is not True:
        return False
    if search.get("model_support_complete") is True:
        return not any(support[name] for name in (names[0], names[1], "unknown_trials", "unreported_trial_envelopes"))
    finite = search.get("finite_search_evidence")
    bounded_names = ("bounded_score_candidates", "unbounded_score_candidates", "unreported_trial_envelopes")
    return (search.get("model_support_complete") is False and search.get("model_assessment_complete") is True
        and isinstance(finite, dict)
        and finite.get("score_kind") == "matched_group_support_minus_normalized_residual"
        and all(type(support.get(name)) is int and support[name] >= 0 for name in bounded_names)
        and not any(support[name] for name in (names[0], bounded_names[1], bounded_names[2]))
        and 0 < support["bounded_score_candidates"] == support["unclassified_score_candidates"]
        and support["unknown_trials"] >= support["bounded_score_candidates"]
        and type(finite.get("bounded_unknown_envelopes")) is int
        and finite["bounded_unknown_envelopes"] == support["bounded_score_candidates"])


def valid_clock_exclusion(item, configuration):
    """Check a recorded time bound, without reloading the source trajectory."""
    from decimal import Decimal, localcontext

    def number(value):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("invalid bound number")
        return Decimal.from_float(float(value))

    try:
        scoring, filters = configuration["scoring"], configuration["filters"]
        window = scoring["recording_start_window_s"]
        if (not isinstance(item, dict) or not isinstance(item["track_id"], str) or not item["track_id"]
                or not isinstance(item["reason"], str) or not item["reason"]
                or not isinstance(item["reported_intervals"], dict)
                or item["reported_intervals"].get("reason") != item["reason"]
                or scoring.get("mode") != "robust_ridge" or scoring.get("time_origin") is not None
                or filters.get("association", "subsets") != "subsets" or filters.get("silence")
                or not isinstance(window, list) or len(window) != 2):
            return False
        with localcontext() as ctx:
            # Enough digits to add arbitrary finite binary64 inputs without
            # losing a very small operand next to a very large one.
            ctx.prec = 1100
            lo, hi = map(number, window)
            sweep, offset = number(scoring.get("time_sweep_s", 5.0)), number(filters.get("offset_tolerance_s", 5.0))
            c = number(scoring.get("c", 343.0))
            if lo > hi or sweep < 0 or offset < 0 or c <= 0:
                return False
            lower_bounds, upper_bounds = [], []
            for event in configuration["events"]:
                event_tolerance = number(event.get("quality", {}).get("cpa_tolerance_s", 0))
                if event_tolerance < 0:
                    return False
                cpa = number(event["cpa_receive_s"])
                lower_bounds.append(lo - sweep - offset / 2 - event_tolerance + cpa)
                upper_bounds.append(hi + sweep + offset / 2 + event_tolerance + cpa)
            if item.get("exclusion_kind") == "before_events":
                radii = [number(event["slant_range_m"][1]) for event in configuration["events"]]
                if not radii or min(radii) <= 0 or max(radii) >= 1000000:
                    return False
                first_event = number(item["earliest_event_receive_s"])
                last_emission = number(item["latest_emission_s"])
                last_receive = number(item["latest_possible_receive_s"])
                return bool(lower_bounds and first_event <= min(lower_bounds)
                            and last_receive >= last_emission + max(radii) / c
                            and last_receive < first_event)
            if "exclusion_kind" in item:
                return False
            saved = number(item["latest_event_receive_s"])
            first = number(item["earliest_emission_s"])
            return bool(upper_bounds and saved >= max(upper_bounds) and first > saved)
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return False


def valid_interval_cpa_support(item, configuration):
    """Check recorded spatial inequalities; source splines are not reloaded."""
    from decimal import Decimal, localcontext

    def number(value):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("invalid interval bound number")
        return Decimal.from_float(float(value))

    try:
        method = item.get("method") if isinstance(item, dict) else None
        spatial_methods = {"outside_native_hulls_and_sample_edges",
                           "outside_native_hulls_and_bounded_sample_edges"}
        scoped_methods = {"outside_native_hulls_and_bounded_sample_edges",
                          "outside_bounded_cpa_sample_edges"}
        allowed_methods = spatial_methods | {"outside_bounded_cpa_sample_edges"}
        if (method not in allowed_methods
                or not isinstance(item["track_id"], str) or not item["track_id"]
                or type(item.get("sample_count")) is not int or item["sample_count"] <= 0
                or type(item.get("affected_sample_edges")) is not int or item["affected_sample_edges"] < 0
                or type(item.get("receiver_point_count")) is not int or item["receiver_point_count"] <= 0
                or item["affected_sample_edges"] >= item["sample_count"]
                or item["sample_count"] > configuration["budget"]["max_track_samples_per_tile"]):
            return False
        with localcontext() as ctx:
            ctx.prec = 1100
            c = number(configuration["scoring"].get("c", 343.0))
            radius = max(number(event["slant_range_m"][1]) for event in configuration["events"])
            step = number(configuration["budget"]["cpa_step_s"])
            maximum_step = number(item["max_sample_step_m"])
            query_radius = number(item["query_radius_m"])
            if (c <= 0 or radius <= 0 or step <= 0 or maximum_step < 0
                    or number(item["sound_speed_mps"]) != c or number(item["max_distance_m"]) != radius
                    or number(item["sample_step_s"]) != step or query_radius < radius + 2*maximum_step):
                return False
            first, last = map(number, item["emission_time_range_s"])
            intervals = item["emission_intervals_s"]
            outside = item.get("outside_query_scope_intervals_s", [])
            if first >= last or not isinstance(intervals, list) or not isinstance(outside, list):
                return False
            for interval_list in (intervals, outside):
                previous = first
                for pair in interval_list:
                    start, end = map(number, pair)
                    if not previous <= start < end <= last:
                        return False
                    previous = end
            if method in scoped_methods:
                sample_range = item["sample_range"]
                receive_bounds = item["receive_time_bounds_s"]
                if (not isinstance(sample_range, list) or len(sample_range) != 2
                        or any(type(value) is not int for value in sample_range)
                        or not 0 <= sample_range[0] <= sample_range[1] <= item["sample_count"]):
                    return False
                receive_lower, receive_upper = map(number, receive_bounds)
                if receive_lower > receive_upper:
                    return False
                if method == "outside_bounded_cpa_sample_edges":
                    if (intervals or not outside or item["affected_sample_edges"] != 0
                            or item["hull_distance_lower_m"] is not None
                            or item["edge_distance_lower_m"] is not None):
                        return False
                elif (not intervals or item["affected_sample_edges"] == 0
                      or number(item["hull_distance_lower_m"]) <= radius
                      or number(item["edge_distance_lower_m"]) <= query_radius):
                    return False
            elif (not intervals or not isinstance(outside, list)
                  or outside or item["affected_sample_edges"] == 0
                  or number(item["hull_distance_lower_m"]) <= radius
                  or number(item["edge_distance_lower_m"]) <= query_radius):
                return False
            lower, upper = item["observer_box_ecef_m"]
            return (len(lower) == len(upper) == 3
                    and all(number(lo) <= number(hi) for lo, hi in zip(lower, upper)))
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return False


def inspect_finite_search(artifact, read, search, configuration, refinement):
    """Audit recorded retention read-only; physical trials are not recomputed."""
    import sqlite3

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()

    finite = search["finite_search_evidence"]
    diagnostics, rows = refinement["matching_diagnostics"], refinement["ranked"]
    require(isinstance(finite, dict) and finite == diagnostics.get("finite_search_evidence"),
            "Search and refinement finite evidence differ")
    require(finite.get("schema_version") == 1 and finite.get("scope") == "evaluated_finite_grid"
            and finite.get("ledger_artifact") == "retention.sqlite", "Unsupported finite retention evidence")
    require(finite.get("max_event_groups") == len(configuration["events"]), "Finite group bound differs from search")
    budget, scoring = configuration["budget"], configuration["scoring"]
    require(finite.get("spacing_m") == budget["spacing_m"]
            and finite.get("refine_spacing_m") == min(budget["spacing_m"], budget["refine_spacing_m"]),
            "Finite resolutions differ from the recorded search budget")
    domain = finite.get("optimizer_domain", {})
    require(domain.get("run_hash") == search["run_hash"]
            and domain.get("source_fit_code_sha256") == configuration["code"].get("matching.py")
            and domain.get("score_code_sha256") == configuration["code"].get("search.py")
            and domain.get("clock_window_s") == scoring.get("recording_start_window_s")
            and domain.get("time_step_s") == scoring.get("time_step_s", .5)
            and domain.get("declared_sweep_s") == scoring.get("time_sweep_s", 5.),
            "Finite optimizer domain differs from the recorded source or shared clock")
    path = artifact("search/retention.sqlite")
    retained = search.get("ranked")
    require(isinstance(retained, list), "Finite search lacks its original retained rows")
    identities = [row.get("mode_id") for row in retained]
    require(all(isinstance(identity, str) and identity for identity in identities)
            and len(identities) == len(set(identities)), "Original retained mode IDs are invalid")
    by_id = dict(zip(identities, retained))
    totals, names, originals = {}, {}, {}
    evaluated = discarded = coarse_unknown = 0
    coarse_trials = dict(evaluated_trials=0, rejected_trials=0, unknown_trials=0, unreported_trial_envelopes=0)
    bounded_count = bounded_rejections = clock_excluded = interval_supported = 0
    bound = unknown_bound = None
    bound_fields = ("bounded_unknown_envelopes", "bounded_unknown_rejections", "best_unknown_score_upper_bound")
    bound_contract = any(name in finite for name in bound_fields) or "model_assessment_complete" in search
    support_totals = dict(unsupported_cpa_track_evaluations=0, unclassified_score_candidates=0,
        bounded_score_candidates=0, unbounded_score_candidates=0, unknown_trials=0, unreported_trial_envelopes=0)
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        require(db.execute("PRAGMA quick_check").fetchall() == [("ok",)], "Corrupt finite retention ledger")
        require(db.execute("SELECT identity,score_kind FROM run").fetchall() ==
                [(search["run_hash"], finite["score_kind"])], "Finite ledger identifies another run or score kind")
        has_bounds = db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='bounds'").fetchone()[0] == 1
        bound_contract = bound_contract or has_bounds
        require(not bound_contract or (has_bounds and all(name in finite for name in bound_fields)),
                "Finite bound evidence lacks its persisted bound table or aggregate fields")
        for name, checksum, occurrences, duplicates, encoded_kept in db.execute(
                "SELECT name,checksum,occurrences,duplicates,kept FROM stages ORDER BY name"):
            phase, suffix = name.split(":", 1)
            require(phase in {"coarse", "global", "fine", "fine-global"} and suffix,
                    "Unknown finite retention stage")
            names.setdefault(phase, set()).add(suffix)
            kept = json.loads(encoded_kept)
            require(isinstance(kept, list) and all(isinstance(identity, str) for identity in kept)
                    and len(kept) == len(set(kept)), "Invalid retained IDs in a ledger stage")
            kept_set, seen_kept, count, stage_digest = set(kept), set(), 0, hashlib.sha256()
            for record in db.execute("SELECT mode_id,score,groups_count,kind,seed FROM records WHERE stage=? ORDER BY mode_id", (name,)):
                identity, score, groups, kind, encoded_seed = record
                seed = json.loads(encoded_seed)
                require(isinstance(seed, dict) and set(seed) == {"event_indices", "flight_ids",
                    "pass_emission_times_s", "position", "recording_start_time_s"}
                    and isinstance(seed["position"], list) and len(seed["position"]) == 5
                    and isinstance(seed["pass_emission_times_s"], list)
                    and isinstance(seed["event_indices"], list) and isinstance(seed["flight_ids"], list)
                    and all(isinstance(flight, str) and flight for flight in seed["flight_ids"])
                    and all(type(value) in (int, float) and math.isfinite(value)
                        for value in [*seed["position"], *seed["pass_emission_times_s"], seed["recording_start_time_s"]]),
                    "Finite ledger seed has invalid physical fields")
                require(type(score) in (int, float) and math.isfinite(score) and kind == finite["score_kind"],
                        "Finite ledger contains an invalid score or mixed units")
                require(type(groups) is int and 1 <= groups <= finite["max_event_groups"]
                        and groups == len(seed["event_indices"]) == len(seed["flight_ids"]) == len(seed["pass_emission_times_s"])
                        and all(type(i) is int and 0 <= i < finite["max_event_groups"] for i in seed["event_indices"])
                        and seed["event_indices"] == sorted(set(seed["event_indices"])),
                        "Finite ledger group or association counts are inconsistent")
                require(identity == "mode-" + digest(seed), "Finite mode ID differs from its exact seed")
                if identity in by_id:
                    row = by_id[identity]
                    expected_seed = {"event_indices": row.get("event_indices", list(range(len(row["flight_ids"])))),
                        "flight_ids": row["flight_ids"], "pass_emission_times_s": row.get("pass_emission_times_s", []),
                        "position": [float(row.get(key, 0)) for key in ("x", "y", "lat", "lon", "elev_m")],
                        "recording_start_time_s": float(row["recording_start_time_s"])}
                    require(seed == row.get("mode_seed") == expected_seed,
                            "Original retained row differs from its physical seed")
                stage_digest.update(json.dumps(record, separators=(",", ":"), allow_nan=False).encode() + b"\n")
                count += 1
                if identity in kept_set:
                    seen_kept.add(identity)
            require(type(occurrences) is int and type(duplicates) is int and occurrences >= count
                    and duplicates == occurrences-count and seen_kept == kept_set,
                    "Finite ledger occurrence, duplicate or retained counts differ")
            stage_digest.update(json.dumps(sorted(kept), separators=(",", ":")).encode())
            stage_digest.update(json.dumps([occurrences, duplicates]).encode())
            stage_bounds = {}
            if has_bounds:
                for record in db.execute("SELECT bound_id,upper_score,groups_count,detail FROM bounds WHERE stage=? ORDER BY bound_id", (name,)):
                    identity, upper, groups, encoded_detail = record
                    detail = json.loads(encoded_detail)
                    require(bound_contract and phase in {"coarse", "fine"}
                            and finite["score_kind"] == "matched_group_support_minus_normalized_residual"
                            and bounded_finite_clock_assessment(detail, finite["max_event_groups"]),
                            "Finite bound has no complete association-trial accounting")
                    receiver, flights, times = (detail.get(key) for key in ("receiver", "flight_ids", "pass_emission_times_s"))
                    require(isinstance(receiver, dict) and set(receiver) == {"x", "y", "lat", "lon", "elev_m"}
                            and all(type(value) in (int, float) and math.isfinite(value) for value in receiver.values())
                            and isinstance(flights, list) and all(isinstance(flight, str) and flight for flight in flights)
                            and isinstance(times, list) and all(type(value) in (int, float) and math.isfinite(value) for value in times)
                            and type(groups) is int and groups == len(detail["event_indices"]) == len(flights) == len(times)
                            and type(detail.get("point_id")) is int and type(detail.get("envelope_index")) is int
                            and detail["envelope_index"] > 0 and type(detail.get("finite_best")) is bool
                            and detail.get("support_failure") is None,
                            "Finite bound has invalid physical association fields")
                    clocks, interval = detail.get("clock_settings"), detail.get("recording_start_bounds_s")
                    require(isinstance(clocks, dict) and set(clocks) == {"time_origin", "time_sweep_s", "time_step_s"}
                            and all(value is None or type(value) in (int, float) and math.isfinite(value) for value in clocks.values())
                            and (clocks["time_sweep_s"] is None or clocks["time_sweep_s"] >= 0)
                            and (clocks["time_step_s"] is None or clocks["time_step_s"] > 0)
                            and (interval is None or isinstance(interval, list) and len(interval) == 2
                                 and all(type(value) in (int, float) and math.isfinite(value) for value in interval)
                                 and interval[0] <= interval[1]),
                            "Finite bound has invalid shared-clock fields")
                    require(identity == "bound-" + digest(detail)
                            and upper == detail["scoring_assessment"]["unknown_score_upper_bound"],
                            "Finite bound differs from its recorded assessment or identity")
                    stage_bounds[identity] = detail
                    bounded_count += 1
                    bounded_rejections += upper is None
                    if upper is not None:
                        unknown_bound = upper if unknown_bound is None else max(unknown_bound, upper)
                    stage_digest.update(json.dumps(record, separators=(",", ":"), allow_nan=False).encode() + b"\n")
            require(stage_digest.hexdigest() == checksum, "Finite ledger stage checksum differs")
            total = totals.setdefault(phase, {"stages": 0, "input_occurrences": 0, "exact_duplicates": 0,
                                             "retained": 0, "discarded": 0})
            for key, value in (("stages", 1), ("input_occurrences", occurrences), ("exact_duplicates", duplicates),
                               ("retained", len(kept)), ("discarded", count-len(kept))):
                total[key] += value
            if phase == "coarse":
                tile = read(f"search/tile-{suffix}.json")
                payload = tile["payload"]
                require(tile.get("run_hash") == search["run_hash"] and tile.get("payload_hash") == digest(payload)
                        and payload.get("retention_checksum") == checksum
                        and sorted(row["mode_id"] for row in payload["ranked"]) == sorted(kept),
                        "Coarse checkpoint differs from its retention stage")
                for row in payload["ranked"]:
                    score, kind, encoded_seed = db.execute(
                        "SELECT score,kind,seed FROM records WHERE stage=? AND mode_id=?", (name, row["mode_id"])).fetchone()
                    seed = json.loads(encoded_seed)
                    physical = {"event_indices": row.get("event_indices", list(range(len(row["flight_ids"])))),
                        "flight_ids": row["flight_ids"], "pass_emission_times_s": row.get("pass_emission_times_s", []),
                        "position": [float(row.get(key, 0)) for key in ("x", "y", "lat", "lon", "elev_m")],
                        "recording_start_time_s": float(row["recording_start_time_s"])}
                    require(row["score"] == score and row.get("score_kind") == kind
                            and row.get("mode_seed") == seed == physical,
                            "Coarse checkpoint score or physical seed differs from its ledger")
                counts = payload.get("counts", {})
                if bound_contract:
                    require("counts" in payload and isinstance(counts, dict), "Missing coarse model-support counts")
                    empty_tile = (counts == {} and payload.get("grid_points") == 0 and count == 0
                                  and not payload["ranked"] and not stage_bounds)
                    empty_cpa = (type(counts.get("cpa_rows")) is int and counts["cpa_rows"] == 0
                                 and type(counts.get("candidates")) is int and counts["candidates"] == 0
                                 and count == 0 and not payload["ranked"] and not stage_bounds)
                    if not empty_tile:
                        require(isinstance(counts.get("unsupported_cpa_tracks"), list)
                                and all(isinstance(item, dict) for item in counts["unsupported_cpa_tracks"]),
                                "Missing coarse unsupported-track diagnostics")
                    if not (empty_tile or empty_cpa):
                        require(all(type(counts.get(key)) is int and counts[key] >= 0 for key in
                                    ("cpa_rows", "candidates", "unclassified_score_candidates",
                                     "bounded_score_candidates", "unbounded_score_candidates", *coarse_trials))
                                and isinstance(counts.get("unclassified_candidate_assessments"), list),
                                "Coarse scoring work lacks explicit model-support counts")
                    for key in coarse_trials:
                        value = counts.get(key, 0)
                        require(type(value) is int and value >= 0, "Invalid coarse clock-trial count")
                        coarse_trials[key] += value
                unknown = counts.get("unclassified_score_candidates", 0)
                require(type(unknown) is int and unknown >= 0, "Invalid coarse unknown-trial count")
                coarse_unknown += unknown
            if bound_contract and phase in {"coarse", "fine"}:
                support_path = path.parent / "support" / (name.replace(":", "-") + ".json")
                details, unsupported, excluded, local_support = [], [], [], []
                if support_path.exists():
                    support_record = read("search/support/" + support_path.name)
                    details, unsupported = (support_record.get(key) for key in
                                            ("unclassified_candidate_assessments", "unsupported_cpa_tracks"))
                    require(support_record.get("stage") == name and isinstance(details, list)
                            and all(isinstance(detail, dict) for detail in details) and isinstance(unsupported, list),
                            "Finite support artifact has invalid stage or candidate evidence")
                    excluded = support_record.get("clock_excluded_unsupported_tracks", [])
                    require(isinstance(excluded, list) and all(valid_clock_exclusion(item, configuration) for item in excluded),
                            "Invalid unsupported-track clock exclusion evidence")
                    local_support = support_record.get("interval_supported_cpa_tracks", [])
                    require(isinstance(local_support, list)
                            and all(valid_interval_cpa_support(item, configuration) for item in local_support)
                            and len({item["track_id"] for item in local_support}) == len(local_support),
                            "Invalid interval CPA motion-support evidence")
                certificates = {"bound-" + digest(detail): detail for detail in details
                                if bounded_finite_clock_assessment(detail, finite["max_event_groups"])}
                require(stage_bounds == certificates, "Finite bound ledger omits or invents a recorded candidate assessment")
                if phase == "coarse":
                    counts = payload.get("counts", {})
                    require(counts.get("unclassified_candidate_assessments", []) == details
                            and counts.get("unsupported_cpa_tracks", []) == unsupported
                            and counts.get("clock_excluded_unsupported_tracks", []) == excluded
                            and counts.get("interval_supported_cpa_tracks", []) == local_support
                            and counts.get("unclassified_score_candidates", 0) == len(details)
                            and counts.get("bounded_score_candidates", 0) == len(stage_bounds)
                            and counts.get("unbounded_score_candidates", 0) == len(details)-len(stage_bounds),
                            "Coarse support counts differ from persisted candidate assessments")
                support_totals["unsupported_cpa_track_evaluations"] += len(unsupported)
                clock_excluded += len(excluded)
                interval_supported += len(local_support)
                support_totals["unclassified_score_candidates"] += len(details)
                support_totals["bounded_score_candidates"] += len(stage_bounds)
                support_totals["unbounded_score_candidates"] += len(details)-len(stage_bounds)
                for detail in details:
                    assessment = detail.get("scoring_assessment")
                    if isinstance(assessment, dict) and all(type(assessment.get(key)) is int and assessment[key] >= 0
                            for key in ("evaluated_trials", "rejected_trials", "unknown_trials")):
                        support_totals["unknown_trials"] += assessment["unknown_trials"]
                    else:
                        support_totals["unreported_trial_envelopes"] += 1
        require(db.execute("SELECT count(*) FROM records WHERE stage NOT IN (SELECT name FROM stages)").fetchone()[0] == 0,
                "Finite ledger contains records without a stage")
        require(not has_bounds or db.execute("SELECT count(*) FROM bounds WHERE stage NOT IN (SELECT name FROM stages)").fetchone()[0] == 0,
                "Finite ledger contains bounds without a stage")
        # Checksums cover stage records; an unchecked secondary mode_index can
        # still omit alternatives while quick_check reports success.
        for identity, score in db.execute("SELECT mode_id,MAX(score) FROM records NOT INDEXED GROUP BY mode_id"):
            evaluated += 1
            if identity in by_id:
                originals[identity] = score
            else:
                discarded += 1
                bound = score if bound is None else max(bound, score)
    finally:
        db.close()
    require(names.get("coarse", set()) == names.get("global", set())
            and len(names.get("coarse", set())) == search["tiles_completed"]
            and names.get("fine", set()) == names.get("fine-global", set()),
            "Finite ledger stage coverage is incomplete")
    fine_points = search.get("refinement_points_searched", 0)
    require(type(fine_points) is int and fine_points >= 0
            and bool(names.get("fine")) == (fine_points > 0), "Finite refinement stage records are missing")
    require(finite.get("stages") == totals and set(originals) == set(identities),
            "Finite stage totals or original retained records differ")
    discarded += bounded_count-bounded_rejections
    if unknown_bound is not None:
        bound = unknown_bound if bound is None else max(bound, unknown_bound)
    require(finite.get("pruning_accounted_for") is True and finite.get("discarded_count") == discarded
            and finite.get("evaluated_modes") == evaluated, "Finite distinct retention counts differ")
    unknown = finite.get("unclassified_score_candidates")
    require(type(unknown) is int and unknown >= coarse_unknown, "Finite unknown-trial count is inconsistent")
    all_unknown_accounted = unknown == 0
    if bound_contract:
        require(type(finite["bounded_unknown_envelopes"]) is int
                and type(finite["bounded_unknown_rejections"]) is int
                and finite["bounded_unknown_envelopes"] == bounded_count
                and finite["bounded_unknown_rejections"] == bounded_rejections
                and finite["best_unknown_score_upper_bound"] == unknown_bound,
                "Finite bound aggregates differ from the persisted association ceilings")
        support = search.get("model_support")
        require(isinstance(support, dict) and all(type(support.get(key)) is int and support[key] == value
                for key, value in support_totals.items()) and unknown == support_totals["unclassified_score_candidates"]
                and finite.get("unsupported_cpa_track_evaluations") == support_totals["unsupported_cpa_track_evaluations"],
                "Finite model-support totals differ from persisted candidate assessments")
        require(support.get("clock_excluded_unsupported_track_evaluations", 0) == clock_excluded,
                "Clock-excluded unsupported-track totals differ from stage evidence")
        require(support.get("interval_supported_cpa_track_evaluations", 0) == interval_supported,
                "Interval-supported CPA totals differ from stage evidence")
        require(all(type(support.get(key)) is int and support[key] >= value
                    for key, value in coarse_trials.items()),
                "Search model-support totals omit recorded coarse clock trials")
        all_unknown_accounted = model_assessment_accounted_for(search)
        require(type(search.get("model_assessment_complete")) is bool
                and search["model_assessment_complete"] == all_unknown_accounted,
                "Model assessment completeness contradicts its bound evidence")
    expected_bound = bound if all_unknown_accounted else None
    require(finite.get("best_discarded_score") == expected_bound,
            "Finite discarded score bound differs from all recorded distinct modes")
    if not all_unknown_accounted:
        require(finite.get("discarded_bound_status") == "unknown_model_or_trial_support",
                "Unknown trials have no explicit unbounded status")
    require(type(finite.get("complete")) is bool
            and finite["complete"] == (search["status"] == "complete" and all_unknown_accounted),
            "Finite completeness contradicts search or unknown trial evidence")
    require(finite.get("retained_modes") == [{"mode_id": identity, "score": originals[identity],
        "event_groups": len(by_id[identity]["mode_seed"]["event_indices"])} for identity in identities],
        "Finite retained original scores or groups differ")
    core_ids, assessed, unknown_retained = [], [], 0
    window = diagnostics["recording_start_window_s"]
    for identity, row in by_id.items():
        require(row["score"] == originals[identity] and row.get("score_kind") == finite["score_kind"],
                "Retained original row has a different score or score kind")
        assessment = row.get("scoring_assessment", {})
        known = complete_finite_clock_assessment(assessment, allow_legacy=not bound_contract)
        require(row.get("core_assessment_complete") is known, "Finite winner masks missing or unknown clock trials")
        unknown_retained += not known
        if known:
            core_ids.append(identity)
            fits = row.get("event_diagnostics", [])
            if (window[0] <= row["recording_start_time_s"] <= window[1]
                    and len(fits) == len(row["flight_ids"]) > 0
                    and all(isinstance(fit, dict) and type(fit.get("support_fraction")) in (int, float)
                            and fit["support_fraction"] == 1. and type(fit.get("score")) in (int, float)
                            and math.isfinite(fit["score"]) for fit in fits)):
                assessed.append(identity)
    require(unknown >= unknown_retained, "Finite aggregate omits unclassified retained clock trials")
    require(search.get("core_assessed_mode_ids") == core_ids
            and diagnostics.get("core_assessment_mapping_status") == "present"
            and diagnostics.get("assessed_mode_ids") == sorted(assessed),
            "Finite assessed IDs differ from supported producer assessments")
    mapped = [row.get("mode_id") for row in rows]
    require(diagnostics.get("candidate_mode_ids") == mapped and set(mapped) == set(identities),
            "Finite wire candidates omit or invent retained modes")
    for row, identity in zip(rows, mapped):
        seed = by_id[identity]["mode_seed"]
        require(row.get("mode_seed") == seed and row.get("event_indices") == seed["event_indices"]
                and row["flight_ids"] == seed["flight_ids"]
                and row.get("pass_emission_times_s") == seed["pass_emission_times_s"],
                "Refinement borrowed another mode's event-to-flight identity")
    original_fields = ("mode_id", "mode_seed", "pass_emission_times_s", "event_indices", "flight_ids",
                       "lat", "lon", "recording_start_time_s", "score", "score_kind")
    for identity, original in by_id.items():
        require(any(all(row.get(key) == original.get(key) for key in original_fields)
                    and row.get("elev_m", 0) == original.get("elev_m", 0) for row in rows),
                "Refinement omitted an original retained physical point")
    result = {"ledger_verified": True, "complete": finite["complete"] and set(assessed) == set(identities),
            "retained_modes": len(identities), "evaluated_modes": evaluated, "discarded_count": discarded,
            "best_discarded_score": expected_bound, "unclassified_score_candidates": unknown,
            "physical_trials_independently_recomputed": False}
    if bound_contract:
        result.update(unknown_score_bounds_verified=True, bounded_unknown_envelopes=bounded_count,
            bounded_unknown_rejections=bounded_rejections, best_unknown_score_upper_bound=unknown_bound)
    return result


def inspect_discovery_counts(events, provenance):
    """Count independent groups separately from their measured harmonic ridges."""
    groups = len({event.get("harmonic_group_id", event["event_id"]) for event in events})
    if provenance.get("discovered_events") != groups:
        raise ValueError("Provenance has a different independent-event count")
    # Older producers recorded only the group count. The ridge artifact still
    # identifies every event; check the additional counter when it is present.
    if "discovered_ridges" in provenance and provenance["discovered_ridges"] != len(events):
        raise ValueError("Provenance has a different discovered-ridge count")
    return {"event_groups": groups, "ridge_records": len(events)}


def complete_pipeline_with_candidates(evidence, candidate_count):
    """An operational full-path check is stronger than an expected early exit."""
    stages = {"automatic_provenance", "intake", "discovery", "acquisition",
              "search", "refinement", "decision"}
    return (evidence.get("passed") is True and evidence.get("full_pipeline_observed") is True
            and stages.issubset(evidence.get("stages_verified", []))
            and type(candidate_count) is int and candidate_count > 0)


def inspect_pipeline_evidence(directory, final, wire, operator):
    """Check stage receipts, separately from HTTP success or location accuracy.

    A completed scientific decision needs every upstream stage. Early exits need
    the stages they reached; a work limit must not pretend to be a complete run.
    This checks persisted evidence and repeats the decision policy, not decoding,
    trajectory fitting, data acquisition, or comparison with withheld truth.
    """
    directory = Path(directory).resolve()
    scientific = (final.get("status") == "succeeded" or "candidates" in final
        or (final.get("status") == "inconclusive" and final.get("reason_code")
            not in {"no_supported_events", "data_unavailable", "work_limit"}))
    report = {"schema_version": 1, "passed": False, "full_pipeline_observed": False,
        "scientific_decision_expected": scientific, "stages_verified": [], "artifacts": {},
        "errors": [], "location_accuracy_evaluated": False,
        "limitations": ["Stage consistency does not independently reproduce decoding or scientific fitting.",
                        "No withheld-location comparison or accuracy evaluation is performed."]}

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def artifact(name):
        path = directory / name
        require(path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)
                if p != directory and directory in p.parents), f"Missing or unsafe stage artifact: {name}")
        require(path.resolve().is_relative_to(directory), f"Stage artifact leaves the job: {name}")
        report["artifacts"][name] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
        return path

    def read(name):
        def reject_constant(value):
            raise ValueError(f"Nonfinite JSON constant in {name}: {value}")
        value = json.loads(artifact(name).read_text(), parse_constant=reject_constant)
        require(isinstance(value, dict), f"Stage artifact must be a JSON object: {name}")
        return value

    try:
        validate_result(final)
        provenance, input_record = read("provenance.json"), read("input.json")
        request = dict(wire, audio_path=str(directory / "input.mp3"))
        require(read("request.json") == request and provenance.get("request") == request,
                "Automatic provenance does not preserve the submitted request")
        operator_hash = hashlib.sha256(json.dumps(operator, sort_keys=True, allow_nan=False).encode()).hexdigest()
        require(provenance.get("operator_configuration_sha256") == operator_hash,
                "Automatic provenance has a different operator configuration")
        require(provenance.get("source_sha256") == input_record.get("sha256"),
                "Automatic provenance has a different source hash")
        require(provenance.get("ground_truth_used_by_search") is False
                and provenance.get("event_selection") == "automatic_whole_recording",
                "Automatic event selection and truth separation are not recorded")
        interrupted = (final["status"] == "cancelled" or final.get("reason_code") in
                       {"work_limit", "worker_exit", "worker_launch", "processing_error"})
        if not interrupted:
            require(provenance.get("status") == final["status"]
                    and provenance.get("coverage") == final.get("coverage"),
                    "Automatic provenance does not match the terminal status and coverage")
        report["stages_verified"].append("automatic_provenance")

        recording = None
        if scientific or "recording" in final:
            recording = validate_recording(read("intake/recording.json"))
            intake = read("intake/intake.json")
            require(recording == final.get("recording"), "Intake recording differs from the terminal recording")
            require(recording["source_sha256"] == input_record["sha256"], "Intake identifies a different source")
            require(recording.get("time_window") == wire.get("time_window"), "Intake changed the recording clock")
            count = intake.get("sample_count")
            require(type(count) is int and count > 0 and recording["channels"] == 1,
                    "Intake sample count or mono channel evidence is invalid")
            require(artifact("intake/recording.f32").stat().st_size == 4 * count,
                    "Decoded cache size does not match the intake sample count")
            require(math.isclose(recording["duration_s"], count / recording["sample_rate_hz"],
                                 rel_tol=1e-12, abs_tol=1e-12), "Intake duration does not match its sample count")
            require(intake.get("time_status") == ("ready" if wire.get("time_window") else "needs_input"),
                    "Intake time status does not match the submitted clock")
            require(intake.get("decode", {}).get("sample_format") == "f32le", "Decoded cache format is unrecorded")
            report["stages_verified"].append("intake")

        events = final.get("events")
        if scientific or events is not None:
            require(recording is not None, "Discovery has no completed intake evidence")
            discovery = read("discovery/discovery.json")
            require(discovery.get("source_sha256") == input_record["sha256"]
                    and discovery.get("events") == events, "Discovery source or events differ from the terminal result")
            if not interrupted:
                report["discovery_counts"] = inspect_discovery_counts(events, provenance)
            chunks = discovery.get("chunks")
            require(isinstance(chunks, list) and chunks, "Discovery has no whole-recording chunk evidence")
            covered = 0.0
            tolerance = 1 / recording["sample_rate_hz"]
            for chunk in sorted(chunks, key=lambda item: item["start_s"]):
                start, duration = chunk["start_s"], chunk["duration_s"]
                require(all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                            for v in (start, duration)) and start >= 0 and duration > 0,
                        "Discovery chunk bounds are invalid")
                require(start <= covered + tolerance and start + duration <= recording["duration_s"] + tolerance,
                        "Discovery chunk evidence has a gap or exceeds the recording")
                covered = max(covered, start + duration)
            require(covered >= recording["duration_s"] - tolerance, "Discovery did not cover the complete recording")
            report["stages_verified"].append("discovery")

        if final.get("reason_code") == "recording_time_required":
            require(recording is not None and events is not None and not wire.get("time_window")
                    and final["status"] == "needs_input" and final.get("required_fields") == ["time_window"],
                    "Recording-time early exit contradicts its intake evidence")
        if final.get("reason_code") == "no_supported_events":
            require(recording is not None and events == [] and wire.get("time_window") is not None,
                    "No-event early exit contradicts its discovery evidence")

        if scientific:
            require(recording.get("time_window") is not None, "Scientific decision has no recording clock")
            acquisition = read("data-provenance.json")
            manifest, search = read("search/manifest.json"), read("search/search-report.json")
            configuration = manifest["configuration"]
            run_hash = hashlib.sha256(json.dumps(configuration, sort_keys=True, allow_nan=False).encode()).hexdigest()
            require(manifest.get("run_hash") == search.get("run_hash") == provenance.get("search_run_hash") == run_hash,
                    "Search manifest, report, and automatic provenance have different run identities")
            require(configuration.get("code") == provenance.get("implementation_files"),
                    "Search manifest identifies a different implementation")
            bounds = wire["search"].get("bounds_wsen", [-125.0, 24.0, -66.0, 50.0])
            require(configuration.get("bounds") == search.get("scope") == bounds,
                    "Search scope differs from the submitted request")
            window = [utc_timestamp(wire["time_window"][key]).timestamp() for key in ("start_utc", "end_utc")]
            require(configuration.get("scoring", {}).get("recording_start_window_s") == window
                    and search.get("scoring_time_is_locked") is True
                    and search.get("ground_truth_used_by_search") is False,
                    "Search does not preserve one truth-separated recording clock")
            indexes = acquisition.get("flight_indexes")
            require(isinstance(indexes, list) and indexes and isinstance(acquisition.get("terrain"), dict)
                    and acquisition["terrain"], "Acquisition lacks flight-index or terrain evidence")
            require([index["input_hash"] for index in indexes] ==
                    [index["input_hash"] for index in configuration["indexes"]] ==
                    [item["input_hash"] for item in search["data_coverage"]],
                    "Acquisition and search use different flight inputs")
            require([index["coverage"] for index in indexes] ==
                    [item["source"] for item in search["data_coverage"]],
                    "Acquisition and search disagree about flight coverage")
            require(all(item.get("requested_bounds") == bounds and item.get("requested_time_range_s") ==
                        configuration.get("time_range_s") for item in search["data_coverage"]),
                    "Flight-coverage receipts describe another search area or time interval")
            if "terrain_configuration" in acquisition["terrain"]:
                require(acquisition["terrain"] == configuration.get("additional_input_hashes"),
                        "Acquisition and search use different terrain inputs")
            report["stages_verified"].append("acquisition")

            total, completed = search["tiles_total"], search["tiles_completed"]
            require(type(total) is int and type(completed) is int and 0 <= completed <= total and total > 0,
                    "Search tile counts are invalid")
            require(search.get("unsearched_tiles") == total - completed, "Search unsearched-tile count is inconsistent")
            work_complete = (completed == total and not search["tile_failures"] and not search.get("refinement_failure")
                             and search["all_event_groups_considered"] is True and model_assessment_accounted_for(search))
            coverage = final["coverage"]
            require(coverage["search_complete"] == work_complete
                    and coverage["data_status"] == ("available" if search["data_scope_complete"] else "partial")
                    and coverage.get("requested_area") == wire["search"],
                    "Terminal coverage differs from the search report")
            expected_complete = work_complete and search["data_scope_complete"] and search["all_event_groups_considered"]
            require(search["status"] == ("complete" if expected_complete else "partial"),
                    "Search completion status contradicts its work and coverage evidence")
            prepared = configuration["events"]
            groups = [event.get("event_ids", [event["event_id"]]) for event in prepared]
            flattened = [event_id for group in groups for event_id in group]
            require(len(flattened) == len(set(flattened)) and set(flattened) == {event["event_id"] for event in events},
                    "Search grouping omits or duplicates discovered events")
            require(search.get("event_groups_proposed") == len(groups), "Search reports a different event-group count")
            report["stages_verified"].append("search")

            refinement = read("refined-candidates.json")
            candidates, rows = final.get("candidates"), refinement.get("ranked")
            require(isinstance(candidates, list) and isinstance(rows, list) and len(rows) == len(candidates),
                    "Refinement and terminal candidate counts differ")
            pre_decision_candidates = []
            for row, candidate in zip(rows, candidates):
                require(all(row.get(key) == candidate[key] for key in ("lat", "lon", "score"))
                        and row.get("score_kind", "negative_robust_ridge_loss_hz") == candidate["score_kind"],
                        "Refinement and terminal candidate coordinates or scores differ")
                require(math.isclose(row["recording_start_time_s"], utc_timestamp(candidate["recording_start_utc"]).timestamp(),
                                     rel_tol=0, abs_tol=1e-6), "Refinement and terminal candidate clocks differ")
                indices = row.get("event_indices", list(range(len(groups))))
                require(isinstance(indices, list) and all(type(i) is int and 0 <= i < len(groups) for i in indices)
                        and len(indices) == len(set(indices)) == len(row["flight_ids"]),
                        "Refinement event-to-flight association is invalid")
                associated = [(event_id, flight) for i, flight in zip(indices, row["flight_ids"]) for event_id in groups[i]]
                require(associated == list(zip(candidate["event_ids"], candidate["flight_ids"])),
                        "Refinement and terminal event-to-flight associations differ")
                sensitivity = row.get("sensitivity", {})
                uncertainty = ({key: sensitivity[key] for key in ("radius_m", "method", "kind")}
                               if sensitivity.get("complete") and not sensitivity.get("boundary_limited") else None)
                original = dict(candidate)
                original.pop("uncertainty", None)
                if uncertainty is not None:
                    original["uncertainty"] = uncertainty
                pre_decision_candidates.append(original)
            diagnostics = refinement["matching_diagnostics"]
            require(diagnostics.get("recording_start_window_s") == window
                    and diagnostics.get("clock_locked") == search["scoring_time_is_locked"],
                    "Refinement changed the shared recording clock")
            require(diagnostics.get("sensitivity") == (rows[0].get("sensitivity", {}) if rows else {}),
                    "Leading matching diagnostics use another candidate's sensitivity")
            if rows:
                best = rows[0]
                selected = best.get("event_indices", list(range(len(groups))))
                fits = best.get("event_diagnostics", [])
                require(len(fits) == len(selected), "Leading refinement lacks per-event fit evidence")
                require(all(diagnostics.get(event_id) == fit for i, fit in zip(selected, fits) for event_id in groups[i]),
                        "Matching diagnostics differ from the leading candidate's event fits")
                require(set(diagnostics.get("unmatched_event_ids", [])) ==
                        {event["event_id"] for event in events} - set(candidates[0]["event_ids"]),
                        "Matching diagnostics do not account for unmatched acoustic proposals")
            if "finite_search_evidence" in search:
                report["finite_search"] = inspect_finite_search(artifact, read, search, configuration, refinement)
                report["finite_search_verified"] = report["finite_search"]["complete"]
            else:
                require("finite_search_evidence" not in diagnostics, "Refinement invented finite-search evidence")
            report["stages_verified"].append("refinement")

            decision = read("decision.json")
            require(all(decision.get(key) == final.get(key) for key in ("status", "reason_code", "message", "candidates")),
                    "Persisted scientific decision differs from the terminal result")
            from dopplergeist import confidence
            require(sha256(confidence.__file__) == provenance["implementation_files"].get("confidence.py"),
                    "Cannot recheck the decision with a different confidence implementation")
            expected = confidence.decide_location(events, pre_decision_candidates, coverage, matching_diagnostics=diagnostics)
            expected.setdefault("candidates", pre_decision_candidates)
            require(decision == expected,
                    "Saved scientific evidence does not reproduce the persisted decision")
            report["stages_verified"].append("decision")
            report["full_pipeline_observed"] = True
        report["passed"] = True
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    return report


def verify_http(audio, output, *, request=None, operator=None, expect_status,
                expect_reason=None, timeout_s=600.0, poll_interval_s=0.25,
                require_full_pipeline=False, job_max_runtime_s=None,
                cancel_at_stage=None, verify_restart=False):
    """Verify an actual upload; None observes any terminal status for callers.

    A missing job runtime preserves the original timeout_s worker allowance.
    Give the client a larger timeout to observe an intentional worker time limit.
    """
    if expect_status is not None and expect_status not in TERMINAL:
        raise ValueError("Expected status must be terminal or None for observation")
    if type(require_full_pipeline) is not bool:
        raise ValueError("require_full_pipeline must be a boolean")
    if type(verify_restart) is not bool:
        raise ValueError("verify_restart must be a boolean")
    if cancel_at_stage is not None and cancel_at_stage not in CANCELLABLE_STAGES:
        raise ValueError("Cancellation stage must be an actual running pipeline stage")
    job_runtime = timeout_s if job_max_runtime_s is None else job_max_runtime_s
    for name, value in (("timeout_s", timeout_s), ("poll_interval_s", poll_interval_s),
                        ("job_max_runtime_s", job_runtime)):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    audio = Path(audio).resolve(strict=True)
    if audio.suffix.lower() != ".mp3" or not audio.is_file():
        raise ValueError("Supply a local MP3 file")
    wire = dict(validate_request(dict(request or {"schema_version": 1}, audio_path=str(audio))))
    wire.pop("audio_path")
    operator = {} if operator is None else operator
    if not isinstance(operator, dict):
        raise ValueError("Operator configuration must be a JSON object")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "upload-request.json", wire)
    write_json(output / "operator.json", operator)
    package = Path(dopplergeist.__file__).resolve().parent
    implementation = {path.relative_to(package).as_posix(): sha256(path) for path in _implementation_files(package)}
    began, process, final, log = time.monotonic(), None, None, None
    deadline = began + timeout_s
    receipt = {"schema_version": 1, "scope": "actual_local_http_upload_verification",
        "started_utc": now(), "expected_status": expect_status, "expected_reason": expect_reason,
        "timeout_s": timeout_s, "job_max_runtime_s": job_runtime,
        "cancel_at_stage": cancel_at_stage, "verify_restart": verify_restart,
        "source_sha256": sha256(audio), "source_bytes": audio.stat().st_size,
        "request": wire, "implementation_files": implementation, "verifier_sha256": sha256(__file__),
        "python_executable": sys.executable, "python_version": sys.version,
        "verification_passed": False, "http_checks_passed": False, "pipeline_evidence_passed": False,
        "full_pipeline_observed": False, "supported_decision": False,
        "require_full_pipeline": require_full_pipeline, "full_pipeline_requirements_met": False,
        "location_accuracy_evaluated": False, "browser_verified": False,
        "interrupted": False, "interruption_signal": None,
        "limitations": ["HTTP verification does not establish location accuracy or browser behavior.",
                       "Expected inconclusive or failed responses are preserved, not promoted to localization success.",
                       "This verifier accepts no evaluation labels and performs no ground-truth comparison."],
        "service": {"shutdown_signal": None, "forced_kill": False, "returncode": None}}
    with TerminationGuard(receipt) as termination:
        try:
            termination.interruptible = True
            termination.raise_if_interrupted()
            command = [sys.executable, "-m", "dopplergeist.cli", "serve", "--port", "0",
                       "--jobs", str(output / "jobs"), "--config", str(output / "operator.json"),
                       "--max-runtime", str(job_runtime)]
            receipt["service"]["command"] = command
            env = dict(os.environ, PYTHONPATH=str(package.parent) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                       MPLBACKEND="Agg")
            log = (output / "service.log").open("wb")
            termination.interruptible = False
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            receipt["service"]["pid"] = process.pid
            termination.interruptible = True
            termination.raise_if_interrupted()
            client = wait_for_service(process, output / "service.log", deadline)
            receipt["service"]["port"] = client.port
            capabilities = client.get("/api/capabilities")
            write_json(output / "capabilities.json", capabilities)
            if capabilities.get("max_runtime_s") != job_runtime:
                raise ValueError("Service did not retain the requested job runtime")
            with audio.open("rb") as stream:
                code, queued = client.request("POST", "/api/jobs", body=stream, headers={
                    "Content-Type": "application/octet-stream", "Content-Length": str(audio.stat().st_size),
                    "X-Filename": "input.mp3", "X-Dopplergeist-Request": json.dumps(wire)})
            write_json(output / "submission.json", {"http_status": code, "response": queued})
            if code != 202:
                raise RuntimeError(f"Upload returned HTTP {code}")
            validate_result(queued)
            job_id = queued["job_id"]
            if uuid.UUID(job_id).hex != job_id:
                raise ValueError("Service returned an invalid job identifier")
            receipt["job_id"] = job_id
            cancellation = {}
            if cancel_at_stage is not None:
                receipt["cancellation"] = cancellation
            final = poll_job(client, job_id, output / "history.jsonl", interval_s=poll_interval_s,
                             cancel_at_stage=cancel_at_stage, cancellation=cancellation)
            write_json(output / "result.json", final)
            receipt.update(status=final["status"], reason_code=final.get("reason_code"),
                           producer_reported_success=final["status"] == "succeeded",
                           candidate_count=len(final.get("candidates", [])), event_count=len(final.get("events", [])))
            if cancel_at_stage is not None:
                cancellation["confirmed"] = (cancellation.get("response_status") == "cancelled"
                                             and final["status"] == "cancelled")
                if not cancellation["confirmed"]:
                    raise ValueError("Cancellation was requested, but the job did not finish cancelled")
            listing = client.get(f"/api/jobs/{job_id}/artifacts")
            write_json(output / "artifacts.json", listing)
            if sum(item["path"] == "input.mp3" for item in listing["artifacts"]) != 1:
                raise ValueError("Artifact listing does not expose exactly one original MP3")
            code, _ = client.request("GET", f"/api/jobs/{job_id}/artifacts/input.mp3",
                                     destination=output / "downloaded-original.mp3")
            if code != 200 or sha256(output / "downloaded-original.mp3") != receipt["source_sha256"]:
                raise ValueError("Downloaded original differs from the submitted MP3")
            if client.get(f"/api/jobs/{job_id}/artifacts/result.json") != final:
                raise ValueError("Downloaded result artifact differs from the terminal HTTP result")
            receipt["checks"] = inspect_job(output / "jobs" / job_id, final, wire, operator, audio, implementation)
            receipt["checks"].update(downloaded_original_verified=True, downloaded_result_verified=True)
            receipt["http_checks_passed"] = True
            receipt["expectation_matched"] = ((expect_status is None or final["status"] == expect_status)
                and (expect_reason is None or final.get("reason_code") == expect_reason))
            if verify_restart:
                termination.interruptible = False
                stop_service(process, receipt, termination)
                termination.interruptible = True
                termination.raise_if_interrupted()
                if receipt["service"]["returncode"] != 0 or receipt["service"]["forced_kill"]:
                    raise ValueError("Initial service did not stop cleanly before readback")
                receipt["restart_readback"] = {}
                inspect_restart(output, command, env, final, receipt["source_sha256"], deadline,
                                termination, receipt["restart_readback"], repeat_cancel=bool(cancellation))
        except Exception as exc:
            receipt["error"] = f"{type(exc).__name__}: {exc}"
            receipt["timed_out"] = isinstance(exc, TimeoutError)
        finally:
            termination.interruptible = False
            try:
                stop_service(process, receipt, termination)
            except Exception as exc:
                receipt["shutdown_error"] = f"{type(exc).__name__}: {exc}"
            finally:
                if log is not None:
                    log.close()
            try:
                if not receipt["interrupted"]:
                    termination.interruptible = True
                    termination.raise_if_interrupted()
                    receipt["implementation_unchanged"] = implementation == {
                        path.relative_to(package).as_posix(): sha256(path) for path in _implementation_files(package)}
                    if final is not None:
                        directory = output / "jobs" / final["job_id"]
                        receipt["terminal_result_preserved_after_shutdown"] = json.loads((directory / "result.json").read_text()) == final
                        evidence = inspect_pipeline_evidence(directory, final, wire, operator)
                        termination.raise_if_interrupted()
                        write_json(output / "pipeline-evidence.json", evidence)
                        receipt["pipeline_evidence_passed"] = evidence["passed"]
                        receipt["full_pipeline_observed"] = evidence["full_pipeline_observed"]
                        receipt["full_pipeline_requirements_met"] = complete_pipeline_with_candidates(
                            evidence, receipt.get("candidate_count"))
                        receipt["pipeline_evidence_errors"] = evidence["errors"]
                        files = {path.relative_to(directory).as_posix(): {"bytes": path.stat().st_size, "sha256": sha256(path)}
                                 for path in sorted(directory.rglob("*")) if path.is_file() and not path.is_symlink()}
                        receipt["pipeline_evidence_preserved_after_inspection"] = all(
                            files.get(name) == record for name, record in evidence["artifacts"].items())
                        write_json(output / "job-manifest.json", {"job_id": final["job_id"], "files": files})
                    verified = bool(receipt["http_checks_passed"] and receipt["pipeline_evidence_passed"]
                        and receipt.get("pipeline_evidence_preserved_after_inspection") and receipt["implementation_unchanged"]
                        and receipt.get("terminal_result_preserved_after_shutdown")
                        and receipt["service"]["returncode"] == 0 and not receipt["service"]["forced_kill"]
                        and not receipt.get("shutdown_error") and not receipt["interrupted"]
                        and (not verify_restart or receipt.get("restart_readback", {}).get("passed")))
                    if require_full_pipeline and not receipt["full_pipeline_requirements_met"]:
                        receipt["requirement_error"] = (
                            "Full pipeline verification requires verified acquisition, search, refinement, "
                            "and decision stages with at least one candidate.")
                    receipt["verification_passed"] = (verified and bool(receipt.get("expectation_matched"))
                        and (not require_full_pipeline or receipt["full_pipeline_requirements_met"]))
                    receipt["supported_decision"] = (verified and receipt["full_pipeline_observed"]
                                                     and receipt.get("status") == "succeeded")
            except Exception as exc:
                receipt["finalization_error"] = f"{type(exc).__name__}: {exc}"
            termination.interruptible = False
    # The bounded cleanup attempt precedes this handler handoff. Signals handled
    # above are committed below; later signals use the caller's disposition.
    # In the CLI, a later default SIGTERM cannot yield a successful process exit.
    if receipt["interrupted"]:
        receipt.update(verification_passed=False, supported_decision=False,
                       finalization_skipped_reason="verification_interrupted")
        receipt.setdefault("error", "VerificationInterrupted: HTTP verification interrupted by SIGTERM")
    receipt.update(finished_utc=now(), elapsed_s=time.monotonic() - began)
    write_json(output / "http-verification.json", receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New directory; existing evidence is never overwritten")
    parser.add_argument("--operator-config", type=Path)
    parser.add_argument("--start-utc")
    parser.add_argument("--end-utc")
    parser.add_argument("--time-source", default="Explicit verification recording-start window")
    parser.add_argument("--bounds", type=float, nargs=4, metavar=("WEST", "SOUTH", "EAST", "NORTH"),
                        help="Optional predeclared regional bounds inside the CONUS search area")
    parser.add_argument("--expect-status", choices=sorted(TERMINAL), required=True)
    parser.add_argument("--expect-reason")
    parser.add_argument("--require-full-pipeline", action="store_true",
                        help="Fail unless every pipeline stage is verified and at least one candidate is returned")
    parser.add_argument("--timeout-s", type=float, default=600)
    parser.add_argument("--job-max-runtime-s", type=float,
                        help="Worker time budget; defaults to --timeout-s. Use a smaller value to verify work_limit")
    parser.add_argument("--cancel-at-stage", choices=CANCELLABLE_STAGES,
                        help="Request actual API cancellation after this running stage is observed")
    parser.add_argument("--verify-restart", action="store_true",
                        help="Restart this verifier's service and verify unchanged completed artifacts through HTTP")
    parser.add_argument("--poll-interval-s", type=float, default=0.25)
    args = parser.parse_args(argv)
    if args.end_utc and not args.start_utc:
        parser.error("--end-utc requires --start-utc")
    search = {"coverage": "conus"}
    if args.bounds is not None:
        search["bounds_wsen"] = args.bounds
    request = {"schema_version": 1, "search": search}
    if args.start_utc:
        request["time_window"] = {"start_utc": args.start_utc, "end_utc": args.end_utc or args.start_utc,
                                  "source": "user", "source_detail": args.time_source}
    try:
        operator = json.loads(args.operator_config.read_text()) if args.operator_config else {}
        receipt = verify_http(args.audio, args.output, request=request, operator=operator,
            expect_status=args.expect_status, expect_reason=args.expect_reason,
            timeout_s=args.timeout_s, poll_interval_s=args.poll_interval_s,
            require_full_pipeline=args.require_full_pipeline, job_max_runtime_s=args.job_max_runtime_s,
            cancel_at_stage=args.cancel_at_stage, verify_restart=args.verify_restart)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"report": str(args.output.resolve() / "http-verification.json"),
                      "verification_passed": receipt["verification_passed"], "status": receipt.get("status"),
                      "http_checks_passed": receipt["http_checks_passed"],
                      "pipeline_evidence_passed": receipt["pipeline_evidence_passed"],
                      "full_pipeline_observed": receipt["full_pipeline_observed"],
                      "full_pipeline_requirements_met": receipt["full_pipeline_requirements_met"],
                      "reason_code": receipt.get("reason_code"), "supported_decision": receipt["supported_decision"],
                      "location_accuracy_evaluated": False, "interrupted": receipt["interrupted"],
                      "interruption_signal": receipt["interruption_signal"], "error": receipt.get("error"),
                      "requirement_error": receipt.get("requirement_error")}))
    return 0 if receipt["verification_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
