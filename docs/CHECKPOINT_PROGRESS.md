# Read saved checkpoint progress

`scripts/report_checkpoint_progress.py` reports recorded coarse search work.
It does not resume the search or change its scoring, inputs, or checkpoints.
It is a bounded increment for [issue #265](https://github.com/1337isnot1337/DOPPLERGEIST/issues/265).
The original a904 report and measured stage CPU acceptance remain outstanding.

Run this from an installed checkout. Pass a saved job directory or its `search`
directory. Store the reports and optional cache outside that job:

```bash
python scripts/report_checkpoint_progress.py /path/to/job/search \
  --json /path/to/reports/progress.json \
  --markdown /path/to/reports/progress.md \
  --cache /path/to/reports/progress-cache.json
```

The same command refreshes the report. Unchanged receipt summaries and grid
counts come from the cache. The script refreshes canonical stage metadata on
every invocation. Without output arguments, it prints JSON to standard output.

## What the report counts

The report reconstructs the saved spacing, CRS, geographic bounds, root tile
size, `max_tiles`, and spatial split tree. Requested roots and the scheduled
prefix have separate denominators. Child tiles do not inflate the root count.

Receiver counts use the producer's integer projected grid and geographic bounds.
They do not use the checkpoint's `grid_points` counter. Empty-flight tiles can
record zero receiver-point visits after processing a nonempty receiver region.
Recorded points can also reach the CPA screen without receiving an acoustic fit.

Clock siblings share the same receiver cell. The cell counts once, after all
required clock descendants have receipts. An incomplete clock partition remains
outstanding. The script checks the recorded split geometry and child names; it
does not rerun clock interval or scoring calculations.

The output separates two accounting levels:

| Field | Required evidence | Remaining limit |
| --- | --- | --- |
| `receipt_recorded` | Matching manifest producer, payload hash, cell rectangle, and required split descendants | A receipt may precede its ledger commit. |
| `canonical_stage_agreed` | The same receipt plus a matching committed canonical stage checksum | Record and bound contents are not rehashed. |

Neither level certifies resumability, scientific completeness, data coverage, or
localization accuracy. Unsupported trajectories and unresolved trials remain
explicit counters. Terrain and source files are not rehashed. A failed receipt,
unknown producer, missing child, unreadable file, or read limit leaves work
unresolved. Canonical stages without reconciled receipts are counted separately.
Fine-grid files have a separate inventory and do not add to coarse coverage.

Each saved counter keeps its own unit. Track counts are recorded segment visits
after query filtering, not raw query counts or unique aircraft. CPA rows,
associations, clock trials, and retained mode
appearances are separate. Missing or invalid fields stay unknown; they are not
zero. There are no inferred pass rates across different units.

The `trial_classification` section counts evaluated, rejected and unknown
clock-trial visits using the scorer's disjoint visit categories. A percentage is
shown only when **every active coarse receipt** includes all three counters plus
the unreported-envelope counter, no envelope remains unreported, and at least
one trial visit was recorded. Missing counters, including those in historic
empty-tile receipts, remain unknown instead of being assumed zero. An unreported
envelope is counted in candidate units and cannot be added to clock-trial visits.
This is a ratio within the selected receipt population, not a nationwide
rejection rate or an estimate of flight coverage. Time and proposal-count
statistics are not trial denominators.

Historical checkpoints do not supply stage CPU/wall percentiles, stage
throughput, unique aircraft/sample counts, complete nonlinear fit counts, or
global retained-mode counts. Those output fields remain `null`.

## Read limits and active jobs

Each refresh reads at most 128 MiB of new JSON by default. A single JSON input
must be at most 64 MiB. Split metadata has a 64 KiB limit; manifest/workload input
has an 8 MiB limit. Change the receipt limits explicitly when necessary:

```bash
python scripts/report_checkpoint_progress.py /path/to/job/search \
  --max-read-mib 256 --max-file-mib 96 \
  --cache /path/to/reports/progress-cache.json \
  --json /path/to/reports/progress.json
```

Small receipts are read first. A limited refresh is a biased partial inventory.
Later refreshes can read additional changed files because cached summaries do
not consume the JSON read budget. Oversized files remain unresolved until the
individual limit permits them. JSON decoding can use more memory than file size.

The external cache keys summaries by device, inode, size, modification time,
change time, and reader source hash. It is a local performance cache, not an
independent content-integrity proof. The script checks input stats before and
after reading and reports whether the receipt inventory changed during refresh.

SQLite opens with `mode=ro` and `query_only`, in one read transaction. The script
reads `run` and the indexed coarse range of `stages`, capped at 100,000 rows.
It never scans `records` or `bounds`, hashes the database, checkpoints the WAL,
or opens worker ledgers. Standard SQLite WAL shared-memory bookkeeping can still
occur; no application rows are written. The database header identifies WAL mode
even if its sidecars are absent. A WAL-mode database needs existing readable
WAL and SHM files of at least 32 bytes and 32 KiB, respectively. Otherwise,
canonical agreement stays unknown without opening a SQLite connection.
Pre-open checks cannot atomically prevent sidecar races during an active
producer's shutdown. Normal SQLite shared-memory reader bookkeeping is allowed.
Do not copy an active database file alone and treat it as a consistent snapshot.
See SQLite's [read-only WAL rules](https://www.sqlite.org/wal.html#read_only_databases)
and [WAL file format](https://www.sqlite.org/walformat.html).

The SQLite transaction cannot atomically include separate JSON files. A report
from an active producer is an observation across those files and that database
snapshot. Changed inputs are marked provisional. Empty-tile receipts can exist
before their transaction commits; they do not receive canonical agreement until
the committed stage metadata is visible.

The script accepts only the exact manifest run identity. It lists other payload
producer identities but does not approve them through an upgrade receipt alone.
That compatibility review remains separate. Geometry reconstruction reports the
current NumPy, pyproj, and PROJ versions; it does not reproduce a historic runtime.
Known saved/current NumPy or pyproj version differences are flagged explicitly.

Receiver reconstruction is capped at 20 million projected grid points and
100,000 roots. `--max-grid-points` changes the first limit. A missing pyproj
installation, `--skip-grid`, or a saved receiver mask leaves receiver counts
unknown. The script does not approximate a country mask with its bounding box.

## Existing recorded workload check

The existing fixture contains 256 completed empty coarse checkpoints from the
stopped original automatic baseline. Its source and recording hashes are saved
inside the fixture. It is not a904, a completed national run, or an accuracy test.

```bash
python scripts/report_checkpoint_progress.py \
  tests/data/real-empty-tile-workload.json \
  --json /path/to/reports/recorded-empty-progress.json \
  --markdown /path/to/reports/recorded-empty-progress.md \
  --cache /path/to/reports/recorded-empty-cache.json
```

The script verifies each recorded payload hash. The recorded point and track
counters are zero. The installed check reconstructs 53,210 receiver points in
these 256 tiles, exactly matching the current production grid. The requested
grid has 19,278 root tiles; this fixture is a partial set of receipts. CPA and
trial counters are absent and stay unknown. The portable fixture has no canonical
ledger, so canonical agreement remains unknown. Repeating the command exercises
the incremental cache without generating recordings, tracks, or fixture data.
