"""Geometric event filters, silence constraints and locked multi-event scoring."""
from collections import OrderedDict
from itertools import product
import math

import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar
from scipy.signal import find_peaks
from scipy.spatial import cKDTree

from .geometry import AcousticSupportError, Track, _CANONICAL_ECEF, ecef
from .matching import _CANONICAL_NATIVE_EVENT_VALIDATOR, _native_validate_ridge_events
from .native_cpa import try_cpa_edges

_CPA_COLUMNS = ["point_id", "flight_id", "emission_time", "receive_time", "distance_m", "speed_mps"]
_CPA_DTYPE = np.dtype([(name, "int64" if name == "point_id" else object if name == "flight_id" else "float64")
                       for name in _CPA_COLUMNS])
_EMPTY_CPA_TABLE = pd.DataFrame(columns=_CPA_COLUMNS)


def cpa_table_dense(tracks, points, max_distance, c=343.0, step_s=2.0, chunk_size=256):
    """Vectorized piecewise-linear CPA prefilter; supports repeated local passes.

    This is the inexpensive first stage. Final scoring evaluates the interpolated
    trajectory and retarded emission time directly, rather than these CPA samples.
    """
    observers = ecef(points.lat.to_numpy(),points.lon.to_numpy(),points.elev_m.to_numpy())
    rows = []
    for track in tracks:
        track.validate_motion_support(c)
        t = np.unique(np.r_[np.arange(track.time[0],track.time[-1],step_s),track.time[-1]])
        pos = track.position(t)
        # Prune trajectory segments outside this observer tile before forming a
        # point-by-segment tensor. Keep original segment endpoints/times so a gap
        # between retained pieces cannot invent a straight-line flight.
        lower=observers.min(axis=0)-max_distance
        upper=observers.max(axis=0)+max_distance
        keep=np.all((np.minimum(pos[:-1],pos[1:])<=upper)&
                    (np.maximum(pos[:-1],pos[1:])>=lower),axis=1)
        if not np.any(keep):
            continue
        first,last=pos[:-1][keep],pos[1:][keep]
        t0,t1=t[:-1][keep],t[1:][keep]
        delta = last-first
        length2 = np.sum(delta**2,axis=1)
        endpoints=np.concatenate((first,last))
        near_box=np.all((observers>=endpoints.min(axis=0)-max_distance)&
                        (observers<=endpoints.max(axis=0)+max_distance),axis=1)
        nearby=np.flatnonzero(near_box)
        supported = np.isfinite(cKDTree(endpoints).query(observers[nearby],
            distance_upper_bound=max_distance+np.sqrt(length2.max()))[0])
        active=nearby[supported]
        for base in range(0,len(active),chunk_size):
            indices = active[base:base+chunk_size]
            obs = observers[indices]
            residual = first[None,:,:]-obs[:,None,:]
            fraction = np.clip(-np.einsum("bsi,si->bs",residual,delta)/np.maximum(length2,1e-12),0,1)
            dist2 = np.sum((residual+fraction[:,:,None]*delta[None,:,:])**2,axis=2)
            # A zero-velocity segment or path endpoint is not evidence of a full
            # flyover. Keep interior minima and deduplicate adjoining segments.
            for j in range(len(obs)):
                peaks = find_peaks(-dist2[j],plateau_size=1)[0]
                if len(dist2[j]) > 2:
                    global_i = int(np.argmin(dist2[j]))
                    if 0 < global_i < len(dist2[j])-1:
                        peaks = np.unique(np.r_[peaks,global_i])
                last_time = -np.inf
                for k in peaks:
                    distance = np.sqrt(dist2[j,k])
                    when = t0[k]+fraction[j,k]*(t1[k]-t0[k])
                    if distance > max_distance or when-last_time < step_s*1.1:
                        continue
                    last_time = when
                    rows.append({"point_id":int(points.iloc[indices[j]].point_id),"flight_id":track.id,
                                 "emission_time":when,"receive_time":when+distance/c,
                                 "distance_m":distance,
                                 "speed_mps":np.linalg.norm(track.speed_vector(when))})
    return pd.DataFrame(rows,columns=["point_id","flight_id","emission_time","receive_time","distance_m","speed_mps"])


def cpa_table(tracks,points,max_distance,c=343.0,step_s=2.0,chunk_size=512, *, _sample_cache=None):
    """Spatial-radius CPA graph, retaining every local pass without dense tensors.

    The radius includes two complete sample steps beyond the audible envelope,
    so both neighbours of an in-range sampled minimum are present. Each minimum
    is refined on its two actual adjacent segments. The dense reference above
    provides an independent regression oracle for this search optimization.
    The private sample cache lives only across one track's bounded tile chunks.
    """
    observers=ecef(points.lat.to_numpy(),points.lon.to_numpy(),points.elev_m.to_numpy())
    point_ids=points.point_id.to_numpy(dtype=int)
    rows = _cpa_rows(tracks, observers, point_ids, max_distance, c, step_s, chunk_size,
                     _sample_cache=_sample_cache)
    if not rows:
        return _EMPTY_CPA_TABLE.copy()
    return pd.DataFrame(rows, columns=_CPA_COLUMNS)


def _cpa_rows(tracks, observers, point_ids, max_distance, c=343.0, step_s=2.0, chunk_size=512, *,
              _sample_cache=None):
    """Return the same ordered CPA records without allocating per-track tables.

    Tiled scoring already has ECEF observers and needs to isolate unsupported
    tracks. It can reuse those observers and materialize one table after the
    bounded per-track work instead of rebuilding a DataFrame for every query.
    """
    rows=[]
    for track in tracks:
        prepared = _PreparedCPATrack(track, c, step_s, _sample_cache=_sample_cache)
        rows.extend(prepared.raw_rows(observers, point_ids, max_distance, chunk_size))
    return _ordered_cpa_rows(rows, step_s)


def _bounded_cpa_sample_range(sample_time, receive_time_bounds, max_distance, c, step_s):
    """Return the exact global-lattice slice used by a bounded CPA query."""
    if receive_time_bounds is None:
        return None
    try:
        padding = 4 * step_s
        lower = receive_time_bounds[0] - max_distance / c - padding
        upper = receive_time_bounds[1] + padding
        if not (math.isfinite(lower) and math.isfinite(upper) and lower <= upper):
            return None
    except (IndexError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    first = max(0, int(np.searchsorted(sample_time, lower, side="left")) - 1)
    last = min(len(sample_time), int(np.searchsorted(sample_time, upper, side="right")) + 1)
    return first, last


class _PreparedCPATrack:
    """One validated track/step, reused only across one call's receiver chunks."""

    def __init__(self, track, c, step_s, *, _sample_cache=None, motion_observers=None, max_distance=None,
                 receive_time_bounds=None):
        self.motion_support = None
        motion_error = None
        try:
            track.validate_motion_support(c)
        except AcousticSupportError as error:
            if motion_observers is None or error.reason != "non_subsonic_motion" or _sample_cache is not None:
                raise
            motion_error = error
        self.track, self.c, self.step_s = track, c, step_s
        key = (id(track), step_s)
        sampled = None if _sample_cache is None else _sample_cache.get(key)
        if sampled is None:
            self.time = np.unique(np.r_[np.arange(track.time[0], track.time[-1], step_s), track.time[-1]])
            self.position = track.position(self.time)
            self.max_step = np.max(np.linalg.norm(np.diff(self.position, axis=0), axis=1))
            if _sample_cache is not None:
                # Match the public wrapper's existing tuple protocol. Only
                # sampling is reused: validation and sound speed stay local.
                _sample_cache[key] = (track, self.time, self.position, self.max_step)
        else:
            _, self.time, self.position, self.max_step = sampled
        if motion_error is not None:
            sample_range = _bounded_cpa_sample_range(self.time, receive_time_bounds, max_distance, c, step_s)
            self.motion_support = Track._cpa_interval_exclusion(track, motion_observers, max_distance, c,
                self.time, self.position, float(self.max_step), sample_range=sample_range)
            if self.motion_support is None:
                raise motion_error
            self.motion_support["sample_step_s"] = float(step_s)
            if sample_range is not None:
                self.motion_support["receive_time_bounds_s"] = tuple(map(float, receive_time_bounds))
        self.lower, self.upper = self.position.min(axis=0), self.position.max(axis=0)
        self.tree = None
        self._tree_sample_range = None

    def raw_rows(self, observers, point_ids, max_distance, chunk_size=512, *, receive_time_bounds=None):
        if self.motion_support is not None:
            support = self.motion_support
            lower, upper = np.asarray(support["observer_box_ecef_m"])
            expected_bounds = support.get("receive_time_bounds_s")
            try:
                requested_bounds = None if receive_time_bounds is None else tuple(map(float, receive_time_bounds))
            except (TypeError, ValueError):
                requested_bounds = None
            reported_intervals = list(support["emission_intervals_s"])
            reported_intervals.extend(support.get("outside_query_scope_intervals_s", []))
            if (not np.isfinite(max_distance) or max_distance > support["max_distance_m"]
                    or self.c != support["sound_speed_mps"] or self.step_s != support["sample_step_s"]
                    or (expected_bounds is not None and requested_bounds != expected_bounds)
                    or not np.isfinite(observers).all() or not np.all((observers >= lower) & (observers <= upper))):
                raise AcousticSupportError(self.track.id, "non_subsonic_motion",
                                           emission_intervals_s=reported_intervals)
        rows = []
        track, t, pos, c = self.track, self.time, self.position, self.c
        sample_range = _bounded_cpa_sample_range(t, receive_time_bounds, max_distance, c, self.step_s)
        sample_first, sample_last = sample_range or (0, len(t))
        if sample_range is not None:
            # Four steps retain projection neighbours and the preceding raw
            # CPA used by adjacent-row deduplication; the global lattice stays intact.
            t, pos = t[sample_first:sample_last], pos[sample_first:sample_last]
        if len(t) < 2:
            return rows
        near=np.flatnonzero(np.all((observers>=self.lower-max_distance)&
                                  (observers<=self.upper+max_distance),axis=1))
        if not len(near):
            return rows
        sample_range = (sample_first, sample_last)
        if self.tree is None or self._tree_sample_range != sample_range:
            self.tree = cKDTree(pos)
            self._tree_sample_range = sample_range
        tree = self.tree
        for base in range(0,len(near),chunk_size):
            chosen=near[base:base+chunk_size]
            obs=observers[chosen]
            neighbours=tree.query_ball_point(obs,max_distance+2*self.max_step,return_sorted=True)
            counts=np.fromiter((len(a) for a in neighbours),dtype=int,count=len(neighbours))
            if not counts.sum():continue
            sample=np.concatenate(neighbours).astype(int)
            projected = try_cpa_edges(pos, obs, counts, sample)
            if projected is None:
                observer_index=np.repeat(np.arange(len(obs)),counts)
                distance2=np.sum((pos[sample]-obs[observer_index])**2,axis=1)
                previous=np.r_[False,(observer_index[1:]==observer_index[:-1])&(sample[1:]==sample[:-1]+1)]
                following=np.r_[(observer_index[1:]==observer_index[:-1])&(sample[1:]==sample[:-1]+1),False]
                eligible = np.flatnonzero(counts >= 2)
                ends = np.cumsum(counts)[eligible]
                starts = ends - counts[eligible]
            # An endpoint sample can be the sampled minimum even when the
            # continuous spline turns strictly inside its adjacent interval.
            # Check the endpoint range-rate signs before optimizing the actual
            # spline; an endpoint alone is not evidence of a complete pass.
            for end in (False, True):
                if (end and sample_last != len(self.time)) or (not end and sample_first != 0):
                    continue  # A crop boundary is not a trajectory endpoint.
                if projected is None:
                    edge_indices = ends - 1 if end else starts
                    adjacent = edge_indices - 1 if end else edge_indices + 1
                    mask = ((sample[edge_indices] == (len(t)-1 if end else 0))
                            & (previous[edge_indices] if end else following[edge_indices])
                            & (distance2[edge_indices] <= distance2[adjacent]))
                    oi_edge = eligible[mask]
                else:
                    oi_edge = projected[0][end]
                if not len(oi_edge):
                    continue
                lo, hi = (t[-2], t[-1]) if end else (t[0], t[1])
                endpoints = pos[[-2, -1]] if end else pos[[0, 1]]
                velocities = track.speed_vector(np.array([lo, hi]))
                radial_start = np.sum((endpoints[0] - obs[oi_edge]) * velocities[0], axis=1)
                radial_end = np.sum((endpoints[1] - obs[oi_edge]) * velocities[1], axis=1)
                for j in np.flatnonzero((radial_start < 0) & (radial_end > 0)):
                    observer = obs[oi_edge[j]]
                    fit = minimize_scalar(lambda dt: np.sum((track.position(lo + dt) - observer) ** 2),
                                          bounds=(0, hi-lo), method="bounded", options={"xatol": 0.001})
                    when = lo + fit.x
                    if not fit.success or not lo < when < hi or not np.isfinite(fit.fun):
                        raise AcousticSupportError(track.id, "cpa_endpoint_solver_not_converged",
                                                   emission_intervals_s=[[float(lo), float(hi)]])
                    distance = np.sqrt(fit.fun)
                    if distance <= max_distance:
                        rows.append((int(point_ids[chosen[oi_edge[j]]]), track.id,
                                     float(when), float(when + distance/c), float(distance),
                                     float(np.linalg.norm(track.speed_vector(when)))))
            if projected is None:
                minima=(previous&following&(distance2<=np.r_[np.inf,distance2[:-1]])&
                        (distance2<np.r_[distance2[1:],np.inf]))
                indices=np.flatnonzero(minima)
                if not len(indices):continue
                k=sample[indices]
                oi=observer_index[indices]
                # Project onto both neighbouring polyline edges, never across a
                # missing interval or between unrelated flight passes.
                first=np.stack((pos[k-1],pos[k]),axis=1)
                last=np.stack((pos[k],pos[k+1]),axis=1)
                delta=last-first
                residual=first-obs[oi,None,:]
                fraction=np.clip(-np.sum(residual*delta,axis=2)/np.maximum(np.sum(delta**2,axis=2),1e-12),0,1)
                d2=np.sum((residual+fraction[:,:,None]*delta)**2,axis=2)
                edge=np.argmin(d2,axis=1)
                n=np.arange(len(k))
                distance=np.sqrt(d2[n,edge])
                start=np.where(edge==0,k-1,k)
                fraction = fraction[n, edge]
            else:
                _, oi, start, fraction, d2 = projected
                if not len(oi):continue
                distance=np.sqrt(d2)
            when=t[start]+fraction*(t[start+1]-t[start])
            speed=np.linalg.norm(track.speed_vector(when),axis=1)
            for j in np.flatnonzero(distance<=max_distance):
                rows.append((int(point_ids[chosen[oi[j]]]), track.id,
                             float(when[j]), float(when[j]+distance[j]/c),
                             float(distance[j]), float(speed[j])))
        return rows


def _ordered_cpa_rows(rows, step_s):
    if rows:
        # A CPA exactly at a sample can be proposed twice under roundoff.
        rows.sort(key=lambda row: (row[0], row[1], row[2]))
        duplicate_tolerance = step_s*1.1
        previous, retained = None, []
        for row in rows:
            # Match pandas' shifted int64 IDs (which become float64) and the
            # original float64-difference promotion against the caller's step.
            duplicate = (previous is not None and float(row[0]) == float(previous[0])
                         and row[1] == previous[1]
                         and np.float64(abs(row[2]-previous[2])) < duplicate_tolerance)
            if not duplicate:
                retained.append(row)
            # Match the original simultaneous adjacent-row mask, including a
            # chain of close proposals: compare with the preceding raw row.
            previous = row
        return retained
    return rows


def filter_event(table, event, time_origin=None, time_tolerance=5.0):
    speed, distance = event["speed_range_mps"],event["slant_range_m"]
    quality = event.get("quality", {})
    dv, dd = quality.get("speed_tolerance_mps", 0), quality.get("slant_tolerance_m", 0)
    dt = quality.get("cpa_tolerance_s", 0)
    if not all(np.isfinite(v) and v >= 0 for v in (dv, dd, dt)):
        raise ValueError("Proposal tolerances must be finite and nonnegative")
    keep = table.speed_mps.between(max(0, speed[0]-dv), speed[1]+dv) & table.distance_m.between(max(0, distance[0]-dd), distance[1]+dd)
    if time_origin is not None:
        keep &= np.abs(table.receive_time-(time_origin+event["cpa_receive_s"])) <= time_tolerance + dt
    return table.loc[keep]


def filter_association_event(table, event, offset_tolerance_s, time_origin, time_tolerance_s):
    """Filter with the same shared-clock interval used by association."""
    frame = filter_event(table, event)
    if time_origin is not None:
        start = frame.receive_time - event["cpa_receive_s"]
        tolerance = event.get("quality", {}).get("cpa_tolerance_s", 0) + offset_tolerance_s/2
        frame = frame[(start - tolerance <= time_origin + time_tolerance_s) &
                      (start + tolerance >= time_origin - time_tolerance_s)]
    return frame


def pair_candidates(table, events, offset_tolerance_s=5.0, time_origin=None,
                    time_tolerance_s=5.0, silence=None):
    filtered = [filter_association_event(table, event, offset_tolerance_s, time_origin, time_tolerance_s)
                for event in events]
    if any(frame.empty for frame in filtered):
        return [],{"per_event_survivors":[len(f) for f in filtered],"pairs_before_silence":0,"silence_rejected":0}
    group = [dict(tuple(frame.groupby("point_id"))) for frame in filtered]
    common = set.intersection(*(set(g) for g in group))
    all_by_point = dict(tuple(table.groupby("point_id")))
    candidates, before, rejected = [],0,0
    for point_id in sorted(common):
        for combination in product(*(list(g[point_id].to_dict("records")) for g in group)):
            anchor = combination[0]["receive_time"]
            starts = np.array([row["receive_time"]-event["cpa_receive_s"] for row,event in zip(combination,events)])
            tolerance = np.array([event.get("quality", {}).get("cpa_tolerance_s", 0) for event in events]) + offset_tolerance_s/2
            lower, upper = float(np.max(starts-tolerance)), float(np.min(starts+tolerance))
            if time_origin is not None:
                lower, upper = max(lower,time_origin-time_tolerance_s), min(upper,time_origin+time_tolerance_s)
            if lower > upper:
                continue
            if len(combination)>1 and len({(r["flight_id"],round(r["emission_time"],1)) for r in combination})<len(combination):
                continue
            before += 1
            if silence and len(combination)>1:
                threshold = max(row["distance_m"] for row in combination)*silence.get("distance_multiplier",1.0)
                margin = silence.get("edge_margin_s",60)
                between = all_by_point[point_id]
                between = between[(between.receive_time>anchor+margin)&
                                  (between.receive_time<combination[-1]["receive_time"]-margin)&
                                  (between.distance_m<=threshold)]
                # Only the selected actual passes are excluded, not every future
                # pass by an aircraft with the same flight ID.
                for selected in combination:
                    is_selected = (between.flight_id.eq(selected["flight_id"]) &
                                   between.emission_time.eq(selected["emission_time"]))
                    between = between.loc[~is_selected]
                if len(between):
                    rejected += 1
                    continue
            candidates.append({"point_id":point_id,"passes":combination,
                               "recording_start_bounds_s":[lower,upper]})
    return candidates,{"per_event_survivors":[len(f) for f in filtered],
                       "pairs_before_silence":before,"silence_rejected":rejected,"survivors":len(candidates)}


def event_score(track, observer, spectrum, ridge, weight, start_time, f0_bounds, mode, c=343.0,
                frequency_step=0.2):
    # The same receiver-clock time is used for all events. No per-event time fit.
    times = spectrum.time
    factor,_,_ = track.acoustic(start_time+times,observer,c)
    valid = np.isfinite(factor)
    if np.mean(valid)<0.95:
        return -np.inf,np.nan
    if mode=="ridge":
        valid &= np.isfinite(ridge)
        if np.sum(valid)<10:
            return -np.inf,np.nan
        w = np.maximum(np.asarray(weight)[valid],0.05)
        f0 = np.clip(np.sum(w*factor[valid]*ridge[valid])/np.sum(w*factor[valid]**2),*f0_bounds)
        rmse = np.sqrt(np.average((f0*factor[valid]-ridge[valid])**2,weights=w))
        return -float(rmse),float(f0)
    from .spectral_grid import best_frequency
    return best_frequency(spectrum, times, factor, f0_bounds, frequency_step)


def _ridge_trial_classification(result, events, association):
    """Classify recorded core fits, without changing their score or rejection rule.

    Missing support and numerical failures are unknown. A supported finite fit
    can be rejected only by the existing association residual thresholds.
    """
    def finite(value):
        if type(value) is float:
            return math.isfinite(value)
        return (isinstance(value, (int, float, np.integer, np.floating))
                and not isinstance(value, (bool, np.bool_)) and np.isfinite(value))

    diagnostics = result.get("event_diagnostics")
    scores, frequencies = result.get("event_scores"), result.get("f0_hz")
    if (not isinstance(diagnostics, list) or not isinstance(scores, list) or not isinstance(frequencies, list)
            or not len(diagnostics) == len(scores) == len(frequencies) == len(events)):
        return "unknown", "missing_event_fit_evidence"
    fields = ("score", "f0_hz", "source_drift_hz", "source_drift_hz_s",
              "source_reference_emission_time_s", "source_span_s", "drift_bound_hz", "rmse_hz", "outlier_fraction")
    # The optional native path recognizes only valid evidence in plain core
    # containers. Invalid or custom inputs retain the exact Python validation
    # below, including error precedence and subclass/numeric-type behavior.
    validated = (_native_validate_ridge_events is not None and
                 _native_validate_ridge_events(diagnostics, scores, frequencies, events))
    pending = () if validated else zip(diagnostics, scores, frequencies, events)
    for diagnostic, score, frequency, event in pending:
        if (not isinstance(diagnostic, dict) or not finite(diagnostic.get("support_fraction"))
                or diagnostic["support_fraction"] != 1.0):
            return "unknown", "incomplete_acoustic_support"
        if (not all(finite(diagnostic.get(name)) for name in fields)
                or not finite(score) or not finite(frequency)):
            return "unknown", "nonfinite_fit_evidence"
        count, independent = diagnostic.get("sample_count"), diagnostic.get("independent_sample_count")
        if (type(count) is not int or type(independent) is not int or not 1 <= independent <= count
                or count < 10 or diagnostic["source_span_s"] <= 0 or diagnostic["drift_bound_hz"] < 0
                or diagnostic["rmse_hz"] < 0 or not 0 <= diagnostic["outlier_fraction"] <= 1
                or diagnostic["f0_hz"] <= 0 or diagnostic["score"] > 0):
            return "unknown", "invalid_fit_evidence"
        if "optimizer_success" in diagnostic and diagnostic["optimizer_success"] is not True:
            return "unknown", "failed_optimizer"
        if score != diagnostic["score"] or frequency != diagnostic["f0_hz"]:
            return "unknown", "inconsistent_event_fit_evidence"
        bounds = event.get("f0_range_hz")
        fraction = event.get("quality", {}).get("source_drift_fraction", 0.02)
        if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2
                or not all(finite(value) for value in bounds) or not 0 < bounds[0] <= bounds[1]
                or not bounds[0] <= frequency <= bounds[1]
                or not finite(fraction) or not 0 <= fraction <= 0.1):
            return "unknown", "fit_outside_declared_frequency_domain"
        drift, cap = diagnostic["source_drift_hz"], diagnostic["drift_bound_hz"]
        if not bounds[0] * fraction <= cap <= bounds[1] * fraction or abs(drift) > cap:
            return "unknown", "fit_outside_declared_drift_domain"
        if abs(diagnostic["source_drift_hz_s"] * diagnostic["source_span_s"] - drift) > 1e-12 + 1e-12 * abs(drift):
            return "unknown", "inconsistent_source_drift"
    if "optimizer_success" in result and result["optimizer_success"] is not True:
        return "unknown", "failed_optimizer"
    residual = float(sum(scores))
    if not finite(residual):
        return "unknown", "nonfinite_residual_score"
    if not association:
        if (result.get("score_kind") == "negative_robust_ridge_loss_hz"
                and finite(result.get("score")) and result["score"] == residual):
            return "evaluated", None
        return "unknown", "inconsistent_residual_score"
    from .matching import _frequency_scale
    limits = [3 * _frequency_scale(event) for event in events]
    if (result.get("score_kind") != "matched_group_support_minus_normalized_residual"
            or not finite(result.get("residual_score")) or result["residual_score"] != residual
            or not all(finite(v) and v > 0 for v in limits)):
        return "unknown", "inconsistent_association_evidence"
    supported = all(-score <= limit for score, limit in zip(scores, limits))
    if supported:
        expected = float(sum(1 - (-score) / limit for score, limit in zip(scores, limits)))
        if finite(result.get("score")) and result["score"] == expected:
            return "evaluated", None
    elif result.get("score") == -np.inf:
        return "rejected", "association_residual_limit"
    return "unknown", "unexplained_association_score"



_CEILING_FLOAT_FIELDS = (
    "support_fraction", "score", "f0_hz", "source_drift_hz", "source_drift_hz_s",
    "source_reference_emission_time_s", "source_span_s", "drift_bound_hz", "rmse_hz", "outlier_fraction",
)


def _association_event_ceiling(diagnostic, score, frequency, event, limit):
    """Reuse a validated ceiling only for the same complete scalar evidence.

    Fit dictionaries are copied for each association. Content keys preserve
    mutation checks while avoiding repeated validation of the same clock fit.
    Noncanonical numeric types retain the original validation and arithmetic.
    """
    if _native_validate_ridge_events is not _CANONICAL_NATIVE_EVENT_VALIDATOR:
        # A custom hook can change validity without changing scalar evidence.
        # Preserve the full classifier and do not reuse an earlier hook's key.
        return _association_event_ceiling_uncached(diagnostic, score, frequency, event, limit)
    if (_native_validate_ridge_events is not None
            and _ridge_trial_classification is _CANONICAL_CEILING_FUNCTIONS[2]
            and _association_event_ceiling_uncached is _CANONICAL_CEILING_FUNCTIONS[1]
            and type(limit) is float and math.isfinite(limit) and limit > 0
            and _native_validate_ridge_events([diagnostic], [score], [frequency], [event])):
        if -score > limit:
            return -np.inf
        return float(np.nextafter(1 - (-score) / limit, np.inf))
    from .matching import _ridge_fit_cache
    cache = _ridge_fit_cache.get()
    if (cache is None or cache.max_entries <= 0 or type(diagnostic) is not dict
            or type(event) is not dict or type(event.get("quality", {})) is not dict):
        return _association_event_ceiling_uncached(diagnostic, score, frequency, event, limit)
    bounds = event.get("f0_range_hz")
    if type(bounds) not in (list, tuple) or len(bounds) != 2:
        return _association_event_ceiling_uncached(diagnostic, score, frequency, event, limit)
    values = tuple(diagnostic.get(name) for name in _CEILING_FLOAT_FIELDS)
    fraction = event.get("quality", {}).get("source_drift_fraction", 0.02)
    counts = (diagnostic.get("sample_count"), diagnostic.get("independent_sample_count"))
    optimizer = diagnostic.get("optimizer_success", True)
    if (not all(type(value) is float for value in (*values, score, frequency, limit, *bounds, fraction))
            or not all(type(value) is int for value in counts) or type(optimizer) is not bool):
        return _association_event_ceiling_uncached(diagnostic, score, frequency, event, limit)
    key = (_ridge_trial_classification, values, counts, optimizer, score, frequency,
           tuple(bounds), fraction, limit)
    entries = cache.ceiling_entries
    previous = entries.get(key)
    if previous is not None:
        entries.move_to_end(key)
        return previous
    result = _association_event_ceiling_uncached(diagnostic, score, frequency, event, limit)
    entries[key] = result
    if len(entries) > cache.max_entries:
        entries.popitem(last=False)
    return result


def _association_event_ceiling_uncached(diagnostic, score, frequency, event, limit):
    """Validate one fitted group's contribution before tightening a ceiling."""
    single = {"score_kind": "negative_robust_ridge_loss_hz", "score": score,
              "event_scores": [score], "f0_hz": [frequency], "event_diagnostics": [diagnostic]}
    classification, _ = _ridge_trial_classification(single, [event], False)
    if classification != "evaluated":
        return 1.0
    if -score > limit:
        return -np.inf
    # Preserve the scorer's operation order and upward rounding, including
    # noncanonical numeric types handled by the original general bound helper.
    return float(np.nextafter(1 - (-score) / limit, np.inf))


_CANONICAL_CEILING_FUNCTIONS = (_association_event_ceiling, _association_event_ceiling_uncached,
                              _ridge_trial_classification)


def _association_trial_bound(result, events):
    """Bound an incompletely supported association at one shared clock.

    The declared score is sum(1 - loss_i / limit_i), or -inf if any event
    fails its residual limit. Each nonnegative unknown loss contributes at
    most one. Only a fully validated core fit can tighten that ceiling or
    establish a residual rejection. Missing support is still unknown.
    """
    kind = "matched_group_support_minus_normalized_residual"
    if not events or not isinstance(result, dict) or result.get("score_kind") != kind:
        return None
    from .matching import _frequency_scale
    try:
        limits = [3 * _frequency_scale(event) for event in events]
    except (TypeError, ValueError, KeyError):
        return None
    if not all(np.isfinite(value) and value > 0 for value in limits):
        return None
    maximum = float(len(events))
    diagnostics, scores, frequencies = (result.get(name) for name in ("event_diagnostics", "event_scores", "f0_hz"))
    if (not all(isinstance(values, list) and len(values) == len(events)
                for values in (diagnostics, scores, frequencies))
            or result.get("optimizer_success", True) is not True):
        return maximum
    contributions = []
    for diagnostic, score, frequency, event, limit in zip(diagnostics, scores, frequencies, events, limits):
        contribution = _association_event_ceiling(diagnostic, score, frequency, event, limit)
        if contribution == -np.inf:
            return -np.inf
        contributions.append(contribution)
    upper = 0.0
    for contribution in contributions:
        upper = float(np.nextafter(upper + contribution, np.inf))
    return min(maximum, upper)


def _validate_score_ceiling_inputs(events, measurements):
    """Do not hide malformed fit inputs behind an unperformed-fit certificate."""
    from .matching import _frequency_scale, _prepared_score_tokens, _ridge_fit_inputs
    if _prepared_score_tokens(measurements, events) is not None:
        return
    for event, measurement in zip(events, measurements):
        value = _frequency_scale(event)
        if not isinstance(value, (int, float, np.number)) or isinstance(value, (bool, np.bool_)):
            raise ValueError("Frequency loss scales must be finite and positive")
        try:
            scale = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("Frequency loss scales must be finite and positive") from error
        if not np.isfinite(scale) or scale <= 0 or not np.isfinite(3*scale):
            raise ValueError("Frequency loss scales must be finite and positive")
        _ridge_fit_inputs(measurement, scale, event.get("quality", {}).get("source_drift_fraction", 0.02))
        bounds = event.get("f0_range_hz")
        if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2
                or not all(isinstance(value, (int, float, np.number)) and not isinstance(value, (bool, np.bool_))
                           and np.isfinite(value) for value in bounds)
                or not 0 < bounds[0] <= bounds[1]):
            raise ValueError("Source frequency bounds must be finite, positive and ordered")


def _screen_association_trial(tracks, events, measurements, observer, start, c, acoustic_at, floor, dual_at=None, first_fit=None):
    """Fit groups until a conservative residual ceiling cannot reach the floor.

    Unperformed groups each contribute at most one. The existing validated-fit
    bound rounds upward and never uses missing evidence as a rejection.
    """
    from .matching import score_ridges
    diagnostics = []
    missing = {"score": -np.inf, "f0_hz": np.nan, "support_fraction": 0.0}
    for index, (track, event, measurement) in enumerate(zip(tracks, events, measurements)):
        if dual_at is not None:
            upper = dual_at(index)
            if upper is not None:
                return None, upper
        acoustic = None if acoustic_at is None else [acoustic_at(index)]
        if index == 0 and first_fit is not None:
            single = {"event_diagnostics": [first_fit]}
        else:
            single = score_ridges([track], [event], [measurement], observer, start, c, acoustic_results=acoustic)
        diagnostics.extend(single["event_diagnostics"])
        remaining = len(events) - len(diagnostics)
        if remaining:
            partial = diagnostics + [missing] * remaining
            bound = _association_trial_bound({
                "score_kind": "matched_group_support_minus_normalized_residual",
                "event_scores": [d["score"] for d in partial],
                "f0_hz": [d["f0_hz"] for d in partial], "event_diagnostics": partial}, events)
            if bound is not None and bound < floor:
                return None, bound
    return {"score": float(sum(d["score"] for d in diagnostics)),
            "event_scores": [d["score"] for d in diagnostics],
            "f0_hz": [d["f0_hz"] for d in diagnostics], "event_diagnostics": diagnostics,
            "score_kind": "negative_robust_ridge_loss_hz"}, None


def _ridge_result(diagnostics):
    return {"score": float(sum(d["score"] for d in diagnostics)),
            "event_scores": [d["score"] for d in diagnostics],
            "f0_hz": [d["f0_hz"] for d in diagnostics], "event_diagnostics": diagnostics,
            "score_kind": "negative_robust_ridge_loss_hz"}


def _batched_ridge_trials(tracks, events, measurements, observer, starts, c,
                         acoustic_batches, score_floor, use_dual, legacy_first_batch=False):
    """Batch only clocks that survive the preceding event's existing screen.

    Event order within each clock is unchanged. A later event sees precisely the
    clocks that would reach it in the scalar loop. Unsupported batches retain
    scalar fits, including each clock's first acoustic-support error.
    """
    from .acoustic_screen import ridge_trial_rejections
    from .batch_ridge import first_event_fits, _first_event_fits_with_ceilings
    from .matching import _frequency_scale, fit_ridge
    from .trial_prefix import request

    reusable = request(tracks, events, measurements, observer, starts, c, acoustic_batches,
                       score_floor, use_dual, legacy_first_batch)
    if reusable is not None:
        previous = reusable.lookup()
        if previous is not None:
            return previous

    diagnostics = [[] for _ in starts]
    outcomes = [None] * len(starts)
    active = list(range(len(starts)))
    rejected_count = floor_count = 0
    prefixes = np.zeros(len(starts), dtype=np.float64)
    limits = [3 * _frequency_scale(event) for event in events] if score_floor is not None else []
    bound_enabled = bool(limits) and all(np.isfinite(value) and value > 0 for value in limits)
    batches = iter(acoustic_batches)
    processed = 0
    for index, (track, event, measurement) in enumerate(zip(tracks, events, measurements)):
        if not active:
            break
        batch = next(batches)
        processed = index + 1
        if use_dual and batch is not None:
            rejected, upper = ridge_trial_rejections(measurement, event, *batch,
                score_floor=score_floor, group_count=len(events))
            indices = np.asarray(active, dtype=np.intp)
            rejected = rejected[indices]
            upper = upper[indices]
            finite_upper = ~rejected & np.isfinite(upper)
            rejected_count += int(np.count_nonzero(rejected))
            floor_count += int(np.count_nonzero(finite_upper))
            for trial in indices[rejected].tolist():
                outcomes[trial] = (None, -np.inf, None)
            for trial, bound in zip(indices[finite_upper].tolist(), upper[finite_upper].tolist()):
                outcomes[trial] = (None, float(bound), None)
            eligible = indices[~(rejected | finite_upper)].tolist()
        else:
            eligible = active
        fitted = fitted_ceilings = None
        remaining = len(events) - index - 1
        if batch is not None and len(eligible) >= 4:
            arguments = (measurement, event, batch[0][eligible], batch[1][eligible])
            legacy = legacy_first_batch and index == 0
            if remaining and bound_enabled:
                fitted, fitted_ceilings = _first_event_fits_with_ceilings(
                    *arguments, limits[index], legacy_arithmetic=legacy)
            else:
                fitted = first_event_fits(*arguments, legacy_arithmetic=legacy)
        if fitted_ceilings is not None:
            # Only the unchanged core validator supplies this complete vector.
            # Custom hooks and numerical policies keep the scalar path below.
            for trial, fit in zip(eligible, fitted):
                diagnostics[trial].append(fit)
            indices = np.asarray(eligible, dtype=np.intp)
            contributions = np.asarray(fitted_ceilings, dtype=np.float64)
            rejected = contributions == -np.inf
            for trial in indices[rejected].tolist():
                outcomes[trial] = (None, -np.inf, None)
            indices = indices[~rejected]
            prefixes[indices] = np.nextafter(prefixes[indices] + contributions[~rejected], np.inf)
            bounds = prefixes[indices]
            # From a nonnegative prefix, each remaining +1/upward-rounding
            # step leaves a bound at least as large as its step count. The
            # final cap is also >= remaining, so none can fall below this
            # floor. Check prefixes explicitly to preserve earlier custom fits.
            if score_floor <= remaining and np.all(bounds >= 0.0):
                active = indices.tolist()
                continue
            # Keep each addition and upward rounding in the scalar order. In
            # particular, summing losses first or rounding only once is unsafe.
            for _ in range(remaining):
                bounds = np.nextafter(bounds + 1.0, np.inf)
            # Python min(maximum, NaN) retains maximum, including a NaN prefix
            # supplied by an earlier custom fitter before core validation resumes.
            np.fmin(float(len(events)), bounds, out=bounds)
            below_floor = bounds < score_floor
            for trial, bound in zip(indices[below_floor].tolist(), bounds[below_floor].tolist()):
                outcomes[trial] = (None, bound, None)
            active = indices[~below_floor].tolist()
            continue
        next_active = []
        for fit_index, trial in enumerate(eligible):
            try:
                if fitted is None:
                    acoustic = None if batch is None else (batch[0][trial], batch[1][trial])
                    fit = fit_ridge(track, observer, measurement, starts[trial],
                        event["f0_range_hz"], c, _frequency_scale(event),
                        event.get("quality", {}).get("source_drift_fraction", 0.02),
                        acoustic_result=acoustic)
                else:
                    fit = fitted[fit_index]
            except AcousticSupportError as error:
                outcomes[trial] = (None, None, error)
                continue
            diagnostics[trial].append(fit)
            if remaining:
                if bound_enabled:
                    contribution = (fitted_ceilings[fit_index] if fitted_ceilings is not None else
                        _association_event_ceiling(fit, fit["score"], fit["f0_hz"], event, limits[index]))
                    if contribution == -np.inf:
                        outcomes[trial] = (None, -np.inf, None)
                        continue
                    # Reuse the already validated prefix. Add unperformed
                    # groups one at a time with the identical outward rounding;
                    # capping a prefix or summing losses first changes the bound.
                    prefixes[trial] = math.nextafter(float(prefixes[trial]) + contribution, math.inf)
                    bound = float(prefixes[trial])
                    for _ in range(remaining):
                        bound = math.nextafter(bound + 1.0, math.inf)
                    bound = min(float(len(events)), bound)
                    if bound < score_floor:
                        outcomes[trial] = (None, bound, None)
                        continue
                next_active.append(trial)
            else:
                outcomes[trial] = (_ridge_result(diagnostics[trial]), None, None)
        active = next_active
    if reusable is not None:
        reusable.remember(processed, outcomes, rejected_count, floor_count)
    return outcomes, rejected_count, floor_count


def _candidate_observer(lat, lon, height):
    """Reuse the scalar ECEF operation for private, ordinary receiver inputs."""
    from .matching import _NATIVE_RIDGE_ERROR_POLICY, _ridge_fit_cache

    cache = _ridge_fit_cache.get()
    if (cache is None or not cache.score_inputs or cache.max_entries < 1
            or ecef is not _CANONICAL_ECEF
            or not all(type(value) is float and math.isfinite(value) for value in (lat, lon, height))
            or not (-90 <= lat <= 90 and -180 <= lon <= 180 and abs(height) <= 1e7)
            or np.geterr() != _NATIVE_RIDGE_ERROR_POLICY):
        return ecef(lat, lon, height)
    entries = getattr(cache, "observer_entries", None)
    if entries is None:
        entries = cache.observer_entries = OrderedDict()
    # Hex preserves signed zero as well as every bit of each finite coordinate.
    key = lat.hex(), lon.hex(), height.hex()
    value = entries.get(key)
    if value is None:
        result = ecef(lat, lon, height)
        value = np.frombuffer(result.tobytes(), dtype=result.dtype).reshape(result.shape)
        entries[key] = value
    entries.move_to_end(key)
    while len(entries) > min(cache.max_entries, 256):
        entries.popitem(last=False)
    # Each scalar ECEF call used to return a fresh array. Keep that ownership
    # contract, including for a caller that mutates its observer later.
    return value.copy()


def score_candidate(candidate, point, tracks, events, measurements, mode="brightness",
                    time_origin=None, time_sweep_s=5.0, time_step_s=0.5, c=343.0,
                    score_floor=None, legacy_first_batch=False):
    """Score one hypothesis; a bounded score-floor result has no numeric score."""
    all_events, all_measurements = events, measurements
    association = "event_indices" in candidate
    association_fields = {}
    if association:
        if mode != "robust_ridge":
            raise ValueError("Partial-event association requires robust_ridge scoring")
        indices = candidate["event_indices"]
        if not indices or indices != sorted(set(indices)) or any(i < 0 or i >= len(events) for i in indices):
            raise ValueError("Association event indices must be unique, ordered and in range")
        association_fields = {"event_indices": indices,
            "event_ids": [events[i].get("event_id", str(i)) for i in indices],
            "unmatched_event_ids": [e.get("event_id", str(i)) for i, e in enumerate(events) if i not in indices]}
        events, measurements = [events[i] for i in indices], [measurements[i] for i in indices]
    if not events or len(events) != len(measurements) or len(events) != len(candidate["passes"]):
        raise ValueError("Need exactly one pass and measurement for each event")
    if mode not in ("brightness", "ridge", "robust_ridge"):
        raise ValueError(f"Unknown scoring mode: {mode}")
    if time_step_s <= 0 or time_sweep_s < 0:
        raise ValueError("Need positive clock step and nonnegative sweep")
    from .ridge_overlap import selected_ridge_conflicts
    shared_observations = selected_ridge_conflicts(events) if association else []
    if score_floor is not None:
        if not association or mode != "robust_ridge":
            raise ValueError("score_floor is only supported for robust_ridge associations")
        if isinstance(score_floor, (bool, np.bool_)):
            raise ValueError("score_floor must be finite")
        try:
            score_floor = float(score_floor)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("score_floor must be finite") from error
        if not np.isfinite(score_floor):
            raise ValueError("score_floor must be finite")
        _validate_score_ceiling_inputs(events, measurements)
    elif shared_observations:
        _validate_score_ceiling_inputs(events, measurements)
    observer = _candidate_observer(point.lat,point.lon,point.elev_m)
    # Derive one recording start from the first event; all other events retain
    # their true absolute geometry/time. This implements the locked offset.
    anchor = time_origin if time_origin is not None else candidate["passes"][0]["receive_time"]-events[0]["cpa_receive_s"]
    default_count = max(1,int(np.ceil(2*time_sweep_s/time_step_s)))+1
    bounded_clock = mode == "robust_ridge" and "recording_start_bounds_s" in candidate
    # The bounded clock below replaces this entire array. Retain its original
    # arithmetic/validation for unusual scalar types, ranges and error policies.
    ordinary_clock = (bounded_clock and type(time_sweep_s) in (float, int) and type(time_step_s) in (float, int)
                      and 0 <= time_sweep_s <= 86400 and 1e-6 <= time_step_s <= 86400
                      and default_count <= 4096
                      and np.geterr() == {"divide": "warn", "over": "warn", "under": "ignore", "invalid": "warn"})
    shifts = None if bounded_clock and ordinary_clock else np.linspace(-time_sweep_s,time_sweep_s,default_count)
    if bounded_clock:
        lower, upper = candidate["recording_start_bounds_s"]
        # CPA intervals propose a shared clock; the physical model may move it
        # within the same declared sweep used by legacy scoring.
        lower, upper = lower-time_sweep_s, upper+time_sweep_s
        if time_origin is not None:
            lower, upper = max(lower,anchor-time_sweep_s), min(upper,anchor+time_sweep_s)
        if lower > upper:
            shifts = []
        else:
            shifts = np.linspace(lower-anchor,upper-anchor,max(1,int(np.ceil((upper-lower)/time_step_s)))+1)
    # linspace(..., 2) produces the same trial twice for a fixed clock. Keep
    # one evaluation and report one trial; the fitted clock and score are
    # unchanged, including the first-shift tie rule.
    if len(shifts) > 1 and shifts[0] == shifts[-1]:
        shifts = shifts[:1]
    best = {"score":-np.inf}
    if mode == "robust_ridge":
        best["score_kind"] = ("matched_group_support_minus_normalized_residual" if association
                              else "negative_robust_ridge_loss_hz")
    assessment = {"complete": False, "evaluated_trials": 0, "rejected_trials": 0,
                  "unknown_trials": 0, "reasons": [], "scheduled_trials": len(shifts),
                  "trial_count_semantics": "disjoint_evaluated_rejected_unknown",
                  "empty_clock_domain": len(shifts) == 0}
    reasons = set()
    unknown_bounds = []
    partial_prunes = 0
    # Every accepted group contributes at most one; a strict comparison keeps ties eligible.
    floor_pruned = bool(score_floor is not None and len(shifts) > 0 and len(events) < score_floor)
    unresolved_overlap = bool(shared_observations and len(shifts) > 0)
    skip_fits = floor_pruned or unresolved_overlap
    if skip_fits:
        best["score"] = None
        assessment["unknown_trials"] = len(shifts)
    if floor_pruned:
        reasons.add("matched_group_count_below_score_floor")
    if unresolved_overlap:
        # Keep the joint assignment as an unresolved envelope, not a rejected
        # physical source combination. Its group count is only an upper bound.
        reasons.add("reused_ridge_observations")
        assessment["shared_ridge_observations"] = shared_observations
    selected_tracks = ([tracks[p["flight_id"]] for p in candidate["passes"]]
                       if mode == "robust_ridge" and not skip_fits else None)
    batches = None
    lazy_batches = None
    if mode == "robust_ridge" and not skip_fits and len(shifts) > 1:
        from .matching import _iter_acoustic_trials, batch_acoustic_trials
        if association and score_floor is not None:
            lazy_batches = iter(_iter_acoustic_trials(selected_tracks, measurements, observer, anchor + shifts, c))
            batches = []
        else:
            batches = batch_acoustic_trials(selected_tracks, measurements, observer, anchor + shifts, c)
    first_fits, first_batch_attempted = None, False
    dual_flags = {}
    dual_pruned = 0
    floor_dual_pruned = 0
    batched_trials = None
    # Bound the retained per-clock diagnostics; large products keep the scalar
    # traversal rather than allocating an unbounded event-by-clock table.
    if (mode == "robust_ridge" and not skip_fits and batches is not None
            and len(shifts) >= 4 and len(shifts) * len(events) <= 4096):
        batched_trials, dual_pruned, floor_dual_pruned = _batched_ridge_trials(
            selected_tracks, events, measurements, observer, anchor + shifts, c,
            lazy_batches if lazy_batches is not None else batches,
            score_floor if association else None,
            association and score_floor is not None and time_origin is None,
            legacy_first_batch and association and score_floor is not None and time_origin is None)
    for trial, shift in enumerate(() if skip_fits else shifts):
        if mode == "robust_ridge":
            from .matching import score_ridges
            def acoustic_at(index):
                if lazy_batches is not None:
                    while len(batches) <= index:
                        batches.append(next(lazy_batches))
                batch = batches[index]
                return None if batch is None else (batch[0][trial], batch[1][trial])
            precomputed = ([acoustic_at(index) for index in range(len(events))]
                           if batched_trials is None and batches is not None and lazy_batches is None else None)
            def dual_flags_at(index):
                if index not in dual_flags:
                    acoustic_at(index)
                    batch = batches[index]
                    if batch is None:
                        dual_flags[index] = (np.zeros(len(shifts), dtype=bool),
                                             np.full(len(shifts), np.nan))
                    else:
                        from .acoustic_screen import ridge_trial_rejections
                        dual_flags[index] = ridge_trial_rejections(measurements[index], events[index], *batch,
                                                                  score_floor=score_floor, group_count=len(events))
                return dual_flags[index]
            def dual_at(index):
                nonlocal dual_pruned, floor_dual_pruned
                rejections, bounds = dual_flags_at(index)
                rejected = bool(rejections[trial])
                dual_pruned += int(rejected)
                if rejected:
                    return -np.inf
                upper = bounds[trial]
                if np.isfinite(upper):
                    floor_dual_pruned += 1
                    return float(upper)
                return None
            try:
                if batched_trials is not None:
                    result, upper, error = batched_trials[trial]
                    if error is not None:
                        raise error
                    if result is None:
                        partial_prunes += 1
                        assessment["unknown_trials"] += 1
                        reasons.add("partial_group_score_below_retention_floor")
                        unknown_bounds.append(upper)
                        continue
                elif association and score_floor is not None:
                    if not first_batch_attempted and batches is not None and lazy_batches is not None and time_origin is None:
                        first_batch_attempted = True
                        acoustic_at(0)
                        if batches[0] is not None:
                            rejected, upper = dual_flags_at(0)
                            eligible = np.flatnonzero(~rejected & ~np.isfinite(upper))
                            if len(eligible) >= 4:
                                from .batch_ridge import first_event_fits
                                factors, emissions = batches[0]
                                fitted = first_event_fits(measurements[0], events[0],
                                                          factors[eligible], emissions[eligible],
                                                          legacy_arithmetic=legacy_first_batch)
                                if fitted is not None:
                                    first_fits = [None] * len(shifts)
                                    for index, fit in zip(eligible, fitted):
                                        first_fits[index] = fit
                    result, upper = _screen_association_trial(selected_tracks, events, measurements, observer,
                                                              anchor+shift, c, acoustic_at if batches is not None else None,
                                                              score_floor, dual_at if batches is not None and time_origin is None else None,
                                                              first_fits[trial] if first_fits is not None else None)
                    if result is None:
                        partial_prunes += 1
                        assessment["unknown_trials"] += 1
                        reasons.add("partial_group_score_below_retention_floor")
                        unknown_bounds.append(upper)
                        continue
                else:
                    result = score_ridges(selected_tracks, events, measurements, observer, anchor+shift, c,
                                          acoustic_results=precomputed)
            except AcousticSupportError as error:
                assessment["unknown_trials"] += 1
                reasons.add(str(error))
                unknown_bounds.append(None)
                continue
            if association:
                from .matching import association_score
                result = association_score(result, events)
            classification, reason = _ridge_trial_classification(result, events, association)
            if classification == "unknown":
                assessment["unknown_trials"] += 1
                reasons.add("incomplete_ridge_or_trajectory_support" if reason == "incomplete_acoustic_support" else reason)
                unknown_bounds.append(_association_trial_bound(result, events) if association else None)
            elif classification == "rejected":
                assessment["rejected_trials"] += 1
                reasons.add("acoustic_loss_exceeds_limit")
            else:
                assessment["evaluated_trials"] += 1
            if result["score"] > best["score"]:
                best = dict(result,recording_start_time_s=float(anchor+shift),time_adjustment_s=float(shift))
            continue
        scores,f0s = [],[]
        for event,measurement,flyover in zip(events,measurements,candidate["passes"]):
            spec,ridge,weight,_ = measurement
            score,f0 = event_score(tracks[flyover["flight_id"]],observer,spec,ridge,weight,
                                   anchor+shift,event["f0_range_hz"],mode,c,event.get("frequency_step_hz",0.2))
            scores.append(score)
            f0s.append(f0)
        total = float(np.sum(scores))
        if total>best["score"]:
            best = {"score":total,"event_scores":scores,"f0_hz":f0s,
                    "recording_start_time_s":float(anchor+shift),"time_adjustment_s":float(shift)}
    if mode == "robust_ridge":
        if dual_pruned:
            assessment["acoustic_bound_rejected_trials"] = dual_pruned
        if floor_dual_pruned:
            assessment["acoustic_floor_pruned_trials"] = floor_dual_pruned
        if partial_prunes and np.isfinite(best["score"]) and best["score"] >= score_floor:
            # Competitive modes retain their complete original diagnostics. Cached
            # fits are reused; skipped clocks are evaluated rather than relabelled.
            return score_candidate(candidate, point, tracks, all_events, all_measurements, mode,
                                   time_origin, time_sweep_s, time_step_s, c)
        if not len(shifts):
            reasons.add("no_clock_trials")
        assessment["complete"] = bool(len(shifts) and assessment["unknown_trials"] == 0)
        assessment["reasons"] = sorted(reasons)
        if association and assessment["unknown_trials"]:
            if skip_fits:
                assessment.update(
                    bounded_unknown_trials=len(shifts),
                    bounded_unknown_rejections=0,
                    unknown_score_upper_bound=float(len(events)),
                    unknown_score_bound_kind="matched_group_support_minus_normalized_residual",
                    unknown_score_bound_method="group_count_ceiling")
            else:
                finite_bounds = [value for value in unknown_bounds if value is not None and np.isfinite(value)]
                assessment.update(
                    bounded_unknown_trials=sum(value is not None for value in unknown_bounds),
                    bounded_unknown_rejections=sum(value == -np.inf for value in unknown_bounds if value is not None),
                    unknown_score_upper_bound=max(finite_bounds) if finite_bounds else None,
                    unknown_score_bound_kind="matched_group_support_minus_normalized_residual",
                    unknown_score_bound_method=("nonnegative_loss_validated_fits_and_acoustic_floor_dual"
                                                if floor_dual_pruned else
                                                "nonnegative_loss_validated_fits_and_acoustic_dual"
                                                if dual_pruned else "nonnegative_loss_and_validated_event_fits"))
                if partial_prunes:
                    assessment["retention_score_floor"] = score_floor
                    assessment["retention_pruned_trials"] = partial_prunes
        best["scoring_assessment"] = assessment
    return dict(best,**association_fields,point_id=int(point.point_id),lat=float(point.lat),lon=float(point.lon),elev_m=float(point.elev_m),
                x=float(point.x),y=float(point.y),flight_ids=[p["flight_id"] for p in candidate["passes"]])


def rank_candidates(candidates, points, tracks, events, measurements, **kwargs):
    """Return finite rankings; ``pipeline.score_block`` also retains unknown work."""
    lookup = {row.point_id:row for row in points.itertuples()}
    track_lookup = {track.id:track for track in tracks}
    result = []
    for i,candidate in enumerate(candidates):
        row = score_candidate(candidate,lookup[candidate["point_id"]],track_lookup,events,measurements,**kwargs)
        if row["score"] is not None and np.isfinite(row["score"]):
            result.append(row)
        if i and i%100==0:
            print(f"Scored {i}/{len(candidates)} candidates",flush=True)
    return sorted(result,key=lambda r:r["score"],reverse=True)

