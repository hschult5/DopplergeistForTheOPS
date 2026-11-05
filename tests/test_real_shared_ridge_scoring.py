"""Keep recorded competing paths without counting reused peaks independently."""
import copy
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from dopplergeist.confidence import _possible_harmonic_overlap, _screen_harmonic_overlap
from dopplergeist.contracts import validate_event
from dopplergeist import pipeline, tiled_search
from dopplergeist.data_access import FlightIndex, content_hash
from dopplergeist.finite_search import RetentionLedger, bounded_assessment, classified_assessment
from dopplergeist.matching import association_candidates, prepare_events
from dopplergeist.ridge_overlap import selected_ridge_conflicts
from dopplergeist.search import rank_candidates, score_candidate


DATA = Path(__file__).parent / "data"


def recorded_events(kind):
    fixture = json.loads((DATA / "shared-ridges-real.json").read_text())
    return [validate_event(event) for event in fixture[kind]["events"]]


@pytest.fixture(scope="module")
def http_verifier():
    spec = importlib.util.spec_from_file_location("verify_upload_http_shared_ridges",
                                                 DATA.parents[1] / "scripts/verify_upload_http.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    return verifier


def joint_hypothesis(events):
    # These archived ADS-B passages and receiver are only association/scorer
    # wiring inputs. They are NOT the date, location or aircraft of ESID103.
    # The conflict check must run before consulting or fitting any trajectory.
    table = json.loads((DATA / "reference-cpa-real.json").read_text())["cpa_rows"]
    candidates, _ = association_candidates(pd.DataFrame(table), events)
    candidate = next(row for row in candidates if len(row["event_indices"]) == 2)
    position = json.loads((DATA / "reference-retention-real.json").read_text())["rows"][0]["mode_seed"]["position"]
    point = SimpleNamespace(point_id=candidate["point_id"],
                            **dict(zip(("x", "y", "lat", "lon", "elev_m"), position)))
    return candidate, point


def test_real_shared_paths_keep_every_observation_and_each_separate_hypothesis():
    raw = recorded_events("shared")
    original = copy.deepcopy(raw)
    events, measurements = prepare_events(raw)
    assert raw == original and len(events) == 2
    assert {event["harmonic_group_id"] for event in events} == {event["harmonic_group_id"] for event in raw}
    conflicts = selected_ridge_conflicts(events)
    assert len(conflicts) == 1
    flag = _possible_harmonic_overlap(raw)
    assert {key: value for key, value in conflicts[0].items() if key != "group_ids"} == flag
    assert flag["shared_ridge_samples"] == 8
    assert (flag["shared_run_start_s"], flag["shared_run_end_s"]) == (367.5, 369.5)
    for event, measurement in zip(events, measurements):
        single, observed = prepare_events([next(item for item in raw if item["event_id"] == event["event_id"])])
        assert selected_ridge_conflicts([event]) == []
        assert {key: value for key, value in event.items() if key != "shared_ridge_observations"} == single[0]
        for first, second in zip((measurement[0].time, *measurement[1:3]),
                                 (observed[0][0].time, *observed[0][1:3])):
            np.testing.assert_array_equal(first, second)


def test_real_overlap_screen_reports_an_exhausted_pair_budget():
    events = recorded_events("shared")
    screened = _screen_harmonic_overlap(events, pair_budget=0)
    assert screened["overlap"] is None
    assert screened["assessment"] == {
        "exhaustive": False,
        "conclusive": False,
        "pairs_examined": 0,
        "pair_budget": 0,
    }


@pytest.mark.parametrize("floor", [None, 0.0])
def test_real_joint_subset_is_unknown_before_any_trajectory_access(floor):
    events, measurements = prepare_events(recorded_events("shared"))
    candidate, point = joint_hypothesis(events)
    # An empty lookup makes any attempted trajectory access fail this check.
    row = score_candidate(candidate, point, {}, events, measurements,
                          mode="robust_ridge", score_floor=floor)
    assessment = row["scoring_assessment"]
    assert row["score"] is None and not classified_assessment(row)
    assert bounded_assessment(row)
    assert assessment["reasons"] == ["reused_ridge_observations"]
    assert assessment["scheduled_trials"] == assessment["unknown_trials"] > 0
    assert assessment["evaluated_trials"] == assessment["rejected_trials"] == 0
    assert assessment["unknown_score_upper_bound"] == 2.0
    assert "retention_score_floor" not in assessment
    assert assessment["shared_ridge_observations"] == selected_ridge_conflicts(events)


def test_real_unknown_overlap_preserves_invalid_model_parameter_errors():
    events, measurements = prepare_events(recorded_events("shared"))
    candidate, point = joint_hypothesis(events)
    events[0]["quality"]["source_drift_fraction"] = 0.2
    with pytest.raises(ValueError, match="drift fraction"):
        score_candidate(candidate, point, {}, events, measurements, mode="robust_ridge")


def test_public_ranking_callers_preserve_real_unknown_overlap_accounting(tmp_path, http_verifier):
    events, measurements = prepare_events(recorded_events("shared"))
    candidate, point = joint_hypothesis(events)
    parameters = {"mode": "robust_ridge", "time_sweep_s": 0}
    scored = score_candidate(candidate, point, {}, events, measurements, **parameters)
    assert rank_candidates([candidate], pd.DataFrame([vars(point)]), [], events, measurements,
                           **parameters) == []
    ranked, unknown, reported, unreported = pipeline.score_block([candidate], {point.point_id: point}, {},
        events, measurements, parameters, None)
    assert ranked == [] and len(unknown) == 1 and unreported == 0
    envelope = unknown[0]
    assert envelope["scoring_assessment"] == scored["scoring_assessment"]
    assert envelope["score_kind"] == scored["score_kind"]
    assert not envelope["finite_best"] and envelope["finite_score"] is None
    assert reported == {"evaluated_trials": 0, "rejected_trials": 0,
                        "unknown_trials": scored["scoring_assessment"]["scheduled_trials"]}
    assert envelope["recording_start_domain_s"] == candidate["recording_start_bounds_s"]
    path = tmp_path / "unknown.json"
    pipeline.dump(path, unknown)
    persisted = json.loads(path.read_text())
    assert persisted == unknown
    assert http_verifier.bounded_finite_clock_assessment(persisted[0], 2)
    assert bounded_assessment(persisted[0]) and not classified_assessment(persisted[0])


def test_real_overlap_bound_survives_ledger_and_independent_verifier(tmp_path, http_verifier):
    events, measurements = prepare_events(recorded_events("shared"))
    candidate, point = joint_hypothesis(events)
    row = score_candidate(candidate, point, {}, events, measurements, mode="robust_ridge")
    detail = dict(row, finite_best=False,
                  pass_emission_times_s=[entry["emission_time"] for entry in candidate["passes"]],
                  recording_start_bounds_s=candidate["recording_start_bounds_s"])
    bound = {"bound_id": "bound-" + content_hash(detail), "detail": detail,
             "groups_count": 2, "upper_score": 2.0}
    ledger = RetentionLedger(tmp_path / "retention.sqlite", "real-shared-ridge-regression", row["score_kind"])
    checksum = ledger.record("coarse:shared-paths", [], [], 0, bounds=[bound])
    assert ledger.stage_valid("coarse:shared-paths", checksum)
    with ledger.connect() as database:
        persisted = json.loads(database.execute("SELECT detail FROM bounds").fetchone()[0])
    assert persisted == detail and bounded_assessment(persisted)
    evidence = ledger.evidence([], complete=True, spacing_m=2000, refine_spacing_m=100, max_event_groups=2)
    assert evidence["evaluated_modes"] == 0
    assert evidence["bounded_unknown_envelopes"] == evidence["discarded_count"] == 1
    assert evidence["best_unknown_score_upper_bound"] == evidence["best_discarded_score"] == 2.0
    assert evidence["bounded_unknown_rejections"] == 0

    assert http_verifier.bounded_finite_clock_assessment(persisted, 2)
    # Unknown observation ownership cannot become a fully assessed fit through
    # serialization, even though its conservative bound is accounted for.
    assert not classified_assessment(persisted)


def test_real_separate_groups_are_not_marked_as_reusing_observations():
    raw = recorded_events("separate")
    events, _ = prepare_events(raw)
    assert len(events) == 2 and _possible_harmonic_overlap(raw) is None
    assert selected_ridge_conflicts(events) == []
    assert all("shared_ridge_observations" not in event for event in events)


@pytest.fixture(scope="module")
def real_flight_hypotheses():
    source = os.environ.get("DOPPLERGEIST_REAL_CPA_FIXTURE")
    if not source:
        pytest.skip("set DOPPLERGEIST_REAL_CPA_FIXTURE to an archived real CPA/index fixture")
    fixture = json.loads(Path(source).read_text())
    events, measurements = prepare_events(recorded_events("shared"))
    candidates, _ = association_candidates(pd.DataFrame(fixture["cpa_rows"]), events,
                                            max_candidates=20000)
    joint = next(row for row in candidates if len(row["event_indices"]) == 2)
    singletons = [row for index in range(2)
                  for row in [item for item in candidates if item["event_indices"] == [index]][:3]]
    identifiers = {entry["flight_id"] for row in [joint, *singletons] for entry in row["passes"]}
    tracks = {track.id: track for track in FlightIndex(fixture["real_index"]).lookup(sorted(identifiers))}
    assert set(tracks) == identifiers
    # These real trajectories are deliberately unrelated hypotheses, with no
    # claim that they recorded ESID103's date, receiver location or aircraft.
    return events, measurements, fixture["point"], tracks, joint, singletons


def test_real_single_path_scores_ignore_an_unselected_conflicting_path(real_flight_hypotheses):
    events, measurements, position, tracks, _, candidates = real_flight_hypotheses
    point = SimpleNamespace(**position)
    unmarked = [{key: value for key, value in event.items() if key != "shared_ridge_observations"}
                for event in events]
    assert {tuple(row["event_indices"]) for row in candidates} == {(0,), (1,)}
    for candidate in candidates:
        marked = score_candidate(candidate, point, tracks, events, measurements,
                                 mode="robust_ridge", time_sweep_s=0)
        original = score_candidate(candidate, point, tracks, unmarked, measurements,
                                   mode="robust_ridge", time_sweep_s=0)
        assert json.dumps(marked, sort_keys=True) == json.dumps(original, sort_keys=True)


def test_real_tiled_search_keeps_conflicts_separate_from_retention_pruning(real_flight_hypotheses, tmp_path, http_verifier):
    events, measurements, position, tracks, joint, _ = real_flight_hypotheses
    selected = [tracks[identifier] for identifier in dict.fromkeys(entry["flight_id"] for entry in joint["passes"])]
    ledger = RetentionLedger(tmp_path / "retention.sqlite", "real-shared-path-tile",
                             "matched_group_support_minus_normalized_residual")
    rows, counts = tiled_search._score(pd.DataFrame([position]), selected, events, measurements,
        tiled_search.SearchBudget(top_k=100), {"mode": "robust_ridge", "time_sweep_s": 0},
        {"association": "subsets"}, ledger=ledger, stage="coarse:shared-paths")
    details = counts["unclassified_candidate_assessments"]
    conflicts = [detail for detail in details
                 if "reused_ridge_observations" in detail["scoring_assessment"]["reasons"]]
    assert conflicts and all(len(row["event_indices"]) == 1 for row in rows)
    for detail in conflicts:
        assessment = detail["scoring_assessment"]
        assert not detail["finite_best"] and bounded_assessment(detail)
        assert assessment["unknown_trials"] == assessment["scheduled_trials"] > 0
        assert assessment["unknown_score_upper_bound"] == 2.0
        assert "retention_score_floor" not in assessment
    assert counts["retention_score_pruned_candidates"] == sum(
        "retention_score_floor" in detail["scoring_assessment"] for detail in details if bounded_assessment(detail))
    with ledger.connect() as database:
        persisted = [json.loads(row[0]) for row in database.execute("SELECT detail FROM bounds")]
    assert len(persisted) == counts["bounded_score_candidates"]
    assert all(detail in persisted for detail in conflicts)
    assert all(http_verifier.bounded_finite_clock_assessment(detail, 2) for detail in persisted)
    evidence = ledger.evidence(rows, complete=True, spacing_m=2000, refine_spacing_m=100, max_event_groups=2)
    assert evidence["bounded_unknown_envelopes"] == len(persisted)
    assert evidence["best_unknown_score_upper_bound"] == 2.0
