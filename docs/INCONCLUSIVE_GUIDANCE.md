# Evidence-based next steps

The local interface displays next steps below the coverage report for supported
terminal results. Each observation links to fields in that job's saved artifacts.
The job's status, reason, candidates, scores and uncertainty remain unchanged.

## Read an existing job

From an installed checkout, run:

```bash
PYTHONPATH=src python scripts/explain_job.py /path/to/job-directory
```

With the local service running, the same producer is available through:

```bash
curl http://127.0.0.1:8765/api/jobs/JOB_ID/guidance
```

Both operations only read `result.json` and optional `decision.json`. They do not
modify job files, resume checkpoints, search settings or decision thresholds.
The existing job-result contract and `/api/jobs/JOB_ID` response are unchanged.
Old saved jobs do not need migration. Recommendations are derived by the currently
installed guidance code, so their wording can change after a software update.

## Additive representation

The guidance response contains `schema_version: 1`, `job_id`, the saved `status`
and `reason_code` (or null), `ranked: false`, `items`, and `limitations`.
Each item contains `code`, `observation`, `suggestion`, `evidence`, and
`benefit: "unmeasured"`. Evidence references use an artifact name and a JSON
pointer, for example `result.json#/required_fields`. The browser downloads the
artifact; the pointer identifies the relevant field within it.

List order is presentation order, not estimated usefulness. No probability,
expected error reduction or guaranteed successful rerun is supplied. Empty items
mean that no supported suggestion was derived, not that a location is established.
An unreadable optional decision suppresses suggestions that need its diagnostics.
Decision-based guidance requires its status, reason and candidate list to match
the saved result. This prevents an older decision from supplying advice after a
different result was saved.

## Current scope

| Saved evidence | Guidance |
| --- | --- |
| Missing recording time and required `time_window` | Supply a defensible UTC window for the original recording start. |
| No discovered events for a missing-time/no-events result | More of the original recording may contain a usable event. |
| Too few matched acoustic groups | Explain the observed group count; a separate flyover may provide more evidence. |
| Measured track support below the saved decision threshold | Identify the affected acoustic event, its support fraction and the recorded policy threshold. |
| Measured acoustic RMSE or outlier fraction above the saved decision threshold | Identify the affected event, offending measurement and threshold. This is a failed association, not a location estimate. |
| Recorded source-frequency drift at its bound | Identify the affected event and report the bounded fit as unresolved. |
| Possible harmonic or reused-ridge overlap | Explain that group independence is unestablished; do not assert aircraft identity. |
| Partial required data or explicit missing identifiers | Restore the identified flight or terrain data. |
| Unavailable data with a recorded acquisition/configuration limit | Inspect that limit before retrying; unavailability alone does not prove an archive gap. |
| Actual wall-time limit and incomplete search | Review runtime; resume only a compatible checkpoint, or rerun. |
| Incomplete search/model assessment | Inspect unfinished or unsupported assessments; extra runtime may not resolve them. |
| Reported competing-hypothesis separation | Preserve both alternatives and report separation and any recorded association difference. |

Coverage advice can coexist with another reason. The initial `unavailable` data
state before acquisition does not by itself trigger a missing-archive claim.
A failed data-access job can receive advice without changing `failed` to an
inconclusive scientific outcome. Other execution failures do not receive generic
retry advice.

This increment does not determine which narrower time window or extra event would
separate competing modes. It does not infer that weak or unmatched acoustic
proposals are confirmed aircraft. It does not perform counterfactuals, rank
suggestions, or recommend ground-truth coordinates or known flight IDs as inputs.

## Verification and remaining work

Use actual completed real-recording jobs when checking recommendations. Replay the
command above, inspect every cited JSON pointer, and compare the saved result
before and after the API/UI read. Check the browser at desktop and mobile widths.
Do not manufacture job outcomes to stand in for missing real evidence.

One local real reference MP3 job verifies the missing-clock case:
3,600.1845 decoded seconds, 18 ridge records, `needs_input` with
`recording_time_required`. Its unknown-clock result must have one `recording_time`
item and no missing-data item. A second completed real job has partial data and
an incomplete search. It produces both `missing_data` and
`incomplete_assessment`; the primary search reason does not hide the data limits.

Both cases passed API serialization, evidence-pointer resolution and actual
Chromium readback at desktop and mobile widths. Saved result bytes and artifact
downloads stayed unchanged. Delayed guidance responses did not overwrite a later
render of the same job. These checks did not publish the source jobs or modify
their original directories.

The browser verifier now checks guidance on every actual upload or saved-job
readback. `--expect-guidance-code` can require a specific observation. Additional
completed real jobs verified `no_events`, `data_access_limit` and `work_limit`
at desktop and mobile widths, including a reload. The latter two came from fresh
whole-reference uploads with an empty offline cache or a 30-second worker limit.
Every cited field resolved, the visible advice matched the API, and saved evidence
bytes stayed unchanged. Both new uploads also passed HTTP service restart checks.
The runtime control ended an incomplete regional development search; it supplies
no national completion, location accuracy or measured-benefit evidence.

Issue [#63](https://github.com/1337isnot1337/DOPPLERGEIST/issues/63) remains open.
Its acceptance still requires completed real jobs for the remaining reasons and both
positive and negative controls for narrower clocks or additional independent
events. Those controls must preserve settings, the competitor population, work
limits, unsupported alternatives and before/after outcomes. No localization
accuracy or measured benefit follows from this software increment.
