"""3-D geometry and emission-time acoustic propagation, in SI units.

Range rate is positive for a receding source. Observer and aircraft heights must
use the same vertical datum. ECEF treats those heights as ellipsoidal; supplying
orthometric heights to both is a documented common-datum approximation.
"""
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from scipy.interpolate import CubicHermiteSpline, PPoly
from scipy.optimize import minimize_scalar
from scipy.signal import find_peaks

from .native_acoustic import ACOUSTIC_BACKEND, try_acoustic as _try_native_acoustic

_EMISSION_ITERATION_TOLERANCE_S = 1e-7
_PACKED_KINEMATICS_MAX_INTERVALS = 4096
_MOTION_SUPPORT_INTERVAL_BATCH = 1024


@lru_cache(maxsize=8)
def _packed_kinematics(position, velocity):
    """Evaluate fixed position/velocity splines with one interval lookup.

    Padding the quadratic velocity coefficients with a leading zero keeps the
    existing polynomial arithmetic. Only small splines enter this cache: eight
    entries add at most 6 MiB of coefficients and keep eight source pairs alive.
    Track changes construct new splines, including altitude-bias trials.
    """
    coefficients = np.zeros((4, position.c.shape[1], 6))
    coefficients[:, :, :3] = position.c
    coefficients[1:, :, 3:] = velocity.c
    return PPoly.construct_fast(coefficients, position.x, extrapolate=False)


class AcousticSupportError(ValueError):
    """A trajectory cannot support the assumed emission-time acoustic model."""
    def __init__(self, track_id, reason, *, emission_intervals_s=None, receive_intervals_s=None):
        self.track_id, self.reason = track_id, reason
        self.emission_intervals_s = emission_intervals_s
        self.receive_intervals_s = receive_intervals_s
        super().__init__(f"{track_id}: {reason}")


def ecef(lat, lon, height):
    lat, lon, height = np.broadcast_arrays(np.deg2rad(lat), np.deg2rad(lon), height)
    a, e2 = 6378137.0, 6.6943799901413165e-3
    n = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
    return np.stack(((n + height) * np.cos(lat) * np.cos(lon),
                     (n + height) * np.cos(lat) * np.sin(lon),
                     (n * (1 - e2) + height) * np.sin(lat)), axis=-1)


_CANONICAL_ECEF = ecef


def velocity_ecef(lat, lon, east, north, up):
    lat, lon = np.deg2rad(lat), np.deg2rad(lon)
    return np.stack((-np.sin(lon) * east - np.sin(lat) * np.cos(lon) * north + np.cos(lat) * np.cos(lon) * up,
                     np.cos(lon) * east - np.sin(lat) * np.sin(lon) * north + np.cos(lat) * np.sin(lon) * up,
                     np.cos(lat) * north + np.sin(lat) * up), axis=-1)


@dataclass
class Track:
    id: str
    time: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    altitude: np.ndarray
    typecode: str = ""
    velocity: np.ndarray | None = None

    def __post_init__(self):
        self.time, self.lat, self.lon, self.altitude = [np.asarray(a, dtype=float) for a in
            (self.time, self.lat, self.lon, self.altitude)]
        if len(self.time) < 3 or not np.all(np.diff(self.time) > 0):
            raise ValueError(f"{self.id}: need >=3 strictly ordered unique timestamps")
        if not np.all(np.isfinite(np.column_stack((self.time, self.lat, self.lon, self.altitude)))):
            raise ValueError(f"{self.id}: nonfinite track data")
        self.xyz = ecef(self.lat, self.lon, self.altitude)
        self._xyz_min = self.xyz.min(axis=0)
        self._xyz_max = self.xyz.max(axis=0)
        self._max_sample_gap = float(np.max(np.diff(self.time)))
        self.origin = self.time[0]
        self.relative_time = self.time - self.origin
        v = self.velocity
        if v is None:
            v = np.gradient(self.xyz, self.relative_time, axis=0)
        self.spline = CubicHermiteSpline(self.relative_time, self.xyz, v, extrapolate=False)
        self.derivative = self.spline.derivative()
        # The derivative's quadratic Bezier control vectors bound its speed.
        # Most clean intervals need no polynomial roots; exact maxima are cached
        # only when this inexpensive upper bound reaches the requested c.
        v = np.asarray(v)
        middle = 3*np.diff(self.xyz, axis=0)/np.diff(self.time)[:, None]-v[:-1]-v[1:]
        self._speed_upper = np.maximum.reduce([np.linalg.norm(a, axis=1)
            for a in (v[:-1], middle, v[1:])])
        self._speed_peak = np.full(len(self.time)-1, np.nan)

    def position(self, t):
        return self.spline(np.asarray(t) - self.origin)

    def speed_vector(self, t):
        return self.derivative(np.asarray(t) - self.origin)

    def _position_velocity(self, t):
        if len(self.time)-1 > _PACKED_KINEMATICS_MAX_INTERVALS:
            return self.position(t), self.speed_vector(t)
        values = _packed_kinematics(self.spline, self.derivative)(np.asarray(t)-self.origin)
        return values[..., :3], values[..., 3:]

    def _unsupported_motion_intervals(self, c):
        """Find non-subsonic native spline intervals, caching speed extrema.

        Velocity is quadratic in normalized interval time. Its squared norm is
        quartic, so endpoints and real roots of its cubic derivative enumerate
        all extrema (in double precision). A small margin treats the sonic
        boundary as unsupported.
        """
        limit = c*(1-1e-10)
        suspicious = np.flatnonzero(self._speed_upper >= limit)
        if not len(suspicious):
            return suspicious
        # Extrema do not change with the sound-speed threshold. Only compute
        # the intervals that have not been solved on an earlier query.
        for i in suspicious[np.isnan(self._speed_peak[suspicious])]:
            dt = self.time[i+1]-self.time[i]
            a, b, d = self.derivative.c[:, i]
            a, b = a*dt**2, b*dt
            stationary = [2*np.dot(b, d), 2*(np.dot(b, b)+2*np.dot(a, d)),
                          6*np.dot(a, b), 4*np.dot(a, a)]
            roots = np.polynomial.polynomial.polyroots(stationary)
            samples = [0., 1.] + [float(r.real) for r in roots
                if abs(r.imag) <= 1e-8 and 0 < r.real < 1]
            self._speed_peak[i] = max(np.linalg.norm((a*u+b)*u+d) for u in samples)
        return suspicious[self._speed_peak[suspicious] >= limit]

    def validate_motion_support(self, c=343.0):
        """Require subsonic motion throughout the source used for CPA proposals.

        The outer search may isolate this track and retain incomplete coverage.
        Direct ridge scoring instead tests the time-local arrival envelope below.
        """
        indices = self._unsupported_motion_intervals(c)
        if len(indices):
            raise AcousticSupportError(self.id, "non_subsonic_motion",
                emission_intervals_s=[[float(self.time[i]), float(self.time[i+1])] for i in indices])

    def _cpa_interval_exclusion(self, observers, radius, c, sample_time, sample_position, max_step,
                                sample_range=None):
        """Prove bad native intervals cannot affect this sparse CPA query.

        Keep the original spline, sample lattice and global maximum sample
        step. Bad curves must be outside every receiver sphere. Both endpoints
        of every lattice edge touching a bad interval must also be outside the
        original KD-tree radius, so no such edge, sampled minimum or endpoint
        refinement can be used. A failed proof leaves whole-track CPA unknown.
        A bounded query only needs proof for intervals whose affected edges
        occur in its selected global-lattice slice. This says nothing about
        later acoustic arrivals from those intervals.
        """
        methods = ("position", "speed_vector", "_unsupported_motion_intervals", "validate_motion_support")
        if (type(self) is not Track or any(name in self.__dict__ for name in methods)
                or tuple(getattr(Track, name) for name in methods) != _CANONICAL_CPA_MOTION_FUNCTIONS
                or type(radius) not in (int, float) or type(c) not in (int, float)
                or not np.isfinite([radius, c, max_step]).all() or radius <= 0 or c <= 0 or max_step < 0):
            return None
        observers = np.asarray(observers)
        if (observers.dtype != np.dtype(float) or observers.ndim != 2 or observers.shape[1] != 3
                or not len(observers) or not np.isfinite(observers).all()
                or sample_time.dtype != np.dtype(float) or sample_time.ndim != 1 or len(sample_time) < 2
                or sample_position.shape != (len(sample_time), 3)
                or not np.isfinite(sample_time).all() or not np.isfinite(sample_position).all()
                or sample_time[0] != self.time[0] or sample_time[-1] != self.time[-1]
                or not np.all(np.diff(sample_time) > 0)):
            return None
        edge_first, edge_stop = 0, len(sample_time)-1
        if sample_range is not None:
            if (type(sample_range) not in (tuple, list) or len(sample_range) != 2
                    or any(type(value) is not int for value in sample_range)):
                return None
            sample_first, sample_last = sample_range
            if not (0 <= sample_first <= sample_last <= len(sample_time)):
                return None
            edge_first, edge_stop = sample_first, max(sample_first, sample_last-1)
        indices = self._unsupported_motion_intervals(c)
        if not len(indices):
            return None
        lower, upper = observers.min(axis=0), observers.max(axis=0)
        query_radius = float(np.nextafter(radius + 2 * max_step, np.inf))
        if not np.isfinite(query_radius):
            return None
        outside_query = []
        if sample_range is not None:
            relevant = []
            for first in range(0, len(indices), _MOTION_SUPPORT_INTERVAL_BATCH):
                selected = indices[first:first+_MOTION_SUPPORT_INTERVAL_BATCH]
                times = self.time[np.column_stack((selected, selected+1))]
                tolerance = np.maximum(1e-7, 4*np.abs(np.spacing(np.maximum(np.abs(times[:, 0]),
                                                                           np.abs(times[:, 1])))))
                left = np.maximum(0, np.searchsorted(sample_time, times[:, 0]-tolerance, side="left")-1)
                right = np.minimum(len(sample_time)-1,
                                   np.searchsorted(sample_time, times[:, 1]+tolerance, side="right"))
                keep = ((left < edge_stop) & (right > edge_first) & (edge_first < edge_stop))
                if np.any(keep):
                    relevant.append(selected[keep])
                outside_query.extend(times[~keep].tolist())
            indices = np.concatenate(relevant) if relevant else np.empty(0, dtype=int)
            if not len(indices):
                return {"track_id": self.id, "method": "outside_bounded_cpa_sample_edges",
                        "emission_time_range_s": [float(self.time[0]), float(self.time[-1])],
                        "emission_intervals_s": [],
                        "outside_query_scope_intervals_s": [[float(a), float(b)] for a, b in outside_query],
                        "sound_speed_mps": float(c), "max_distance_m": float(radius),
                        "max_sample_step_m": float(max_step), "query_radius_m": query_radius,
                        "hull_distance_lower_m": None, "edge_distance_lower_m": None,
                        "sample_count": len(sample_time), "affected_sample_edges": 0,
                        "sample_range": [sample_first, sample_last],
                        "receiver_point_count": len(observers),
                        "observer_box_ecef_m": [lower.tolist(), upper.tolist()]}
        affected_edges = np.zeros(len(sample_time)-1, dtype=bool)
        hull_lower = np.inf
        # No interval-by-receiver matrix: the receiver box encloses the actual
        # receiver set, and fixed-size interval blocks bound temporary storage.
        for first in range(0, len(indices), _MOTION_SUPPORT_INTERVAL_BATCH):
            selected = indices[first:first+_MOTION_SUPPORT_INTERVAL_BATCH]
            times = self.time[np.column_stack((selected, selected+1))]
            dt = (times[:, 1]-times[:, 0])[:, None]
            velocity = self.speed_vector(times)
            p0, p1 = self.xyz[selected], self.xyz[selected+1]
            with np.errstate(over="ignore", invalid="ignore"):
                controls = np.stack((p0, p0+dt*velocity[:, 0]/3,
                                     p1-dt*velocity[:, 1]/3, p1), axis=1)
            if not np.isfinite(controls).all():
                return None
            lo, hi = controls.min(axis=1), controls.max(axis=1)
            separation = np.maximum(np.maximum(lo-upper, lower-hi), 0)
            scale = max(1., float(np.abs(controls).max()), float(np.abs(observers).max()), radius)
            margin = 1e-6 + 128 * np.finfo(float).eps * scale
            distances = np.nextafter(np.linalg.norm(separation, axis=1)-margin, -np.inf)
            if not np.isfinite(distances).all() or not np.all(distances > radius):
                return None
            hull_lower = min(hull_lower, float(distances.min()))
            for start, end in times:
                tolerance = max(1e-7, 4*abs(np.spacing(max(abs(start), abs(end)))))
                left = max(0, int(np.searchsorted(sample_time, start-tolerance, side="left"))-1)
                right = min(len(affected_edges), int(np.searchsorted(sample_time, end+tolerance, side="right")))
                affected_edges[left:right] = True
        if sample_range is not None:
            affected_edges[:edge_first] = False
            affected_edges[edge_stop:] = False
        edges = np.flatnonzero(affected_edges)
        if not len(edges):
            return None
        vertices = np.unique(np.r_[edges, edges+1])
        positions = sample_position[vertices]
        separation = np.maximum(np.maximum(positions-upper, lower-positions), 0)
        scale = max(1., float(np.abs(positions).max()), float(np.abs(observers).max()), query_radius)
        margin = 1e-6 + 128 * np.finfo(float).eps * scale
        distances = np.nextafter(np.linalg.norm(separation, axis=1)-margin, -np.inf)
        if not np.isfinite(distances).all() or not np.all(distances > query_radius):
            return None
        return {"track_id": self.id,
                "method": ("outside_native_hulls_and_bounded_sample_edges" if sample_range is not None
                           else "outside_native_hulls_and_sample_edges"),
                "emission_time_range_s": [float(self.time[0]), float(self.time[-1])],
                "emission_intervals_s": [[float(self.time[i]), float(self.time[i+1])] for i in indices],
                "outside_query_scope_intervals_s": [[float(a), float(b)] for a, b in outside_query],
                "sound_speed_mps": float(c), "max_distance_m": float(radius),
                "max_sample_step_m": float(max_step), "query_radius_m": query_radius,
                "hull_distance_lower_m": hull_lower, "edge_distance_lower_m": float(distances.min()),
                "sample_count": len(sample_time), "affected_sample_edges": len(edges),
                "sample_range": None if sample_range is None else [sample_first, sample_last],
                "receiver_point_count": len(observers), "observer_box_ecef_m": [lower.tolist(), upper.tolist()]}

    def outside_observer_radius(self, observers, radius, c=343.0):
        """Prove all observers are beyond a validated track's range.

        Every instant lies at most half a native sample interval from one
        endpoint. Subsonic speed therefore bounds each ECEF component's
        displacement from an observed position by c * max_gap / 2. An axis
        separation larger than the radius proves the entire cubic trajectory
        and its sampled polyline cannot enter any observer's search sphere.
        """
        try:
            self.validate_motion_support(c)
        except AcousticSupportError:
            # A non-subsonic track can still be excluded without assuming a
            # speed limit: every cubic Hermite segment lies in the convex hull
            # of its four Bezier control points. Bound each hull and the whole
            # observer set by boxes, and exclude only if every pair is distant.
            observers = np.asarray(observers, dtype=float).reshape(-1, 3)
            if len(observers) and np.isfinite(radius) and np.all(np.isfinite(observers)):
                dt = np.diff(self.time)[:, None]
                velocity = self.speed_vector(self.time)
                p0, p1 = self.xyz[:-1], self.xyz[1:]
                controls = np.stack((p0, p0 + dt * velocity[:-1] / 3,
                                     p1 - dt * velocity[1:] / 3, p1), axis=1)
                if np.all(np.isfinite(controls)):
                    lo, hi = controls.min(axis=1), controls.max(axis=1)
                    lower, upper = observers.min(axis=0), observers.max(axis=0)
                    separation = np.maximum(np.maximum(lo - upper, lower - hi), 0)
                    scale = max(1., float(np.max(np.abs(controls))), float(np.max(np.abs(observers))), abs(radius))
                    margin = 1e-6 + 128 * np.finfo(float).eps * scale
                    with np.errstate(over="ignore", invalid="ignore"):
                        distances = np.linalg.norm(separation, axis=1)
                        threshold = radius + margin
                    if (np.isfinite(margin) and np.isfinite(threshold) and np.isfinite(distances).all()
                            and np.all(distances > threshold)):
                        return True
            raise
        if not np.isfinite(radius):
            return False
        scale = max(1.0, float(np.max(np.abs(self._xyz_min))),
                    float(np.max(np.abs(self._xyz_max))), abs(radius), abs(c * self._max_sample_gap))
        rounding = 64 * np.finfo(float).eps * scale
        margin = c * self._max_sample_gap / 2 + rounding
        lower = self._xyz_min - margin - radius - rounding
        upper = self._xyz_max + margin + radius + rounding
        return bool(np.all(np.any((observers < lower) | (observers > upper), axis=-1)))

    def _check_motion_support(self, receive_time, observer, c):
        """Reject possible arrivals from unsupported native emission intervals.

        Bezier position boxes conservatively enclose affected arrival times,
        without assuming monotonic propagation in an unsupported interval.
        """
        indices = self._unsupported_motion_intervals(c)
        if not len(indices):
            return
        # Tiny queries are cheaper in the original scalar loop than through
        # the temporary arrays and sorted-time lookup used by a larger batch.
        if len(indices) <= 2:
            return self._check_motion_support_scalar(indices, receive_time, observer, c)
        query = np.asarray(receive_time).ravel()
        if not len(query):
            return
        if not np.all(query[:-1] <= query[1:]):
            query = np.sort(query)
        emissions, arrivals = [], []
        # Fixed-size interval blocks bound the temporary Bezier arrays while
        # retaining every unsupported interval in its original order.
        for first in range(0, len(indices), _MOTION_SUPPORT_INTERVAL_BATCH):
            selected = indices[first:first+_MOTION_SUPPORT_INTERVAL_BATCH]
            endpoints = np.column_stack((selected, selected+1))
            times = self.time[endpoints]
            dt = (times[:, 1]-times[:, 0])[:, None]
            p0, p1 = self.xyz[selected], self.xyz[selected+1]
            velocity = self.speed_vector(times)
            controls = np.stack((p0, p0+dt*velocity[:, 0]/3,
                                 p1-dt*velocity[:, 1]/3, p1), axis=1)
            lo, hi = controls.min(axis=1), controls.max(axis=1)
            nearest = np.maximum(np.maximum(lo-observer, observer-hi), 0)
            farthest = np.maximum(np.abs(lo-observer), np.abs(hi-observer))
            # Keep the scalar norm reduction used by the original interval loop.
            # Array-axis reductions can round differently from the 1-D dot product.
            near = np.fromiter((np.linalg.norm(row) for row in nearest), nearest.dtype, len(selected))
            far = np.fromiter((np.linalg.norm(row) for row in farthest), farthest.dtype, len(selected))
            arrival_lo = times[:, 0]+near/c
            arrival_hi = times[:, 1]+far/c
            tolerance = np.maximum(1e-7, 4*np.abs(np.spacing(
                np.maximum(np.abs(arrival_lo), np.abs(arrival_hi)))))
            lower, upper = arrival_lo-tolerance, arrival_hi+tolerance
            # Any receive sample lies in an interval iff its first sample at or
            # above the lower endpoint is also at or below the upper endpoint.
            # This avoids an interval-by-sample comparison matrix and preserves
            # inclusive endpoints and repeated times.
            following = np.searchsorted(query, lower)
            overlaps = ((following < len(query)) &
                        (query[np.minimum(following, len(query)-1)] <= upper))
            if np.any(overlaps):
                emissions.extend(times[overlaps].tolist())
                arrivals.extend(np.column_stack((lower[overlaps], upper[overlaps])).astype(float).tolist())
        if emissions:
            raise AcousticSupportError(self.id, "non_subsonic_motion",
                emission_intervals_s=emissions, receive_intervals_s=arrivals)

    def _check_motion_support_scalar(self, indices, receive_time, observer, c):
        emissions, arrivals = [], []
        for i in indices:
            dt = self.time[i+1]-self.time[i]
            p0, p1 = self.xyz[i:i+2]
            v0, v1 = self.speed_vector(self.time[i:i+2])
            controls = np.array([p0, p0+dt*v0/3, p1-dt*v1/3, p1])
            lo, hi = controls.min(axis=0), controls.max(axis=0)
            nearest = np.maximum(np.maximum(lo-observer, observer-hi), 0)
            farthest = np.maximum(np.abs(lo-observer), np.abs(hi-observer))
            arrival_lo = self.time[i]+np.linalg.norm(nearest)/c
            arrival_hi = self.time[i+1]+np.linalg.norm(farthest)/c
            tolerance = max(1e-7, 4*abs(np.spacing(max(abs(arrival_lo), abs(arrival_hi)))))
            if np.any((receive_time >= arrival_lo-tolerance) & (receive_time <= arrival_hi+tolerance)):
                emissions.append([float(self.time[i]), float(self.time[i+1])])
                arrivals.append([float(arrival_lo-tolerance), float(arrival_hi+tolerance)])
        if emissions:
            raise AcousticSupportError(self.id, "non_subsonic_motion",
                emission_intervals_s=emissions, receive_intervals_s=arrivals)

    def acoustic(self, receive_time, observer, c=343.0):
        """Return Doppler factor, emission time and range at receiver-clock times.

        Solve t_receive = t_emit + |x(t_emit)-observer|/c with Newton's method.
        Values outside the supported emission interval are NaN, never extrapolated.
        """
        t = np.asarray(receive_time, dtype=float)
        start, end = self.time[[0, -1]]
        arrival_start = start + np.linalg.norm(self.xyz[0] - observer) / c
        arrival_end = end + np.linalg.norm(self.xyz[-1] - observer) / c
        if (type(self) is Track and len(self.time)-1 <= _PACKED_KINEMATICS_MAX_INTERVALS
                and Track.position is _NATIVE_POSITION
                and Track._position_velocity is _NATIVE_POSITION_VELOCITY
                and "position" not in self.__dict__ and "_position_velocity" not in self.__dict__
                and _EMISSION_ITERATION_TOLERANCE_S == 1e-7):
            result = _try_native_acoustic(self, t, observer, c, start, end,
                arrival_start, arrival_end, _packed_kinematics)
            if result is not None:
                return result
        supported = (t >= arrival_start) & (t <= arrival_end)
        initial = self.position(np.clip(t, start, end))
        tau = np.clip(t - np.linalg.norm(initial - observer, axis=-1) / c, start, end)
        for _ in range(10):
            position, velocity = self._position_velocity(tau)
            delta = position - observer
            distance = np.linalg.norm(delta, axis=-1)
            radial = np.sum(delta * velocity, axis=-1) / distance
            derivative = 1 + radial / c
            if np.any(derivative <= 0):
                raise AcousticSupportError(self.id, "non_monotone_arrival")
            correction = (tau - t + distance / c) / derivative
            updated = np.clip(tau - correction, start, end)
            unchanged = np.array_equal(tau, updated)
            tau = updated
            # A fixed point at Unix-epoch precision would repeat identically
            # even when its unrounded correction exceeds the tolerance.
            if np.max(np.abs(correction)) < _EMISSION_ITERATION_TOLERANCE_S or unchanged:
                break
        # An unchanged Newton iterate already has these exact values. Otherwise
        # evaluate the final updated time, including tolerance/iteration exits.
        if not unchanged:
            position, velocity = self._position_velocity(tau)
            delta = position - observer
            distance = np.linalg.norm(delta, axis=-1)
            radial = np.sum(delta * velocity, axis=-1) / distance
        factor = c / (c + radial)
        self._check_motion_support(t, observer, c)
        arrival_derivative = 1+radial/c
        if np.any(arrival_derivative <= 0):
            raise AcousticSupportError(self.id, "non_monotone_arrival")
        remaining_correction = (tau-t+distance/c)/arrival_derivative
        tolerance = np.maximum(1e-7, 4*np.abs(np.spacing(t)))
        if np.any(supported & (~np.isfinite(remaining_correction) | (np.abs(remaining_correction) > tolerance))):
            raise AcousticSupportError(self.id, "emission_solver_not_converged")
        return tuple(np.where(supported, a, np.nan) for a in (factor, tau, distance))

    def closest_approaches(self, observer, max_distance=np.inf, c=343.0, step=2.0):
        """All interior local 3-D minima, including repeated passes by one flight."""
        self.validate_motion_support(c)
        sample = np.unique(np.r_[np.arange(self.time[0], self.time[-1], step), self.time[-1]])
        range2 = np.sum((self.position(sample) - observer) ** 2, axis=-1)
        indices = find_peaks(-range2)[0]
        brackets = [(sample[i-1], sample[i], sample[i+1]) for i in indices]
        # find_peaks excludes the first and last sample. An interior minimum
        # can nevertheless lie between an endpoint and its neighbour, while
        # the endpoint is the smallest sampled range. Check the range-rate
        # signs before optimizing those intervals; an endpoint itself is not
        # evidence of a complete passage.
        edges = []
        if range2[0] <= range2[1]:
            edges.append((sample[0], sample[1]))
        if range2[-1] <= range2[-2] and (len(sample) > 2 or not edges):
            edges.append((sample[-2], sample[-1]))
        for lo, hi in edges:
            t = np.array([lo, hi])
            radial = np.sum((self.position(t) - observer) * self.speed_vector(t), axis=-1)
            if radial[0] < 0 < radial[1]:
                brackets.append((lo, lo, hi))
        events = []
        for lo, mid, hi in brackets:
            # Optimize relative time: scipy's absolute-time stopping term
            # is too loose when Unix timestamps are around 1.8 billion seconds.
            result = minimize_scalar(lambda dt: np.sum((self.position(mid + dt) - observer) ** 2),
                                     bounds=(lo - mid, hi - mid),
                                     method="bounded", options={"xatol": 0.001})
            t = mid + result.x
            distance = np.sqrt(result.fun)
            if distance <= max_distance:
                events.append({"flight_id": self.id, "emission_time": float(t),
                               "receive_time": float(t + distance / c),
                               "distance_m": float(distance),
                               "speed_mps": float(np.linalg.norm(self.speed_vector(t)))})
        return sorted(events, key=lambda event: event["emission_time"])


_NATIVE_POSITION = Track.position
_NATIVE_POSITION_VELOCITY = Track._position_velocity
_CANONICAL_CPA_MOTION_FUNCTIONS = (Track.position, Track.speed_vector,
    Track._unsupported_motion_intervals, Track.validate_motion_support)
_CANONICAL_ACOUSTIC_FUNCTIONS = (Track.acoustic, Track.position, Track.speed_vector,
    Track._position_velocity, Track._check_motion_support, Track._check_motion_support_scalar,
    Track._unsupported_motion_intervals, _try_native_acoustic, _packed_kinematics)


def straight_doppler(t_receive, f0, speed, slant, cpa_receive, c=343.0):
    """Exact retarded-time solution for a constant-speed straight flyover."""
    # Relative emission t=0 is geometric CPA; its sound arrives at slant/c.
    u = np.asarray(t_receive) - cpa_receive + slant / c
    beta = speed / c
    tau = (u - np.sqrt((beta * u) ** 2 + (1 - beta ** 2) * (slant / c) ** 2)) / (1 - beta ** 2)
    radial = speed ** 2 * tau / np.sqrt(slant ** 2 + (speed * tau) ** 2)
    return f0 * c / (c + radial)

