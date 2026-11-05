# Automatic audio-event discovery

`dopplergeist discover input.mp3 --output results/new-audio-scan` scans the complete recording for acoustic moving-tone proposals. Automatic analysis uses the same discovery path. No event windows, frequency bands, aircraft identities or location hints are required.

Intake decodes the original audio at 4 kHz and 24 kHz. Discovery combines ridge tracking and time-frequency sequence analysis, verifies measured tracks, and groups related tracks into proposals. The lower-rate ridge finder remains an internal component of this detector. The two decodes are checked against the same source hash.

`discovery/discovery.json` contains the input hash, detector settings, canonical proposal intervals and measured child tracks. Tracks belonging to a proposal share a `harmonic_group_id`; downstream association should count the group once while retaining its measured tracks. Times are relative to the first decoded sample. `--plot-summary` renders a track overview and `--plot-event-details` renders individual track plots. Use a new output directory for each run.

Discovery uses fixed settings. A track is acoustic evidence, not proof that the source is an aircraft or that the pitch change was caused by physical motion. Missed, fragmented, merged and spurious proposals remain possible. Runtime, memory and disk use depend on recording length and content. Historical results from earlier discovery versions do not establish this detector's general performance or location accuracy.

[Earlier discovery documentation and historical results](archive/AUDIO_DISCOVERY_LEGACY.md) are preserved separately.
