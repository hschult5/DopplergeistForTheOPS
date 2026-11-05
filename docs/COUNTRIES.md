# Country profiles

The automatic request and upload interface support five receiver regions:

| Country | Coverage | Envelope: west, south, east, north | Search CRS | Receiver region |
| --- | --- | --- | --- | --- |
| `US` | `conus` | `-125, 24, -66, 50` | `EPSG:5070` | Existing CONUS rectangle |
| `DE` | `germany` | `5.5, 47, 15.5, 55.2` | `EPSG:32632` | BKG VG250 Germany land polygon inside the envelope |
| `ES` | `spain` | `-18.2, 27.5, 4.5, 43.9` | `EPSG:6933` | Spain and territories bounding envelope; no country polygon |
| `FR` | `france` | `-5.5, 41.2, 10, 51.2` | `EPSG:2154` | Metropolitan France including Corsica bounding envelope; no country polygon |
| `IT` | `italy` | `6.6, 35.4, 18.6, 47.1` | `EPSG:7794` | Italy including Sicily, Sardinia and Lampedusa bounding envelope; no country polygon |

The envelope limits optional rectangular subsets. Germany's envelope includes
foreign territory; the land polygon removes those receiver locations before
terrain sampling or scoring. Islands, exclaves and polygon holes are preserved.
The boundary is generalized geographic data, not a survey of a microphone site.
Spain uses an outer envelope covering mainland Spain and its island territories.
France covers mainland France and Corsica, excluding overseas departments and
territories, with a Lambert-93 metric search grid. Italy covers the mainland,
Sicily, Sardinia and the Pelagie islands with the national RDN2008 / Italy zone
(E-N) transverse Mercator grid; San Marino, Vatican City and parts of
neighboring countries and the sea fall inside its envelope. These three profiles do not
apply country land masks: their envelopes include sea and neighboring countries.
Use explicit bounds that describe the intended receiver area. Aircraft outside
the receiver bounds remain eligible when they are within the acoustic search margin. Country support
does not establish localization accuracy.

## Requests and commands

```bash
dopplergeist analyze recording.mp3 --country DE --jobs jobs --online \
  --start-utc "$RECORDING_START_UTC_MIN" --end-utc "$RECORDING_START_UTC_MAX" \
  --time-source 'Evidence establishing the recording-start interval'
```

Use `--country FR` for metropolitan France, including Corsica, or `--country IT`
for Italy, or select the country in the browser's country selector. The upload API accepts
`"search":{"country":"DE"}`, `"search":{"country":"ES"}` or
`"search":{"country":"FR"}` or `"search":{"country":"IT"}`. It also accepts
the matching coverage names `germany`, `spain`, `france` and `italy`. A normalized request records the coverage;
an explicitly supplied country is normalized to uppercase. If both are supplied,
they must agree. `bounds_wsen` and `--bounds` narrow the selected profile. They
must be an independently justified prior, not the evaluation location.

Omitting country preserves the previous CONUS behavior. Old requests/results
without a country field remain readable. An older service advertises no country
catalog, so the new browser offers only CONUS and sends the legacy request shape.

The configured research command also accepts a top-level `country` or a `search`
object. A supplied `grid.crs` must match the country profile, and `grid.bounds`
must agree with any `search.bounds_wsen`. That command retains its existing
configured-event and uncertified-coverage limitations.

## Germany boundary acquisition

The first online German search obtains the country layer `vg250:vg250_sta`,
filtered to `GF=4`, from the [official BKG WFS](https://sgx.geodatenzentrum.de/wfs_vg250).
It requests `EPSG:25832` so the original ETRS89 / UTM 32N geometry is retained.
Receiver coordinates are transformed into that CRS for point-in-polygon tests.
The search grid and reported projected coordinates use `EPSG:32632` consistently.

The WFS product advertises the 2025-01-01 dataset. The pinned land feature has
object ID `DEBKGVG200000CKM` and `BEGINN=2025-04-11Z`; it is not described as the
separate 2026 download product. Its canonical geometry, properties and CRS have
SHA-256 `b5e9276fe37ca1c0d65b4542a075cc246e97194ed70d3259b0d7900ad944a1d3`.
The response's request timestamp is excluded from that content identity.

The validated feature is cached at
`CACHE_ROOT/geography/de-vg250-b5e9276fe37ca1c0.json`. The download is limited to
8 MiB and written atomically after validation. Offline searches require this
cache entry. Copying this entry to another machine preserves the same boundary.
Missing, damaged or changed source data produce an explicit data-unavailable
outcome. A future WFS update does not silently replace the pinned geometry;
review its data and update the pin deliberately.

No source simplification is applied. The source's 110 polygon components and
their holes remain intact. Prepared vector predicates avoid creating a separate
geometry object for every receiver. The mask is serialized to each search worker.

## Search and checkpoint behavior

The mask applies to serial and parallel coarse tiles, fine tiles, research grids,
continuous refinement and every sensitivity scenario. A masked-out continuous
proposal never reaches terrain sampling or acoustic scoring. A valid seed remains
available when optimization proposes an invalid location. A refinement whose
explored radius meets the country boundary is marked boundary-limited.

Before automatic flight acquisition, a bounded geographic check establishes at
least one eligible coarse-grid receiver. Foreign-only selections and small land
selections containing no coarse-grid point return `needs_input` with a specific
area or grid-resolution explanation. An inconclusive bounded check stops with an
explicit limitation; it does not report an empty area or a completed flight search.
The full-country check uses land-component probes rather than enumerating the
national grid.

Aircraft are not country-masked. Automatic acquisition retains full historical
trajectories, and tile queries include the existing audible-range border margin.
Archive absence or incomplete trajectory/model support remains visible.

German search identities include the boundary content, source, selected CRS and
Shapely version. Changing those inputs cannot reuse incompatible search results.
Legacy CONUS requests retain their meaning, but a new code revision does not
automatically make an old source-pinned checkpoint resumable. Preserve the old
producer or use a separately verified migration; no identity bypass is added.

## Geographic checks

```bash
python scripts/check_country_profiles.py --online --cache-root /tmp/dopplergeist-geography
python scripts/check_country_profiles.py --cache-root /tmp/dopplergeist-geography
```

The check uses the actual pinned BKG geometry, named geographic points, country
boundary grids, mainland France and Corsica grids, empty-area checks and worker
serialization. It generates no audio or flight tracks.
These are software/geography checks, not an acoustic localization benchmark.

## Boundary license and source

© [BKG](https://www.bkg.bund.de) 2026,
[dl-de/by-2-0](https://www.govdata.de/dl-de/by-2-0).
[Data sources](https://sgx.geodatenzentrum.de/web_public/gdz/datenquellen/datenquellen_vg_nuts.pdf).
[Official product metadata](https://gdz.bkg.bund.de/index.php/default/wfs-verwaltungsgebiete-1-250-000-stand-01-01-wfs-vg250.html).

Processing: selection of the land feature and coordinate transformation for
receiver tests; no polygon simplification. Boundary data retains its source
license. The application code retains the repository's PolyForm license.
