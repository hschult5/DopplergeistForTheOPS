> Historical documentation: this describes earlier discovery implementations and obsolete command-line options. It does not describe the current default.

# Automatic audio-event discovery

`discover_events(recording, output_dir=None, settings=None)` consumes the
`IntakeRecording` from intake and returns shared-contract `DetectedEvent`
dictionaries. The normal call supplies no time windows, frequency bands, aircraft
IDs, search locations or truth. It scans all decoded samples with fixed 120 s
chunks / 60 s overlap, a 2 s Hann spectrum at 0.5 s hops and 40–1600 Hz coverage
(clamped below decoded Nyquist). Working memory is bounded by chunk size; a disk
cache holds the complete recording. Inputs under one spectral window return [].

Time-whitened spectral contrast suppresses persistent stationary tones. Local
maxima are linked with bounded motion, short missing spans and local slope
continuity. Minimum duration/support, resolved downward frequency change,
distributed motion and jump checks reject noise paths and switching stationary
tones. Duplicate overlapping chunk ridges merge before grouping harmonics. Both
integer ratios and rational ratios (e.g. measured 2f and 3f with an absent
fundamental) count as one independent event. The lowest measured harmonic is the
representative ridge; no fundamental is invented. Harmonic count denotes distinct
measured ratios, including fragmented traces of the same harmonic only once.
Harmonic grouping is optional consolidation: a ridge without a compatible
partner remains a single-ridge event candidate. The detector does not require a
second harmonic.

The output preserves recording-relative ridge samples and weights, measured
window/band, harmonic-group identity, contrast/support, spectral resolution and
uncertainty diagnostics. Resolution is not a calibrated error interval; no raw
quality score is a probability. No compulsory straight-flight speed, slant or
source-frequency estimate constrains downstream search. These are **acoustic
moving-tone proposals**, with aircraft identity unconfirmed. Association must
reject unsupported proposals and count harmonic groups once.

In the current real one-hour upload, automatic discovery produced 15 independent
groups: 14 have one observed ridge and one has three. This is a useful reminder
that harmonic grouping can consolidate evidence when multiple bands are
recorded, but a single-ridge candidate must remain eligible for downstream
assessment. The audio-only discovery result does not confirm aircraft identity
or recording location.

With output_dir, discovery saves `discovery.json` (all events, settings, chunk
counts, original harmonic ridges and input hash), `discovery.png` (overview), and
per-event CSV plus spectrogram PNG. Each spectrogram image is limited to a chunk's
span; the full measured ridge remains in JSON/CSV. Existing discovery outputs
are refused. A reproducible whole-file entry point is:

```bash
dopplergeist discover input.mp3 --output results/new-audio-scan
```

The installed CLI records the source hash, settings, measured events and chunk
counts in its output. It does not consume evaluation labels. Run post-hoc
evaluation in a separate process. Use a new output directory for every run.
Pass `--plot-summary` for the overview or `--plot-event-details` for individual
spectrograms; plotting is off by default in the CLI.
The checkout-only `scripts/discover_audio.py` additionally records its Git state,
code hash, runtime and peak RSS for research comparisons.

Development evidence before DG-02 controls: full synthetic WAV (1180 s), 2
proposals spanning both planted passes, 7.24 s / 238044 KiB peak RSS; full public
reference WAV (3600.1845 s after decode), 8 proposals, 31.05 s / 249528 KiB peak
RSS. The two prominent known passes are covered, but are fragmented into multiple
proposals; the documented faint pass is missed. Six proposals do not cover either documented pass center; some appear to be
fragments of the same pass. Their identities remain unverified. They are not
labelled aircraft or certified false alarms. No
complete real event annotation exists to calculate reference precision/recall.
Earlier v1/v2 diagnostics preserve a missed synthetic event and excess proposals.
These are development checks, not independent location/nationwide validation.

Known limits: fixed spectral scale and minimum duration can miss steep/short,
faint, low-frequency or high-frequency events. A single wide ridge can fragment
across missing spans, and acoustically similar harmonic sources can be grouped
ambiguously. No algorithm can establish aircraft identity solely from an arbitrary
matching synthetic chirp. The existing real recording has no independently
verified UTC clock. This implementation does not establish 100 m localization.

Focused tests: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_discovery.py`.
They cover complete-file seams, harmonic grouping including an absent fundamental,
simultaneous unrelated events, noise/stationary/upward-sweep rejection, short
unsupported clips, and crossfading stationary tones. Fixed-setting DG-02 corpus
results are recorded separately in the audio evidence handoff.

Fixed-code DG-02 control check at discovery commit `4a49646`: all 14 opaque-input
cases processed, 8 planted events found, 0 misses and 0 false alarms at event
IoU >= 0.3. Both the development and public deterministic held_out split find
4/4 events. Noise, stationary, upward electronic sweep and short clips produce
no event; harmonic, overlapping and MP3 positives are retained. Detection ran
through the label-free subprocess runner; a later process read truth for scoring.
No detector/settings changes followed the control run. These controls have no
eligible location cases and do not establish real-aircraft identity or accuracy.

Additional unchanged-code public-audio check: all 40 complete passage WAVs process
without errors; 28 have at least one proposal, 12 have none, 102 proposals total.
The run never supplied their frequency annotations. These counts do not establish
true aircraft-event recall/precision: fragments and incidental sources remain
unverified. Every prediction is saved under evidence/audio/w1-20261102/public40-4a49646.

Fragment follow-up: compatible disjoint ridge fragments can join across at most
20 s when the gap is at most 35% of the combined observed span, both endpoint
slopes descend, the observed frequency drop/rate is compatible with those slopes,
and each endpoint has only one compatible continuation. Competing continuations
remain separate. No frequency samples are invented across missing intervals;
harmonic comparison excludes unsupported spans. `fragment_count` and
`max_observed_gap_s` retain missing-support evidence, and `fragment_links` records
accepted/ambiguous continuations with reasons. This is a tonal continuity
hypothesis; it does not prove a flight identity.

`quality.frequency_sigma_hz` is the integration consumer's canonical robust-loss
scale, with `frequency_uncertainty_hz` retained for compatibility. Both currently
carry the same resolution/robust-deviation diagnostic, with no statistical or
probability calibration implied by the field name. Matching applies its >=1 Hz
loss-scale floor. Later tests against the already public DG-02 controls are
regressions, not newly held-out accuracy evidence.

Sensitivity follow-up lowers the global seed contrast threshold from3.0 to2.5.
At this setting the full synthetic recording still yields2 groups, with first
ridge support extended from143–180.5s to119–196s. The reference emits16 proposals,
including a short1265.5–1282s ridge in the documented faint passage; post-run
comparison with the archived manual ridge gives median absolute difference
0.692Hz over23 points (90th percentile2.139Hz). This is acoustic overlap with an
existing development measurement, not independently known aircraft or location
truth. Fourteen known synthetic controls retain8 hits/0 misses/0 false alarms.
The extra unverified reference proposals remain a matching/search cost. Earlier
3.0-threshold evidence and the faint-pass miss stay preserved; no held-out or
100m localization claim follows from this development tuning.

Discovery returns all accepted independent groups; it has no global two- or
three-event maximum. Four- and six-episode generated audio regressions check that
later events survive and harmonics count once. These audio-only controls test
retention, not geographic accuracy. The per-frame peak budget remains an explicit
bounded-analysis setting, and unsupported or missed tones are not a completeness
guarantee. The contrast 2.5 settings are frozen for the new public-camera batch.

## Experimental acoustic episodes

The installed `discover` command and the checkout script support an explicit
`--experimental-episodes` switch. This switch selects
`dopplergeist.episode_discovery` for acoustic reports. Without the switch, both
commands retain the scanner described above. The automatic analysis and upload
workflow also retain that scanner.

Run the experimental scanner on a real recording:

```bash
dopplergeist discover input.mp3 --output results/new-episode-scan \
  --experimental-episodes --plot-summary
```

Use a new output directory for each run. For a wider frequency range, run:

```bash
dopplergeist discover input.mp3 --output results/new-wide-episode-scan \
  --experimental-episodes --max-frequency-hz 6000 --plot-summary
```

The experimental settings default to 40–1600 Hz, a 2-second spectral window,
and a 0.5-second hop. The commands accept `--min-frequency-hz`,
`--max-frequency-hz`, `--window-s`, `--hop-s`, and `--detailed-candidates` only
with `--experimental-episodes`. The checkout script accepts the same switch:

```bash
PYTHONPATH=src python scripts/discover_audio.py input.mp3 \
  --output results/new-research-episode-scan --experimental-episodes
```

Experimental intake selects a decoding rate that supports the requested band.
It explicitly uses the FFmpeg resampler with a 64-tap filter and a 0.95 cutoff.
The default scanner retains its existing decoding command. Unsupported source
coverage and analysis limits produce explicit errors.

### Outputs and compatibility

Default discovery retains its existing artifact, which has no explicit
`schema_version` field. Experimental discovery writes
`discovery/discovery.json` with `schema_version: 2`, a canonical `episodes`
catalog, and measured `events` children. Episode records reference their measured
children. Gaps remain gaps, and an inferred fundamental does not create measured
ridge samples. Coverage, settings, counts, rejection diagnostics, and code identity
remain available in the artifact.

The child events retain the shared `DetectedEvent` contract. Numeric quality
fields include `episode_model_version`, `episode_frequency_scale`, and
`matching_eligible`. The matching adapter supports these recorded children and
retains its legacy behavior for version-1 events. Brief episodes without enough
samples remain evidence for acoustic review. The experimental command does not
run a location search or enable these episodes in automatic upload jobs.

The [frozen real-recording comparison](ACOUSTIC_EPISODES_REPORT.md) retains every
input, including zeros and regressions. Its labels `new` and `default` refer to
the experimental implementation with its original settings. They do not name
the current product default. Six baseline reference groups had no retained
measurement within the report's overlap tolerance, including the documented faint
passage. Runtime also increased on the measured
comparison. The evidence does not establish improved aircraft recall, source
identity, or location accuracy.
