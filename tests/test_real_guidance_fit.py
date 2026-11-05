"""Read-only guidance contract from archived real measured ridge fits, not accuracy evidence."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from dopplergeist.confidence import DecisionPolicy, _candidate_evidence_summary
from dopplergeist.guidance import guidance


ROOT = Path(__file__).resolve().parents[1]


def recorded_case():
    source = json.loads((ROOT / "tests/data/real-refinement-profile.json").read_text())
    original = source["candidate"]
    candidate = {key: original[key] for key in
                 ("lat", "lon", "score", "score_kind", "event_ids", "flight_ids")}
    candidate["recording_start_utc"] = datetime.fromtimestamp(
        original["recording_start_time_s"], tz=timezone.utc).isoformat().replace("+00:00", "Z")
    evidence = dict(zip(original["event_ids"], original["event_diagnostics"]))
    summary = _candidate_evidence_summary(source["events"], candidate, evidence)
    return candidate, {"policy": asdict(DecisionPolicy()), "candidate_evidence_summary": summary}


def check_evidence_pointers(items, diagnostics, reason):
    saved_decision = {"diagnostics": diagnostics}
    saved_result = {"reason_code": reason}
    for item in items:
        for reference in item["evidence"]:
            name, pointer = reference.split("#", 1)
            value = saved_decision if name == "decision.json" else saved_result
            assert name in {"decision.json", "result.json"}
            for token in pointer.lstrip("/").split("/"):
                value = value[int(token)] if isinstance(value, list) else value[token]
            assert value is not None


def test_real_recorded_bad_fit_and_drift_have_measured_guidance():
    candidate, diagnostics = recorded_case()
    fits = diagnostics["candidate_evidence_summary"]["event_fits"]
    assert fits[0]["rmse_hz"] > diagnostics["policy"]["max_event_rmse_hz"]
    assert fits[0]["drift_at_bound"] is True
    for reason, expected, field in (
        ("inconsistent_event_match", "acoustic_fit", "/rmse_hz"),
        ("source_drift_unresolved", "source_drift", "/drift_at_bound"),
    ):
        result = {"status": "inconclusive", "reason_code": reason, "candidates": [candidate]}
        decision = dict(result, diagnostics=diagnostics)
        items = guidance(result, decision)
        assert len(items) == 1
        assert items[0]["code"] == expected
        assert any(pointer.endswith(field) for pointer in items[0]["evidence"])
        assert items[0]["benefit"] == "unmeasured"
        check_evidence_pointers(items, diagnostics, reason)
        # A decision from an earlier candidate state cannot justify this guidance.
        stale = dict(decision, candidates=[dict(candidate, score=candidate["score"] - 1)])
        assert guidance(result, stale) == []


def test_recorded_full_track_support_does_not_invent_a_failure():
    candidate, diagnostics = recorded_case()
    assert all(row["support_fraction"] == 1 for row in
               diagnostics["candidate_evidence_summary"]["event_fits"])
    result = {"status": "inconclusive", "reason_code": "insufficient_track_support",
              "candidates": [candidate]}
    assert guidance(result, dict(result, diagnostics=diagnostics)) == []
