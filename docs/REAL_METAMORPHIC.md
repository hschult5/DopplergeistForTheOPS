# Real recording representation checks

Issue [#61](https://github.com/1337isnot1337/DOPPLERGEIST/issues/61) requires fresh
end-to-end runs for equivalent representations of actual recordings.
The runner in `scripts/run_real_metamorphic.py` executes these runs through the
local HTTP upload service. It accepts a recorded source, a recording-start UTC
window, and an offline operator configuration.

Each arm starts a new service and job. Intake, discovery, selection, matching,
search, refinement, decision and serialization run from the uploaded bytes.
The runner shares only frozen historical indexes and terrain. It does not
resume audio or search checkpoints. The HTTP verifier checks the actual stage
artifacts and the persisted terminal result.

## Frozen protocol

The versioned protocol is
[`verification/metamorphic_protocol_v1.json`](verification/metamorphic_protocol_v1.json).
It was committed locally before the development matrix ran. The exact executed
source tree was published afterward as
[`3094292`](https://github.com/1337isnot1337/DOPPLERGEIST/commit/309429278978adef11e29d2bdfad296dbfdf34e1).
This was not a public preregistration. The protocol has nine sequential arms:

| Arm | Change from the baseline |
| --- | --- |
| Baseline | MP3, 128 kb/s, 48 kHz, mono |
| Low bitrate | 64 kb/s |
| High bitrate | 192 kb/s |
| Sample rate | 44.1 kHz |
| Gain | −6 dB |
| Channels | Two identical channels |
| Leading silence | 17.25 seconds |
| Short discovery chunks | 60-second chunks, 30-second overlap |
| Long discovery chunks | 180-second chunks, 60-second overlap |

All encodings start from the complete source recording's arithmetic channel
mean at 48 kHz. No arm is encoded from another MP3 arm. The baseline keeps the
operator's discovery settings. The current defaults are 120/60 seconds.

Only `operator.discovery.chunk_s` and `overlap_s` are exposed chunk controls.
The protocol explicitly lists unsupported upload, decoder and detector controls.
It does not insert hooks into the source algorithms.

Each job has a 240-second runtime limit and a 300-second client deadline.
The encoding/decode command limit is 300 seconds. Pipeline time is at most
45 minutes across nine client deadlines, plus preparation and cleanup.
Different budgets can be declared at preparation, before any outcomes exist.

Tolerances are fixed in the protocol: 2 ms for duration and residual alignment,
1 second for mapped event edges and fitted clocks, 2 Hz for median ridge
frequency, and 0.05 per matched group for comparable normalized scores.
Usability also requires nonclipping decoded peaks and gain-corrected PCM
NRMSE at most 0.15. Group counts, flight associations and score units must agree.
The runner reports displacement thresholds at 100, 500 and 1,000 metres.
Accepted locations that move over 1,000 metres receive a separate serious flag.

The PCM calculation reads bounded blocks. Lag checks use twelve fixed short
windows, not full-hour correlations. A measured lag is reported; it does not
shift samples to improve the result. Native-rate decoded peaks and identical
stereo channels are checked separately from the 4 kHz analysis PCM.

## Clock mapping

If leading silence is Δ seconds, subtract Δ from **both** sample-zero UTC
window endpoints. Subtract Δ from detected audio-relative times for comparison.
Add Δ to the fitted sample-zero UTC to recover the original recording clock.

For example, 20:17:20–20:17:40 UTC becomes 20:17:02.750–20:17:22.750 UTC
after 17.25 seconds of padding. The physical time of each recorded sound stays
the same. The runner retains codec skip/discard metadata, decoded sample counts,
and residual lag checks. Container start time is not a recording clock.

## Run

Use an installed checkout with FFmpeg and the repository's dependencies.
The request must have a defensible time window. Use absolute local paths for
historical indexes and terrain, or run preparation where relative paths resolve.
Use a stopped, immutable data cache. Active job directories are not data inputs.

```bash
PYTHONPATH=src python scripts/run_real_metamorphic.py prepare \
  --audio /absolute/path/actual-recording.wav \
  --request /absolute/path/request.json \
  --operator /absolute/path/operator.json \
  --source-note 'Identify the actual recorded source and its permission/provenance' \
  --scope-note 'Describe the search scope, clock evidence and evaluation limits' \
  --output /absolute/path/new-frozen-plan

PYTHONPATH=src python scripts/run_real_metamorphic.py run \
  --plan /absolute/path/new-frozen-plan/plan.json \
  --output /absolute/path/new-matrix-run
```

Preparation writes an immutable plan before encoding. It pins source bytes,
configuration, historical inputs, terrain, source code, native binaries,
dependencies, backend environment and FFmpeg version. Runs refuse changed
inputs. Use `--frozen-input PATH` for an additional immutable local dependency.
Every output directory must be new.

New plans also record Shapely and its GEOS runtime version. Package absence is
recorded explicitly. If Shapely is installed without an identifiable
GEOS version, preparation refuses the incomplete runtime pin.

The runner preserves encoded inputs, exact transformation commands, actual
job artifacts and observations for every arm. It continues after an ordinary
arm failure. It preserves pending arms on interruption or changed inputs.

Pairwise comparisons also fail if the material coverage state (search completion,
data availability, missing-data identifiers or requested area) changes, or if
verified pipeline/work-completion gates change. Measured elapsed time, peak memory
and event proposal counts remain reported but are not themselves completeness
failures. Raw coverage descriptions are retained separately. This check applies
to newly generated reports; the recorded November 4 results are immutable.
An unusable encoded arm still reaches the HTTP pipeline. Encoding failures
remain explicit failures.

`report.json` contains all pairwise comparisons. `report.md` gives a short
case table. Event matching uses a one-to-one assignment based on mapped time
overlap and ridge frequency. It reports unmatched ridges/groups, group splits,
group merges, and ambiguous alternatives. IDs and row positions do not define
correspondence. Retained distant modes are compared separately.

Each case includes a one-arm reproducer command. The `--case` option creates
a fresh job and records the other arms as not selected. It cannot pass the
full-matrix requirement. The `report` command compares preserved observations;
it never counts as another end-to-end run.

Exit zero requires all nine usable arms to reach the full pipeline and remain
within the declared comparison tolerances. Exit one preserves incomplete runs
or observed differences. An inconclusive scientific decision is not itself a
runner failure when the complete pipeline ran. It remains an inconclusive
location result.

The full-path count uses the verifier's stronger requirement: every stage must
be verified and at least one diagnostic candidate must survive. Empty outcomes
remain in the report. A separate count records all observed stage sequences.
Neither count requires a supported location decision.

## Scope of the development run

The affordable development control uses the whole released reference recording,
a source-informed regional box, a narrow source-informed UTC window, and
incomplete historical flight coverage. It cannot satisfy national acceptance.
It does not establish independent location accuracy.

No eligible independent truth is supplied in this protocol. Truth errors at
100/500/1,000 metres remain explicitly unevaluated. Pairwise displacement is
reported separately. A future eligible national evaluation needs its own
predeclared request, data, budgets and evaluation-only truth protocol.

Focused contract checks use archived real ridge observations and an optional
short actual recording. They do not replace any full-recording arm:

```bash
DOPPLERGEIST_REAL_METAMORPHIC_AUDIO=/absolute/path/actual-short-recording.mp3 \
  PYTHONPATH=src pytest -q tests/test_real_metamorphic_runner.py
```

## Recorded result: 4 November 2026

All nine fresh jobs passed HTTP and persistence verification and completed the
required pipeline. Each produced 23 diagnostic candidates. Every scientific
decision remained `inconclusive / search_incomplete`. The fixed comparison
criteria failed in 35 of 36 pairs, so the runner correctly exited with code 1.
The 192 kb/s and identical-stereo pair had no comparison flags.

The [measured summary](verification/real_metamorphic_reference_2026-11-04.json)
records all nine outcomes, all 36 comparison flag sets, source and input hashes,
configuration, runtime pins and raw-artifact identities. The full run took
1,192.83 seconds, including encoding and preflight checks. HTTP verification
accounted for 737.92 seconds.

The table compares each arm's leading diagnostic candidate with the declared
128 kb/s baseline. These are candidate movements, not accepted-location errors.
Clock differences are absolute differences after the prescribed mapping for
leading silence; they do not indicate which fitted clock is later.

| Arm | Groups / ridges | Displacement (m) | Absolute clock difference (s) | HTTP time (s) | Worker high-water RSS (MiB) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline | 15 / 17 | 0.00 | 0.000000 | 81.16 | 409.34 |
| 64 kb/s | 16 / 18 | 177.95 | 0.474851 | 101.03 | 430.97 |
| 192 kb/s | 16 / 18 | 3.09 | 0.005837 | 93.50 | 410.20 |
| 44.1 kHz | 16 / 18 | 1,969.93 | 7.452914 | 102.04 | 452.91 |
| −6 dB | 15 / 17 | 2.67 | 0.005770 | 80.67 | 431.94 |
| Identical stereo | 16 / 18 | 1.61 | 0.005285 | 90.59 | 413.76 |
| 17.25-second leading silence | 13 / 16 | 120.24 | 0.213660 | 90.73 | 465.77 |
| 60/30-second discovery chunks | 11 / 13 | 31.78 | 0.065637 | 48.30 | 408.05 |
| 180/60-second discovery chunks | 13 / 15 | 30.88 | 0.053637 | 49.91 | 436.25 |

All nine representations passed the declared usability checks. Duration errors
and measured residual lags were zero. Gain-corrected PCM NRMSE ranged from
0.03011 to 0.05248, below the fixed 0.15 limit. Native decoded peaks stayed
below clipping. These global checks do not prove preservation of every weak
acoustic cue.

Both chunk variants uploaded bytes identical to the baseline. Only the actual
discovery chunk settings changed. The resulting group and ridge counts changed
from 15/17 to 11/13 and 13/15. This is a direct processing-chunk sensitivity
result for the declared recording and search configuration.

The 44.1 kHz arm lost the baseline's 1,396.5–1,418.5-second proposal in the
75.49–93.48 Hz band. All 23 baseline candidates used that proposal, associated
with flight `aa61d2:3`. Its ordinal event ID in the variant belongs to a different
ridge, so it cannot establish correspondence.

A five-group remnant survives at variant public rank 11 and retained-mode
rank 8. Its coordinates and mapped clock exactly equal the baseline's unrefined
parent at public rank 3. It lies 284.63 metres from the refined baseline winner,
with an absolute clock difference of 0.494912 seconds. The 1.97 km winner movement accompanies
changed detected groups and associations. It does not demonstrate accidental
deletion or reordering of an identical hypothesis.

The report retains every `distant_mode_lost` flag. This flag means that strict
full-association correspondence failed. A changed proposal population can cause
that failure without proving that the same hypothesis was pruned. Score deltas
remain incomparable when the mapped associations differ.

No arm produced an accepted location. Consequently, zero accepted-location pairs
were evaluated; accepted-location invariance did not pass or fail. Independent
truth errors remain unevaluated. The regional, source-informed inputs and
incomplete historical coverage leave issue #61's national gate open.

### Memory instrumentation correction

The original descendant-RSS sampler could not traverse this environment's
`/proc` mount. All nine original aggregate readings remain unavailable. The
table preserves the worker high-water measurements recorded at search
completion. They are not certified peaks for the whole job.

The corrected sampler resolves the PID namespace exposed by `/proc/self`.
When thread `children` files are absent, it selects descendants through the
same mount's parent-PID relations before reading their RSS. Sampling errors and
stop timeouts remain explicit. Measurements also survive HTTP verification
exceptions.

A separate four-second actual upload verified this correction. It measured
129.74 MiB across 16 samples, stopped normally, and returned
`needs_input / recording_time_required`. That run validates instrumentation
only; it is not a tenth scientific arm. No original matrix measurement was
replaced, and the nine-arm matrix was not rerun. Descendant RSS is a sampled
sum and can count shared pages more than once.
