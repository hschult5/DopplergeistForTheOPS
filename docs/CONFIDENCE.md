# Evidence checks and inconclusive outcomes

`confidence.decide_location(events, candidates, coverage,
matching_diagnostics=..., policy=None)` consumes the version-one contracts and
returns `status`, `reason_code`, `message` and `diagnostics`. The product stores the
last field in an artifact and only adds accepted contract fields to JobResult.
Ranked candidates may be retained for inspection when the outcome is inconclusive;
the UI must not show them as an accepted recorder position.

The function validates the inputs without changing them or reading evaluation
truth. Malformed producer evidence returns `failed / invalid_evidence`. Missing
recording-clock input remains the orchestrator's `needs_input`; acquisition or
execution exceptions remain execution failures, distinct from insufficient
scientific evidence. Coverage means completion of the requested geographic/time
scope and availability of requested archives, not capture of every aircraft.

When separately labeled matched groups have strongly overlapping measured
ridge samples compatible with a rational harmonic ratio within a conservative
frequency-uncertainty heuristic, the decision is inconclusive. The diagnostic
records the pair and measured discrepancy; it does not relabel events or assert
that they came from the same aircraft. The heuristic is uncalibrated and may
cause false inconclusive outcomes. A missing flag does not establish event
independence, particularly with missing uncertainties or poorly aligned samples.
The screen examines at most 8,192 event pairs per decision. Finding a possible
overlap is sufficient to abstain. If the matched-event screen reaches its pair
budget without a finding, it reports an incomplete assessment and abstains rather
than treating unchecked pairs as independent. The broader proposal screen reports
its assessment state in diagnostics; this cap does not limit event discovery or
change candidate scores and rankings.

## Default policy and evidence

The current default `DecisionPolicy` is `engineering-v3`; the original v1 receipts
remain preserved. Development negative controls motivated explicit abstention
for noise, absent tracks, harmonic duplicates, incomplete coverage, bad residuals
and multiple plausible hypotheses. The engineering thresholds are deliberately
simple and predeclared: normally at least two independent flyovers, <=3 Hz event RMSE,
<=25% outliers and >=95% trajectory support. The current precision target is
1,000 m, following the updated project acceptance requirement; 100–500 m or better
remains preferred. Earlier 100 m runs retain their original policy/threshold.
Sensitivity radius is an engineering envelope, not measured location error.
For `negative_robust_ridge_loss_hz`, a competing loss within 0.5 Hz per independent
event is a near tie. The declared subset-support score has a separate
dimensionless near-tie threshold of 0.2 support units per independent group;
it is not a Hz threshold or probability. A near-tied alternative must fit inside
the actual returned radius, capped at the precision target; overlapping target
circles do not establish containment. A 70 m envelope therefore cannot certify
a near-tied mode 90 m or 150 m from its center. Other score kinds have no declared gap
threshold and abstain. These are regression-tested engineering choices, **not
empirically calibrated thresholds for arbitrary aircraft audio**. No held-out
real recordings have established their false-localization rate.

One flyover can pass the event-count gate with a trusted recording-start window
when the fitted clock lies within it and sensitivity tests cover that **entire
input window**, not only a local clock perturbation. All residual, support,
global-alternative and uncertainty gates still apply. A known clock is not
inferred from the fitted candidate. W5 supplies the validated original request
window as `recording_start_window_s`; W3 profiles expose the tested span in
`sensitivity.settings.clock_bounds_s`.

When a candidate clock falls inside the supplied window, diagnostics also report
`recording_start_window_fit`: the window width, fit offset from the midpoint,
and distance to the nearest edge, all in seconds. These are geometric
descriptions of the optimizer result, not a clock-error estimate or calibration.
They do not change the score, ranking, or decision; the caller remains responsible
for supplying a defensible clock window.

Matched flyovers use one locked recording clock; harmonics count once and must
map to the same flight. The minimum independent-flyover gate counts distinct
matched flight segments: multiple acoustic groups assigned to one segment may
contribute to its fit, but count once toward independent support. A one-flyover
known-clock exception applies only to one matched harmonic group and one segment.
The explicit subset score may leave proposals unmatched
when `unmatched_event_ids` exactly enumerates the original discovered-minus-matched
IDs. Entire harmonic groups must be retained. These omitted proposals remain
unconfirmed sounds, not labelled aircraft misses or silently deleted evidence.
Different retained subsets remain comparable only using their declared support
score and may still trigger flight/location ambiguity. Matching diagnostics
may also supply `supported_event_ids`: independently supported flyovers must all
appear in the winning association. Three, four and more matched groups are all
checked; there is no decision-stage maximum event count. A four-event regression
ensures the fourth event's inconsistent residual cannot be ignored.

Matching diagnostics
use event-ID keys with `rmse_hz`, `outlier_fraction`, `support_fraction` and
`drift_at_bound`. Other required evidence keys are `clock_locked`,
`refinement_boundary_limited`, `alternatives_evaluated` and `sensitivity`.
Alternatives must include the retained global competing hypotheses, not only a
selected local winner. Failed/unrefined hypotheses or incomplete tile coverage
must not silently become evidence that no competing hypothesis exists.

`sensitivity` must carry `complete: true`, `boundary_limited: false`, `radius_m` and `sources`
including `frequency`, `clock`, `altitude` and `sound_speed`. Candidate `uncertainty`
must cover those actual perturbations, with a radius at least as large as the
tested envelope. Unverified calibrated uncertainty claims abstain; this policy
accepts explicitly uncalibrated sensitivity only. The current W3 altitude-only profile does
not meet this requirement, even if its radius is zero. Missing measurement/model
sensitivity therefore yields `uncertainty_not_established`. An engineering pass
can be `succeeded`, but is explicitly uncalibrated and carries no probability.
The synthetic fixture that supplies complete hypothetical evidence tests that
branch; it does not establish that the real producer has provided such evidence.

## Machine-readable reason codes

| Code | Interpretation |
| --- | --- |
| `invalid_evidence` | Malformed producer result; execution failed |
| `no_doppler_events` | No supported discovered event |
| `coverage_unknown` | No coverage report |
| `missing_flight_data` | Archive data unavailable |
| `search_incomplete` | Requested geographic/time search unfinished |
| `partial_flight_coverage` | Some required data missing |
| `no_matching_flights` | No event/track hypothesis |
| `insufficient_independent_events` | Too few flyovers, or matched groups may be overlapping harmonics |
| `inconsistent_harmonic_association` | Harmonics map to different flights |
| `incomplete_harmonic_group` | Partial association omits a matched event's harmonic |
| `unexplained_events` | Unmatched proposals lack explicit subset-association evidence |
| `unmatched_supported_events` | The hypothesis omits independently supported flyovers |
| `recording_clock_mismatch` | Fitted start is outside the trusted original window |
| `timing_not_established` | Shared clock unverified |
| `matching_diagnostics_missing` | Event-fit evidence unavailable |
| `insufficient_track_support` | Incomplete trajectory support |
| `inconsistent_event_match` | Residual/outlier threshold failed |
| `source_drift_unresolved` | Drift estimate missing or on its bound |
| `unsupported_score_kind` | No declared ambiguity rule for score |
| `incomparable_candidates` | Competing hypotheses explain different event sets |
| `ambiguous_locations` | Near-tied materially different locations |
| `ambiguous_flight_match` | Near-tied different flight associations |
| `alternatives_not_assessed` | Retained global alternatives not fully assessed |
| `uncertainty_not_established` | Full measurement/model sensitivity unavailable |
| `calibration_not_verified` | Candidate's calibration claim lacks independent verification |
| `refinement_boundary_limited` | Refinement/sensitivity reaches search boundary |
| `location_uncertainty_too_large` | Sensitivity radius exceeds precision target |
| `supported_by_evidence_checks` | Passed declared checks; uncalibrated |

## Reproduce decision controls

```bash
PYTHONPATH=src .venv/bin/python scripts/evaluate_confidence.py --output results/confidence-new
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_confidence.py tests/test_evaluation.py
```

`benchmarks/decision-inputs.json` contains simulated evidence, separate from
`benchmarks/truth/decision-labels.json`. Decisions are written before truth is
opened. There are eight development and eight held-out fixture cases. Each split
has one engineered supported case and seven expected inconclusive outcomes;
observed abstention is 7/8, unsupported localization 0/8, and the engineered
location error is zero by construction. All case-level outputs are retained.
These public, author-constructed fixture splits verify decision logic only. The
zero errors and false-localization counts do not estimate audio or real-world
localization accuracy. The audio controls and the frozen audit remain separate;
see `benchmarks/README.md` for their evidence limits and real-data gap.

New evaluation reports include successes and false localizations at each of
100, 500 and 1,000 m, with the active threshold stated separately. The frozen
historical runner explicitly retains its original 100 m threshold. An abstained
best candidate may have a diagnostic error at these tiers, but it never counts
as an accepted localization success.
