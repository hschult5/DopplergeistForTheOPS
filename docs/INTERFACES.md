# Automatic workflow contracts, version 1

Issue: [DG-01 / #2](https://github.com/1337isnot1337/DOPPLERGEIST/issues/2).
Implementation: `src/dopplergeist/contracts.py`. The wire objects are plain JSON
dictionaries, described by Python `TypedDict` types and explicit validators. They
do not require a particular HTTP framework, UI library or worker executor.

Use `validate_request`, `validate_recording`, `validate_event`,
`validate_candidate` and `validate_result` at component boundaries. They return
copies, normalize absolute timestamps to UTC, and raise `ValueError` on invalid
input. They validate shape and semantics, not the truth of a measurement or the
correctness of a location. Unknown fields are rejected; coordinate additive changes
through issue #2 before consumers depend on them. A future incompatible wire shape
needs a new schema version.

## Request and recording clock

[Example request](examples/analysis-request.json): `schema_version: 1`, local
`audio_path`, optional `time_window`, optional `search` (defaults to CONUS).
The upload service creates the local audio path after storing the file; the browser
must not be given arbitrary access to server paths. Upload/HTTP handling belongs
to DG-09, not this validator.

`time_window.start_utc` and `end_utc` bound the **start of the recording**, including
both endpoints. Equal endpoints mean one stated start time. They are not event
times or the interval containing all audio samples. Both require ISO 8601 strings
with an explicit timezone; offsets are normalized to UTC. `source` is `user` or
`recording_metadata`, with an optional `source_detail` describing the evidence.
File modification, download and upload timestamps are not recording metadata.

No time window is a valid intake request. After probing metadata, the orchestrator
returns `needs_input` with `required_fields: ["time_window"]` if a defensible window
is still unavailable. Do not fabricate a timestamp. A derived best recording start
is a result field and must not be mislabeled independent timestamp evidence.

`search.country` accepts `US` (CONUS), `DE` (Germany), `ES` (Spain), `FR`
(metropolitan France including Corsica) or `IT` (Italy including Sicily and
Sardinia); lowercase ISO codes are normalized.
The corresponding `search.coverage` values are
`conus`, `germany`, `spain`, `france` and `italy`.
Either country or coverage is sufficient, and contradictory values are rejected.
Legacy `{"coverage":"conus"}` objects keep their serialized shape; the validator
does not insert a country into old requests. Optional `bounds_wsen` further restrict
the selected profile to `[west, south, east, north]` in longitude/latitude degrees.
Bounds must lie inside its declared envelope. Germany's land mask further limits
receiver points. Spain, metropolitan France and Italy use bounding envelopes without
country polygons; provide explicit bounds for a suitably regional search. The
France profile excludes overseas territories. Neighboring aircraft
remain eligible outside the receiver area when they fall within acoustic range. The capability
response publishes the supported catalog; see [country profiles](COUNTRIES.md).
Actual data availability may be smaller and must be reported in provenance and
the decision. Bounds must increase; antimeridian crossing is not supported here.
The automatic national acceptance test omits any truth-derived bounding box.

The request has no event windows, bands, known flight IDs or ground-truth location.
Those remain available in the separate research configuration interface.

## Intake metadata

`RecordingMetadata` records the original file's lowercase `source_sha256`, decoded
`duration_s`, decoded `sample_rate_hz`, decoded `channels`, original `source_format`
(e.g. `mp3`), and optional trustworthy `time_window`. Sample rates/channels describe
the samples handed to discovery, not an earlier source stream. Their first sample
is audio-relative time zero. If decoding introduces an offset, DG-03 must account
for it and record the transformation in its provenance artifact.

## Detected events

[Example event](examples/detected-event.json): a stable `event_id`, audio-relative
`start_s`/`end_s`, positive `[low, high]` `band_hz`, and at least three ordered ridge
points. Each point has `time_s`, `frequency_hz` and an optional nonnegative `weight`
(default 1). Point times lie within the event and increase strictly; frequencies
lie within its band. Times are relative to the beginning of the decoded recording,
not to the beginning of an analysis chunk. Chunked discovery must add its offset.

Optional `harmonic_group_id` is the canonical acoustic proposal key. Related
measured components contribute once as an episode, with physical source
multiplicity unresolved; a group does not establish a flyover or source motion.
Harmonics must not become independent time constraints. Optional
`cpa_receive_s` is an estimated received CPA time, in audio-relative seconds, and
may lie outside a truncated observed event. Optional positive increasing ranges
`f0_range_hz`, `speed_range_mps` and `slant_range_m` are measured proposal bounds.
They are not guaranteed true geometry; the matcher must account for uncertainty.

`quality` maps diagnostic names to finite numbers. Name their units where relevant
(e.g. `ridge_rmse_hz`). A diagnostic score is not a calibrated probability.
There is no blanket `confidence` float whose semantics different workers can guess.

Default discovery retains its existing artifact, which has no explicit
`schema_version` field. The shared event contract remains version 1. Only the explicit
`discover --experimental-episodes` path writes `schema_version: 2` with an
`episodes` catalog that references compatible `events` children. The checkout
scanner also requires this switch. See [outputs and
compatibility](AUDIO_DISCOVERY.md#outputs-and-compatibility).

Experimental children carry `quality.episode_model_version=2`, positive
`quality.episode_frequency_scale`, and numeric `quality.matching_eligible`.
Matching normalizes these measured scales within each canonical group and retains
legacy rational-ratio behavior for version-1 events. Brief episodes with
insufficient samples remain evidence for acoustic review. These numeric quality
fields preserve DetectedEvent's strict keys and the upload/result schema version.
Automatic analysis and upload jobs do not select the experimental implementation.

## Location candidates

A `LocationCandidate` contains `lat`, `lon`, finite `score`, a descriptive
`score_kind`, paired `event_ids`/`flight_ids`, and fitted `recording_start_utc`.
Flight IDs identify imported trajectory segments. Event IDs are distinct; the same
flight may legitimately have multiple approaches. Every event gets one flight
segment at that candidate, and all share the same fitted recording start.

Scores are **higher is better**. Existing brightness scores can use
`spectral_contrast`; existing negative ridge residuals can use
`negative_ridge_rmse_hz`. Candidate lists use one comparable score kind and descend
by score. The tiled search must preserve the same scoring normalization across
tiles. Do not compare unlike statistics without a documented common score.

Optional `uncertainty` contains `radius_m`, `method` and `kind`. `sensitivity` is
an uncalibrated model/measurement sensitivity envelope. It cannot carry a coverage
probability. `calibrated` additionally requires `coverage_probability` in [0, 1]
and a `calibration_reference` to independent evaluation evidence. Providing these
fields does not itself establish that the claimed calibration is valid.

## Jobs, decisions and artifacts

[Example result](examples/job-result.json) and
[missing-time example](examples/job-needs-input.json) are illustrative wire data,
not measured benchmark outcomes.

`JobResult` requires `schema_version: 1`, `job_id` and one status:

| Status | Meaning |
| --- | --- |
| `queued` | Request accepted; analysis not started |
| `running` | Work is in progress |
| `needs_input` | Explicit user information is required; include required_fields |
| `succeeded` | A location is supported by the decision stage; candidates required |
| `inconclusive` | Processing completed but evidence does not support a location |
| `failed` | An execution, input-processing or data-access error prevented completion |
| `cancelled` | Requested cancellation completed |

`needs_input`, `inconclusive` and `failed` require a stable `reason_code` and a
human-readable `message`. Suggested codes include `recording_time_required`,
`no_supported_events`, `insufficient_coverage`, `ambiguous_locations`,
`decode_failed` and `data_unavailable`. Reason codes may expand without a schema
change. An unavailable provider and a noise-only recording are different outcomes.

Optional `stage` is a producer-chosen stable string, e.g. `intake`, `discovery`,
`acquisition`, `search`, `refinement`, `decision`, `complete`. Optional `progress`
is a measured/estimated fraction in [0, 1]; omit it when the total work is unknown.
Optional `recording`, `events` and `candidates` carry the objects above. Event IDs
must be unique. If events are included, candidate event references must resolve.
An inconclusive job may retain ranked alternatives for diagnostics; its status
must prevent the UI from presenting them as an accepted location.

Optional `coverage` has `search_complete` (boolean), `data_status` (`available`,
`partial`, `unavailable`), and a list of known `limitations`. Optional
`requested_area` uses the request SearchArea shape; optional `missing_data` lists
known missing provider/date/tile identifiers. Search completion means the algorithm
finished the requested scope. Data availability means requested provider inputs
were obtained; it does not assert that every real aircraft broadcast or was captured.
Document that limitation even when all archive files were downloaded. A missing
coverage report must never be interpreted as complete coverage by the decision
stage. Detailed tile counts and coverage maps belong in a linked artifact.

Automatic jobs omit `coverage` during acquisition and search until a coverage
assessment is available. Local indexes and initialized terrain providers do not
establish complete coverage. Interrupted jobs can also omit `coverage` when no
assessment is available.

`artifacts` maps names to paths relative to that job's output directory, without
absolute paths or `..` traversal. Suggested names are `report`, `provenance`, and
event-specific plots/arrays. The provenance artifact should record input/data/code
hashes, configuration, time/altitude assumptions, searched/missing coverage and
runtime. The service must independently enforce output-directory containment when
serving files, including symlink handling. Do not embed huge spectra in job JSON.

The optional read-only `GET /api/jobs/JOB_ID/guidance` endpoint derives next steps
from the saved result and matching decision artifacts. It has its own additive
envelope; it does not extend or rewrite `JobResult`. Recommendations include
artifact-relative evidence pointers and an explicit unmeasured-benefit label.
See [evidence-based next steps](INCONCLUSIVE_GUIDANCE.md) for the representation,
replay commands and unresolved verification requirements.

## Compatibility and ownership

The existing `dopplergeist run CONFIG --output DIR` and `measure` commands keep
their current behavior. `event_measure_settings(event)` maps a discovered event to
the existing measurement-setting field names. It deliberately does not pretend to
select flights or invent missing event geometry. Re-measuring a discovered window
through the legacy API is optional; DG-09 can pass measured ridges directly to an
adapted search path.

For a legacy replay, a point time window can populate `recording_start_utc`.
An interval must be searched as an interval; do not substitute its midpoint as a
known clock. DG-05 selects sufficient trajectory support around all possible
recording starts and events, including propagation/interpolation margins.

DG-03 produces recording metadata. DG-04 produces events. DG-05 supplies flight
and terrain data/provenance. DG-06/DG-07 produce compatible ranked candidates.
DG-08 decides whether a location is supported. DG-09 assembles jobs; DG-10 consumes
them. Preserve these shared shapes and coordinate extensions before merging.
