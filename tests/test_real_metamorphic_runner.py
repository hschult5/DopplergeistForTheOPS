"""Representation/mapping contracts using archived real ridges and recorded audio."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("real_metamorphic", ROOT / "scripts/run_real_metamorphic.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
PROTOCOL = json.loads(runner.PROTOCOL.read_text())


def recorded_events():
    return json.loads((ROOT / "tests/data/shared-ridges-real.json").read_text())["shared"]["events"]


def test_padding_preserves_both_absolute_utc_endpoints_and_real_ridge_times():
    request = {"schema_version": 1, "time_window": {
        "start_utc": "2026-09-08T20:17:20Z", "end_utc": "2026-09-08T20:17:40Z",
        "source": "user", "source_detail": "Source-informed reference development control"}}
    shifted = runner.shifted_request(request, 17.25)
    assert shifted["time_window"]["start_utc"] == "2026-09-08T20:17:02.750000Z"
    assert shifted["time_window"]["end_utc"] == "2026-09-08T20:17:22.750000Z"
    for key in ("start_utc", "end_utc"):
        assert (runner.parse_utc(request["time_window"][key])
                - runner.parse_utc(shifted["time_window"][key])).total_seconds() == 17.25
    events = recorded_events()
    padded = runner.mapped_events(events, -17.25)
    assert runner.mapped_events(padded, 17.25) == events
    assert request["time_window"]["start_utc"] == "2026-09-08T20:17:20Z"


def test_recorded_ridges_use_one_to_one_correspondence_not_identifiers_or_order():
    left = recorded_events()
    right = deepcopy(list(reversed(left)))
    originals = {}
    for i, event in enumerate(right):
        originals[event["event_id"]] = f"renamed-{i}"
        event["event_id"] = f"renamed-{i}"
        event["harmonic_group_id"] = f"renamed-group-{i}"
    result = runner.event_correspondence(left, right, PROTOCOL)
    assert {row["left_event_id"]: row["right_event_id"] for row in result["matches"]} == originals
    assert len({row["right_event_id"] for row in result["matches"]}) == len(left)
    assert not result["unmatched_left"] and not result["unmatched_right"]
    assert all(row["median_frequency_delta_hz"] == 0 for row in result["matches"])
    missing = runner.event_correspondence(left, right[:1], PROTOCOL)
    assert len(missing["matches"]) == 1
    assert len(missing["unmatched_left"]) == 1
    assert not missing["unmatched_right"]


def test_cache_freeze_detects_an_added_alias_to_existing_real_input(tmp_path):
    source = ROOT / "tests/data/shared-ridges-real.json"
    cache = tmp_path / "immutable-cache"
    cache.mkdir()
    (cache / "first.json").symlink_to(source)
    operator = {"cache_root": str(cache), "offline": True}
    before = runner.operator_inputs(operator, [])
    (cache / "another-name.json").symlink_to(source)
    after = runner.operator_inputs(operator, [])
    assert before != after
    assert len(before) == 1 and len(after) == 2
    assert len({row["resolved_path"] for row in after}) == 1


def test_actual_recorded_clip_encoding_and_padding_contract(tmp_path):
    source = os.environ.get("DOPPLERGEIST_REAL_METAMORPHIC_AUDIO")
    if not source:
        pytest.skip("Set DOPPLERGEIST_REAL_METAMORPHIC_AUDIO to a short actual recording")
    canonical, reference = runner.make_canonical(source, tmp_path, PROTOCOL)
    for name in ("baseline", "identical_stereo", "leading_17_25s", "rate_44100", "gain_minus_6db"):
        case = next(row for row in PROTOCOL["cases"] if row["id"] == name)
        settings = dict(PROTOCOL["baseline"], **case["changes"])
        directory = tmp_path / name
        directory.mkdir()
        encoded = runner.encode(canonical, directory, settings, PROTOCOL)
        decoded = runner.intake_recording(encoded, directory / "preflight", sample_rate_hz=4000)
        metrics = runner.native_decode_metrics(encoded, directory, PROTOCOL)
        comparison = runner.compare_pcm(reference.samples_path, decoded.samples_path, settings, PROTOCOL)
        assert metrics["channels"] == settings["channels"]
        assert metrics["sample_rate_hz"] == settings["sample_rate_hz"]
        assert abs(comparison["duration_error_s"]) <= PROTOCOL["tolerances"]["decoded_duration_error_s"]
        assert comparison["maximum_absolute_lag_s"] <= PROTOCOL["tolerances"]["residual_alignment_lag_s"]
        assert comparison["padding_samples"] == round(settings["leading_silence_s"]*4000)
        if settings["channels"] == 2:
            assert metrics["identical_channel_max_difference"] == 0
        assert metrics["first_packet"]
        # Transform contracts are not full-pipeline or one-hour matrix evidence.


@pytest.mark.parametrize("case_id", ["chunks_60_30", "chunks_180_60"])
def test_chunk_only_replay_compares_actual_baseline(tmp_path, case_id):
    audio = os.environ.get("DOPPLERGEIST_REAL_METAMORPHIC_AUDIO")
    request = os.environ.get("DOPPLERGEIST_REAL_METAMORPHIC_REQUEST")
    operator = os.environ.get("DOPPLERGEIST_REAL_METAMORPHIC_OPERATOR")
    if not all((audio, request, operator)):
        pytest.skip("Set real recording, request and offline operator paths")
    plan = runner.prepare(audio, json.loads(Path(request).read_text()),
        json.loads(Path(operator).read_text()), tmp_path / "plan",
        source_note="Real recorded input for chunk replay control",
        scope_note="Offline single-arm software check, not a localization test",
        job_max_runtime_s=15, http_timeout_s=40)
    output = tmp_path / "run"
    runner.run(plan, output, selected_case=case_id)
    state = json.loads((output / "state.json").read_text())
    row = next(item for item in state["cases"] if item["id"] == case_id)
    assert state["state"] == "finished" and row["state"] == "finished"
    assert row["usability"]["chunk_input_matches_baseline"] is True
    baseline = next((output / "baseline-encoding").glob("*.mp3"))
    assert runner.identity(baseline)["sha256"] == row["encoded_input"]["sha256"]



def test_recorded_matrix_keeps_coverage_and_completion_separate_from_timing():
    """Actual nine-arm results share coverage/work gates, not measured CPU cost."""
    archive = json.loads((ROOT / "docs/verification/real_metamorphic_reference_2026-11-04.json").read_text())
    cases = archive["cases"]
    assert len(cases) == 9 and archive["pairwise_comparison_count"] == 36

    def observed(case):
        return {"work": case["work"],
                "verification_passed": case["verification_passed"],
                "full_pipeline_observed": case["full_pipeline_observed"],
                "full_pipeline_requirements_met": case["full_pipeline_requirements_met"]}

    coverage = runner.coverage_state(cases[0]["coverage"])
    completion = runner.completion_state(observed(cases[0]))
    assert coverage["search_complete"] is False
    assert coverage["data_status"] == "partial"
    assert coverage["missing_data"] == ["historical_flight_scope_or_audible_border_region"]
    assert completion["full_pipeline_requirements_met"] is True
    assert completion["model_assessment_complete"] is False
    assert completion["all_event_groups_considered"] is True
    assert all(runner.coverage_state(case["coverage"]) == coverage for case in cases)
    assert all(runner.completion_state(observed(case)) == completion for case in cases)
    assert len({case["work"]["elapsed_s"] for case in cases}) > 1
    assert len({case["work"]["event_groups_proposed"] for case in cases}) > 1
    # These observed runtime and proposal-count differences are not completion changes.
