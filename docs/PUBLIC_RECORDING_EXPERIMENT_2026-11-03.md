# Public field recordings: regional end-to-end tests

Five real, ten-minute recordings from the public Eclipse Soundscapes archive were
submitted as MP3s to the local HTTP pipeline on source `611dca2`. Three produced
rankings, but **none of their 105 retained rows lay within 1 km of the
publisher's site**. The other two produced no candidate. These are negative or
inconclusive development tests, not confirmed aircraft-localization benchmarks:
the audio proposals have not been independently identified as aircraft, and the
individual clock and microphone-position errors are unknown.

| Recording | UTC filename start | Groups | Tiles visited | Outcome | Top distance | Closest retained |
| --- | --- | ---: | ---: | --- | ---: | ---: |
| [ESID 920, Rochester area](https://zenodo.org/records/22014438) | 02:41:49 | 6 | 16/16 | 105 rows; `search_incomplete` | 14.06 km | 13.48 km, rank 101 |
| [ESID 430, Fort Worth area](https://zenodo.org/records/21772857) | 05:27:00 | 5 | 12/12 | 105 rows; `source_drift_unresolved` | 12.38 km | 12.38 km, rank 1 |
| [ESID 439, east Texas](https://zenodo.org/records/21804576) | 02:09:50 | 6 | 12/12 | 105 rows; `search_incomplete` | 22.27 km | 19.41 km, rank 101 |
| [ESID 443, Denton area](https://zenodo.org/records/21755346) | 04:53:15 | 1 | 9/9 | No ranked candidates; `search_incomplete` | — | — |
| [ESID 923, Charlottesville area](https://zenodo.org/records/21918793) | 08:39:17 | 4 | 12/12 | No matching flights | — | — |

All dates are 8 April 2024. Groups are detector harmonic-group IDs, not proven
independent aircraft. Distances are WGS84 distances to the site coordinates
reported by the publisher, not measured microphone errors. The audio comes from
the first 600 seconds of each UTC-named WAV and was encoded to MP3. The
publisher describes the filenames as UTC and reports the AudioMoth time-chime
setup; neither establishes the actual clock error at these sites. The ±20-second
clock windows below are **assumptions**, not verified uncertainty bounds.

The experiments supplied approximate regional boxes selected with knowledge of
the published sites, so they are **not blind location tests**. They did not supply
flight IDs, event windows, frequency bands or exact coordinates to the search.
They used the ADSB.lol all-aircraft 2024-04-08 archive index (including unknown
types), Copernicus GLO-90 terrain, the EGM2008 geoid, 1,500 m coarse spacing,
150 m fine spacing, and the full automatically discovered events. Archive scope
is not proof that every real aircraft was received. The detector's proposal and
an aircraft fit do not themselves establish the physical source of a sound.

## Rankings and search limits

Per-candidate ranking tables and the Rochester candidate map are retained locally;
they contain run-specific locations, clock values and flight/event identifiers.

Scores are fit statistics, not probabilities; repeated or nearby rows need not
be independent locations. Rochester's first five geographically separated
candidates (500 m separation) occur at original ranks 1, 2, 6, 14 and 20, with
site distances 14.06, 13.88, 13.96, 14.27 and 13.74 km respectively. The
Rochester winner matched one detector group; the remaining groups were unmatched.
The output also contains unknown model evaluations: 50 unsupported CPA work
evaluations, 87 unclassified candidate evaluations and 515 unknown clock trials.
Completed tiles therefore do not imply complete physical-model assessment.

**Historical bounds defect:** five Rochester and five east-Texas final rows,
including each rank-one result, are outside their requested latitude/longitude
search boxes. These runs used commit `611dca2`, whose finalist refinement could
leave the requested box. Commit [`ae7721b` (#270)](https://github.com/1337isnot1337/DOPPLERGEIST/commit/ae7721b)
fixed this by constraining continuous refinement to the geographic bounds; that
fix is present in current `main`. The saved results remain unaltered and are not
recomputed here. No result in these three runs lies within 1 km of its publisher
site, so the bounds fix does not explain or resolve the large location errors.
The initial Denton run also lacked terrain at one edge; a fresh run with both DEM
tiles produced the row in the table.

## Post-hoc diagnosis of the three large misses

This section inspects only artifacts from the completed real-recording runs
above; it does not rerun discovery or localization and does not feed site
coordinates back into a search.

- **Rochester:** six proposal groups were discovered, but the leading location
  hypothesis matches only group 6; five groups are unmatched. Its two reported
  ridge IDs are harmonics within that one group, not two independent flyovers.
  The finite-search assessment is incomplete, with 515 unknown clock trials and
  87 unclassified score candidates. One matched flyover cannot locate the
  recorder, so the `search_incomplete` status is appropriate.
- **Fort Worth:** the leading hypothesis matches three of five proposal groups;
  groups 3 and 4 are unmatched. Groups 1 and 2 both map to the same archived
  aircraft segment, so the three matched groups do not establish three
  independent flyovers. The source-frequency drift fit reaches its allowed bound
  for both of those groups, which is why the result is
  `source_drift_unresolved` even though the finite grid assessment completed.
- **East Texas:** six proposal groups were discovered, but the leading
  hypothesis matches only groups 4 and 5; four are unmatched. The saved search
  has 999,703 unknown clock trials and 31,333 unclassified score candidates, so
  the assessment is incomplete. Its `search_incomplete` status is appropriate.

### Audit of the saved retention ledgers

These runs predate the fix for a possible corrupt `mode_index` secondary
index. To check whether that defect affected these exact results, the saved
Rochester, Fort Worth and East Texas SQLite ledgers were opened immutable and
read-only. `PRAGMA quick_check` returned `ok` for all three. The regular
`GROUP BY mode_id` aggregation used `mode_index`; its distinct-mode counts and
per-mode maximum scores matched exactly with the same aggregation forced to
scan the `records` table using `NOT INDEXED`:

| Recording | Distinct modes | Indexed/table-scan mismatches |
| --- | ---: | ---: |
| Rochester | 724 | 0 |
| Fort Worth | 4,243 | 0 |
| East Texas | 2,901 | 0 |

The retention-index corruption found elsewhere therefore was not observed in
these persisted ledgers and does not explain these rankings. This check does
not establish that the candidate ranking matches the real aircraft or that the
incomplete searches assessed every alternative.

The five unique flight segments named by the three leading candidates were
also looked up in the same cached 8 April 2024 flight index. Current main's
`Track.validate_motion_support(343.0)` accepted all five, with sample counts
16, 66, 147, 209 and 335; none triggered the non-subsonic-motion rejection.
This rules out that specific current model-support failure for those retrieved
segments. It does not establish that the older run used identical interpolation
or that any segment is the source of its acoustic proposal; no receiver-position
fit was evaluated.

The saved unmatched proposals also do not all have obviously weak detector
heuristics: across individual ridges in those groups, `median_contrast` ranges
from 3.14 to 5.25 and `support_fraction` from 0.805 to 1.000. These fields are
not calibrated measures of aircraft identity, so they cannot distinguish real
flyovers from false proposals. The saved evidence does not justify discarding
all unmatched groups with a higher detector threshold.

Across all three, event groups are acoustic proposals rather than verified
aircraft identities. The ±20-second recording-start windows were assumptions
based on filenames, and the archive does not establish complete local receiver
coverage. These limitations leave plausible tracks and clock/location tradeoffs
unresolved. The displayed leaders are diagnostic rankings, not supported fixes;
increasing the refinement budget or removing the bounds would not repair the
missing independent acoustic evidence.

### CPA support scope follow-up

Read-only analysis of the saved 2024-04-08 support artifacts found a mismatch
between CPA time filtering and the non-subsonic-track support proof. The bounded
search cropped CPA rows to event receive times, while the fallback proof examined
bad-motion intervals across each complete track. Using the saved run configuration
and track time ranges with the production receive-window calculation, 45 of 46
unique Rochester bad-motion intervals and both East Texas intervals have no
affected CPA sample edge in the query window; one Rochester interval remains
in-window. Fort Worth had no unsupported CPA track intervals.

The follow-up code change passes the same bounded window into the CPA support
proof. It skips only intervals whose affected sample edges fall outside the
actual CPA slice; any interval that can contribute an edge still requires the
existing spatial exclusion proof. Direct acoustic support checks remain
time-local. The saved jobs were not rerun or rewritten, and this correction does
not establish that a location estimate is accurate. Fort Worth remains
source_drift_unresolved; all three old decisions remain inconclusive.

## Reproduction

The source records are linked above. The field collector in
[`collect_eclipse_audio.py`](../scripts/collect_eclipse_audio.py) extracts the
first 600 seconds of a UTC-named WAV and encodes MP3. The ten-minute local MP3s,
full job results and operator configurations used here remain under
`/home/cinna/.local/state/dopplergeist-eclipse-corpus-20261103/` and
`/home/cinna/.local/state/dopplergeist-public-eclipse-run-20261103/`. Original
media are not redistributed in this repository.

With an acquired MP3, a locally built all-aircraft index, terrain and geoid, run:

```bash
PYTHONPATH=src python scripts/run_public_eclipse_case.py \
  --audio /path/to/esid-920.mp3 --operator-config /path/to/operator.json \
  --output /path/to/new-output \
  --start-utc 2024-04-08T02:41:29Z --end-utc 2024-04-08T02:42:09Z \
  --bounds -77.90 43.08 -77.50 43.30
```

The saved operator configuration uses `flight_indexes`, `terrain.rasters`,
`terrain.geoid_raster`, `search_workers: 3`, and `search_budget` with
`spacing_m: 1500`, `tile_cells: 8`, `top_k: 100`, `refine_spacing_m: 150`,
`refine_finalists: 5`. The second and third ranked runs used two workers. The
full retained ranking can be exported after the job with
[`export_ranked_job.py`](../scripts/export_ranked_job.py); the reference coordinate
is a post-search comparison. The figure is generated by
[`render_public_eclipse_case.py`](../scripts/render_public_eclipse_case.py) from
the saved recording, result, candidate CSV and Esri World Imagery export. Esri
and contributors retain the rights to the basemap; see
[Esri's static-map attribution terms](https://doc.arcgis.com/en/arcgis-online/reference/static-maps.htm).
