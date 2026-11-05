"""Supported receiver regions and the pinned BKG Germany land boundary.

Country masks restrict receiver locations, never aircraft trajectories.  The US
profile deliberately retains the historical CONUS rectangle and CRS.  Germany
uses BKG VG250 land geometry in its native ETRS89 / UTM 32N coordinates. Spain,
metropolitan France and Italy use outer geographic envelopes; the caller's
declared bounds select the actual receiver search region.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class SearchProfile:
    country: str
    coverage: str
    name: str
    bounds_wsen: tuple[float, float, float, float]
    crs: str


_PROFILES = {
    "US": SearchProfile("US", "conus", "United States (CONUS)",
                        (-125.0, 24.0, -66.0, 50.0), "EPSG:5070"),
    "DE": SearchProfile("DE", "germany", "Germany",
                        (5.5, 47.0, 15.5, 55.2), "EPSG:32632"),
    "ES": SearchProfile("ES", "spain", "Spain (all territories; bounding envelope)",
                        (-18.2, 27.5, 4.5, 43.9), "EPSG:6933"),
    "FR": SearchProfile("FR", "france", "France (metropolitan; bounding envelope)",
                        (-5.5, 41.2, 10.0, 51.2), "EPSG:2154"),
    "IT": SearchProfile("IT", "italy", "Italy (including Sicily and Sardinia; bounding envelope)",
                        (6.6, 35.4, 18.6, 47.1), "EPSG:7794"),
}
_COVERAGE_COUNTRIES = {p.coverage: code for code, p in _PROFILES.items()}

# WFS metadata advertises the 2025-01-01 product; the returned land object's
# BEGINN is 2025-04-11.  Pin the actual feature, not an assumed download vintage.
# The digest covers canonical {properties, geometry, crs}, excluding the WFS
# request timestamp.  A server update requires deliberate review of a new pin.
BKG_BOUNDARY_SHA256 = "b5e9276fe37ca1c0d65b4542a075cc246e97194ed70d3259b0d7900ad944a1d3"
BKG_BOUNDARY_URL = "https://sgx.geodatenzentrum.de/wfs_vg250?" + urlencode({
    "service": "WFS", "version": "2.0.0", "request": "GetFeature",
    "typeNames": "vg250:vg250_sta", "outputFormat": "application/json",
    "srsName": "EPSG:25832", "CQL_FILTER": "gf=4",
})
BKG_LICENSE_URL = "https://www.govdata.de/dl-de/by-2-0"
BKG_SOURCES_URL = "https://sgx.geodatenzentrum.de/web_public/gdz/datenquellen/datenquellen_vg_nuts.pdf"
_MAX_BOUNDARY_BYTES = 8 * 1024 * 1024


class EmptySearchArea(ValueError):
    """A selected country area has no land or no eligible coarse receivers."""

    def __init__(self, reason_code, message):
        super().__init__(message)
        self.reason_code = reason_code


def profile_for_search(search=None):
    """Resolve one supported profile, rejecting contradictory declarations."""
    if search is None:
        search = {}
    if not isinstance(search, dict):
        raise ValueError("search must be an object")
    country = search.get("country")
    coverage = search.get("coverage")
    if "country" in search:
        if not isinstance(country, str) or country.upper() not in _PROFILES:
            raise ValueError("Supported countries are US (CONUS), DE (Germany), ES (Spain), FR (metropolitan France) and IT (Italy)")
        country = country.upper()
    if "coverage" in search and (not isinstance(coverage, str) or coverage not in _COVERAGE_COUNTRIES):
        raise ValueError("Supported coverage values are conus (US/CONUS), germany (DE), spain (ES), france (FR) and italy (IT)")
    if country is None:
        country = _COVERAGE_COUNTRIES.get(coverage, "US")
    profile = _PROFILES[country]
    if coverage is not None and coverage != profile.coverage:
        raise ValueError(f"Country {country} requires coverage {profile.coverage}")
    return profile


def public_profiles():
    """Return JSON-safe profiles for the CLI/API/browser, with no local paths."""
    return [{"country": p.country, "coverage": p.coverage, "name": p.name,
             "bounds_wsen": list(p.bounds_wsen), "crs": p.crs}
            for p in _PROFILES.values()]


def _canonical_bytes(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _checked_record(record):
    """Validate the exact recorded BKG feature before constructing a mask."""
    if not isinstance(record, dict) or set(record) != {"properties", "geometry", "crs"}:
        raise ValueError("Invalid cached Germany boundary record")
    digest = hashlib.sha256(_canonical_bytes(record)).hexdigest()
    if digest != BKG_BOUNDARY_SHA256:
        raise ValueError("Germany boundary differs from the pinned BKG feature; review the source before updating the cache")
    return record


class ReceiverMask:
    """A pickle-safe prepared land polygon, in its original projected CRS."""

    def __init__(self, record):
        record = _checked_record(record)
        from shapely.geometry import shape
        geometry = shape(record["geometry"])
        if geometry.geom_type != "MultiPolygon" or geometry.is_empty or not geometry.is_valid:
            raise ValueError("BKG Germany boundary is not a valid nonempty MultiPolygon")
        properties = record["properties"]
        if (properties.get("gf") != 4 or properties.get("lkz") != "DE"
                or record["crs"].get("properties", {}).get("name") != "urn:ogc:def:crs:EPSG::25832"):
            raise ValueError("BKG boundary has an unsupported country, component or CRS")
        self._identity = {
            "country": "DE", "coverage": "germany", "search_crs": "EPSG:32632",
            "source_crs": "EPSG:25832", "boundary_sha256": BKG_BOUNDARY_SHA256,
            "source_url": BKG_BOUNDARY_URL, "source_layer": "vg250:vg250_sta",
            "selection": "GF=4 (land)", "source_object_id": properties["objid"],
            "source_begin": properties["beginn"], "advertised_product_date": "2025-01-01",
            "attribution": "© BKG 2026, dl-de/by-2-0",
            "license_url": BKG_LICENSE_URL, "data_sources_url": BKG_SOURCES_URL,
            "processing": "Unmodified land polygon; receiver coordinates transformed from WGS84 to ETRS89 / UTM 32N",
        }
        self._initialize_geometry(geometry)

    def _initialize_geometry(self, geometry):
        import shapely
        from pyproj import Transformer
        self._geometry = geometry
        shapely.prepare(self._geometry)
        self._boundary = self._geometry.boundary
        self._to_native = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)

    @property
    def identity(self):
        return deepcopy(self._identity)

    def contains(self, lon, lat):
        """Include the polygon boundary, retaining islands and polygon holes."""
        import shapely
        x, y = self._to_native.transform(lon, lat)
        return shapely.intersects_xy(self._geometry, x, y)

    def preflight_grid(self, bounds_wsen, spacing_m, crs, *, max_points=65536):
        """Reject empty selections before flight acquisition, with bounded work.

        True means an actual coarse-grid receiver was found. None means the
        bounded check was inconclusive; it must not be called an empty area.
        Small grids are examined completely. Large grids probe near interior
        points of the actual clipped land components, without enumerating the
        national grid. Every probe uses the production grid/mask predicate.
        """
        from pyproj import Transformer
        from shapely.geometry import box
        from .tiled_search import _grid_extent, _tile_coordinates

        if type(max_points) is not int or max_points < 1:
            raise ValueError("Receiver preflight point limit must be a positive integer")
        if not math.isfinite(spacing_m) or spacing_m <= 0:
            raise ValueError("Receiver grid spacing must be finite and positive")
        west, south, east, north = bounds_wsen
        left, bottom, right, top = self._to_native.transform_bounds(*bounds_wsen, densify_pts=101)
        # Within the DE profile (5.5--15.5 E, 47--55.2 N), UTM 32N northing
        # along a latitude edge is smallest nearest the 9 E central meridian.
        # Include that interior extremum explicitly; other edge extrema are
        # at corners. Do not rely on densification happening to sample 9 E.
        meridian = max(west, min(east, 9.0))
        _, northings = self._to_native.transform([meridian, meridian], [south, north])
        bottom, top = min(bottom, *northings), max(top, *northings)
        # Ten metres is a numerical allowance for this fixed projection and
        # region, not a general reprojection error bound. Only the exclusion
        # shortcut uses this expansion; actual grid membership remains exact.
        envelope = box(left - 10.0, bottom - 10.0, right + 10.0, top + 10.0)
        if not self._geometry.intersects(envelope):
            raise EmptySearchArea("empty_search_area",
                "The selected rectangle contains no German land in the BKG receiver boundary. "
                "Choose an area that includes a supported receiver location.")

        xmin, ymin, xmax, ymax = _grid_extent(bounds_wsen, spacing_m, crs)
        total = (xmax - xmin + 1) * (ymax - ymin + 1)
        if total <= max_points:
            points = _tile_coordinates((xmin, ymin, xmax + 1, ymax + 1), spacing_m,
                                       bounds_wsen, crs, self)
            if len(points):
                return True
            raise EmptySearchArea("empty_search_grid",
                "The selected rectangle contains no German receiver points on the configured "
                f"{spacing_m:g} m coarse grid. Choose a wider area containing German land, "
                "or ask the operator to use a smaller coarse-grid spacing.")

        overlap = self._geometry.intersection(envelope)
        native_to_grid = Transformer.from_crs("EPSG:25832", crs, always_xy=True)
        parts = (overlap, *getattr(overlap, "geoms", ()))
        checked = 0
        for part in parts:
            if part.is_empty:
                continue
            interior = part.representative_point()
            x, y = native_to_grid.transform(interior.x, interior.y)
            ix, iy = math.floor(x / spacing_m), math.floor(y / spacing_m)
            cells = (max(xmin, ix - 1), max(ymin, iy - 1),
                     min(xmax + 1, ix + 2), min(ymax + 1, iy + 2))
            count = max(0, cells[2] - cells[0]) * max(0, cells[3] - cells[1])
            if checked + count > max_points:
                break
            checked += count
            if count and len(_tile_coordinates(cells, spacing_m, bounds_wsen, crs, self)):
                return True
        return None

    def boundary_limited(self, lon, lat, radius_m):
        """Conservatively flag a local sensitivity search that meets the mask.

        One percent covers UTM scale variation across this Germany envelope.
        This is a truncation flag, not a calibrated geographic error bound.
        """
        from shapely.geometry import Point
        if not math.isfinite(radius_m) or radius_m < 0:
            raise ValueError("Boundary proximity radius must be finite and nonnegative")
        x, y = self._to_native.transform(lon, lat)
        if not math.isfinite(x) or not math.isfinite(y):
            return True
        point = Point(x, y)
        return not self._geometry.covers(point) or self._boundary.distance(point) <= 1.01 * radius_m

    def __getstate__(self):
        from shapely import to_wkb
        return {"geometry_wkb": to_wkb(self._geometry), "identity": self._identity}

    def __setstate__(self, state):
        from shapely import from_wkb
        self._identity = state["identity"]
        self._initialize_geometry(from_wkb(state["geometry_wkb"]))


def receiver_mask_for_search(search, *, cache_root, offline=True):
    """Load the pinned Germany polygon, acquiring it only when online is allowed.

    No boundary is inferred from a rectangular envelope or a failed download.
    Existing cache bytes are checked each time; missing or changed data produce
    an explicit data-unavailable outcome through the acquisition adapter.
    """
    if profile_for_search(search).country != "DE":
        return None
    from .data_access import DataUnavailable
    path = Path(cache_root) / "geography" / f"de-vg250-{BKG_BOUNDARY_SHA256[:16]}.json"
    try:
        if path.exists():
            with path.open("rb") as handle:
                data = handle.read(_MAX_BOUNDARY_BYTES + 1)
            if len(data) > _MAX_BOUNDARY_BYTES:
                raise ValueError("Cached Germany boundary exceeds the size limit")
            return ReceiverMask(json.loads(data))
        if offline:
            raise DataUnavailable(f"Germany receiver boundary is not cached at {path}; enable online acquisition once to obtain the pinned BKG land mask")
        request = Request(BKG_BOUNDARY_URL, headers={"User-Agent": "DOPPLERGEIST/0.1 country-boundary"})
        with urlopen(request, timeout=60) as response:
            raw = response.read(_MAX_BOUNDARY_BYTES + 1)
        if len(raw) > _MAX_BOUNDARY_BYTES:
            raise ValueError("BKG Germany boundary exceeds the download limit")
        collection = json.loads(raw)
        features = collection.get("features", [])
        if collection.get("type") != "FeatureCollection" or len(features) != 1:
            raise ValueError("Expected exactly one BKG Germany land feature")
        feature = features[0]
        record = {"properties": feature["properties"], "geometry": feature["geometry"], "crs": collection["crs"]}
        mask = ReceiverMask(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(_canonical_bytes(record))
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return mask
    except DataUnavailable:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise DataUnavailable(f"Germany receiver boundary unavailable: {error}") from error
