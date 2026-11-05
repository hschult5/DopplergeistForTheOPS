from collections import defaultdict
import hashlib
import numpy as np
from scipy.ndimage import median_filter
from scipy import ndimage, signal, optimize
from scipy.fft import rfft
from scipy.interpolate import PchipInterpolator
from dopplergeist import discovery as baseline

def canonical(members, event_id, basis):
    return dict(event_id=event_id, start_s=min((e['start_s'] for e in members)), end_s=max((e['end_s'] for e in members)), band_hz=[min((e['band_hz'][0] for e in members)), max((e['band_hz'][1] for e in members))], raw_event_ids=sorted((e['event_id'] for e in members)), grouping_basis=basis, source_identity='unknown', physical_source_multiplicity='unresolved')

def group_baseline(events):
    groups = defaultdict(list)
    for e in events:
        groups[e['harmonic_group_id']].append(e)
    return [canonical(members, gid, 'Unchanged baseline harmonic_group_id') for gid, members in sorted(groups.items())]

def ridge(event):
    return np.array([[p['time_s'], p['frequency_hz'], p['weight']] for p in event['ridge']])

def common_evolution(a, b, min_samples=12):
    shared = baseline._overlap(a, b)
    if shared is None:
        return None
    t, f, g = shared
    if len(t) < min_samples or len(t) * 0.5 < 0.5 * min(np.ptp(a[:, 0]), np.ptp(b[:, 0])):
        return None
    lf, lg = (np.log(f), np.log(g))
    residual = float(np.quantile(np.abs(lg - lf - np.median(lg - lf)), 0.9))
    sf, sg = (median_filter(lf, size=5), median_filter(lg, size=5))
    excursion = min(float(np.ptp(sf)), float(np.ptp(sg)))
    correlation = float(np.corrcoef(sf, sg)[0, 1]) if min(np.std(sf), np.std(sg)) > 0 else 0.0
    limit = max(0.003, 0.15 * excursion)
    if not np.isfinite(correlation) or correlation < 0.85 or residual > limit:
        return None
    return dict(log_ratio=float(np.median(lg - lf)), residual_q90=residual, residual_limit=limit, correlation=correlation, shared_duration_s=len(t) * 0.5)

def group_coherent(events, min_samples=12):
    paths = [ridge(e) for e in events]
    pairs = {}
    for i in range(len(events)):
        for j in range(i + 1, len(events)):
            evidence = common_evolution(paths[i], paths[j], min_samples)
            if evidence is not None:
                pairs[i, j] = evidence
    groups = [{i} for i in range(len(events))]
    owner = {i: g for g in groups for i in g}
    joins = []
    for (i, j), evidence in sorted(pairs.items(), key=lambda p: (p[1]['residual_q90'] / p[1]['residual_limit'], p[0])):
        left, right = (owner[i], owner[j])
        if left is right:
            continue
        valid = True
        for x in left:
            for y in right:
                common = baseline._overlap(paths[x], paths[y])
                if common is not None and len(common[0]) >= min_samples and (tuple(sorted((x, y))) not in pairs):
                    valid = False
        if not valid:
            continue
        joins.append(dict(left=sorted((events[x]['event_id'] for x in left)), right=sorted((events[x]['event_id'] for x in right)), supporting_pair=[events[i]['event_id'], events[j]['event_id']], evidence=evidence))
        left.update(right)
        for x in right:
            owner[x] = left
        groups.remove(right)
    out = []
    for group in groups:
        members = [events[i] for i in sorted(group)]
        token = '|'.join(sorted((e['event_id'] for e in members)))
        out.append(canonical(members, 'episode-' + hashlib.sha256(token.encode()).hexdigest()[:16], 'Jointly compatible shared log-frequency evolution'))
    return (sorted(out, key=lambda e: (e['start_s'], e['event_id'])), joins)

def motion_boundaries(proposal, events):
    times, weights = ([], [])
    for e in events:
        a = ridge(e)
        t, f, strength = a.T
        smooth = median_filter(np.log(f), size=5, mode='nearest')
        dt = np.diff(t)
        displacement = np.abs(np.diff(smooth))
        weight = displacement * np.minimum(strength[:-1], strength[1:])
        weight[dt > 2.0] = 0.0
        times.extend(((t[:-1] + t[1:]) / 2).tolist())
        weights.extend(weight.tolist())
    order = np.argsort(times)
    times = np.asarray(times)[order]
    weights = np.asarray(weights)[order]
    proposal = dict(proposal, observed_support_start_s=proposal['start_s'], observed_support_end_s=proposal['end_s'])
    if len(times) and weights.sum() > 0:
        mass = np.cumsum(weights) / weights.sum()
        proposal['start_s'] = max(proposal['start_s'], float(np.interp(0.05, mass, times) - 1.0))
        proposal['end_s'] = min(proposal['end_s'], float(np.interp(0.95, mass, times) + 1.0))
        proposal['boundary_basis'] = 'Central 90% of resolved fractional-motion evidence, plus one half-window'
    return proposal

def catalog(events, variant):
    if variant == 'baseline':
        return (group_baseline(events), [])
    if variant == 'coherent':
        return group_coherent(events)
    if variant == 'coherent_core':
        proposals, decisions = group_coherent(events)
        lookup = {e['event_id']: e for e in events}
        proposals = [motion_boundaries(p, [lookup[eid] for eid in p['raw_event_ids']]) for p in proposals]
        return (proposals, decisions)
    if variant == 'coherent_adaptive':
        proposals, decisions = group_coherent(events)
        lookup = {e['event_id']: e for e in events}
        bounded = []
        for p in proposals:
            members = [lookup[eid] for eid in p['raw_event_ids']]
            typical = float(np.median([e['end_s'] - e['start_s'] for e in members]))
            if len(members) >= 3 and p['end_s'] - p['start_s'] > 2 * typical:
                p = motion_boundaries(p, members)
            else:
                p['boundary_basis'] = 'Full observed support; no duration-outlier boundary correction'
            bounded.append(p)
        return (bounded, decisions)
    raise ValueError(f'Unknown variant: {variant}')

def decoded_sample_rate_hz(variant):
    return 4000

def discover(recording, output, variant, aux_recording=None):
    events = baseline._discover_baseline_events(recording, output_dir=output / 'baseline-tracks', plot_summary=False, plot_event_details=False)
    proposals, decisions = catalog(events, 'coherent_adaptive' if variant.startswith('ridge_dp') else variant)
    diagnostics = []
    if variant.startswith('ridge_dp'):
        extra, diagnostics = dp_front(aux_recording, variant)
        proposed, joins = group_coherent(extra, min_samples=5)
        lookup = {e['event_id']: e for e in events + extra}
        attachments = []
        for p in proposed:
            explained = None
            for accepted in proposals:
                if any((common_evolution(ridge(lookup[a]), ridge(lookup[b]), min_samples=5) is not None for a in p['raw_event_ids'] for b in accepted['raw_event_ids'])):
                    explained = accepted['event_id']
                    break
            if explained:
                attachments.append(dict(proposal=p['event_id'], explained_by=explained, raw_event_ids=p['raw_event_ids']))
            else:
                p['acceptance_path'] = 'spectrogram_sequence_evidence'
                proposals.append(p)
        events += extra
        decisions += joins
        decisions.append(dict(sequence_attachments=attachments))
    result = dict(events=proposals, raw_events=events, grouping_decisions=decisions, variant=variant, settings=vars(baseline.DiscoverySettings()), sequence_diagnostics=diagnostics)
    if variant in {'ridge_dp_accel_broad_final_v24', 'ridge_dp_accel_broad_final_v25'}:
        final = anchored_catalog(result, aux_recording, 'pass_v25' if variant.endswith('_v25') else 'pass_v24')
        final.update(variant=variant, candidate_catalog=result['events'], sequence_diagnostics=diagnostics, baseline_settings=result['settings'], sequence_settings=dict(chunk_s=120.0, overlap_s=60.0, low_band_hz=[20.0, 1600.0], high_band_hz=[1600.0, 8000.0], low_windows_s=[0.5, 2.0], low_hops_s=[0.125, 0.5], high_window_s=0.5, high_hop_s=0.125, log_bins_per_octave=[144, 576], high_background_sigma_hz=100.0, iteration_limit=32, maximum_fractional_rate_per_s=[0.3, 0.05], sequence_emission_bias=1.5), scope='Acoustic contour/episode proposals. No aircraft identity or physical source-motion verification.')
        return final
    return result

def auxiliary_sample_rate_hz(variant):
    return 24000 if variant.startswith('ridge_dp') else None

def log_image(frequency, image, low, high, bins_per_octave=144):
    logf = np.arange(np.log2(low), np.log2(high), 1 / bins_per_octave)
    centres = 2 ** logf
    pos = (centres - frequency[0]) / (frequency[1] - frequency[0])
    i = np.clip(pos.astype(int), 0, len(frequency) - 2)
    w = np.clip(pos - i, 0, 1)
    out = (1 - w[:, None]) * image[i] + w[:, None] * image[i + 1]
    bins = np.floor((np.log2(np.maximum(frequency, 1e-06)) - logf[0]) * bins_per_octave).astype(int)
    valid = (bins >= 0) & (bins < len(logf))
    rows = np.flatnonzero(valid)
    which = bins[valid]
    starts = np.flatnonzero(np.r_[True, np.diff(which) > 0])
    if len(starts):
        maxima = np.maximum.reduceat(image[rows], starts, axis=0)
        out[which[starts]] = np.maximum(out[which[starts]], maxima)
    center = np.median(out, axis=1, keepdims=True)
    scale = 1.4826 * np.median(np.abs(out - center), axis=1, keepdims=True)
    return (centres, np.clip((out - center) / np.maximum(scale, 2.0), -4.0, 8.0).astype(np.float32))

def best_sequence(image, hop, bins_per_octave=144, bias=1.5):
    nf, nt = image.shape
    back = np.full((nt, nf), 127, dtype=np.int8)
    previous = np.zeros(nf, dtype=np.float32)
    maximum = (-np.inf, 0, 0)
    dmax = max(2, int(np.ceil(0.4 * hop * bins_per_octave / np.log(2))))
    for t in range(nt):
        best = np.zeros(nf, dtype=np.float32)
        choice = np.full(nf, 127, dtype=np.int8)
        for shift in range(-dmax, dmax + 1):
            lo = max(0, -shift)
            hi = min(nf, nf - shift)
            value = previous[lo + shift:hi + shift] - 0.2 * abs(shift)
            take = value > best[lo:hi]
            best[lo:hi][take] = value[take]
            choice[lo:hi][take] = shift
        current = image[:, t] - bias + best
        back[t] = choice
        j = int(np.argmax(current))
        if current[j] > maximum[0]:
            maximum = (float(current[j]), t, j)
        previous = current
    score, t, j = maximum
    path = []
    while True:
        path.append((t, j))
        shift = int(back[t, j])
        if shift == 127 or t == 0:
            break
        j += shift
        t -= 1
    return (score, np.asarray(path[::-1], dtype=int))

def dp_front(recording, variant):
    events = []
    diagnostics = []
    for chunk, (start, data) in enumerate(recording.iter_chunks(120.0, 60.0)):
        profiles = [(0.5, 0.125), (2.0, 0.5)]
        if variant.startswith(('ridge_dp_dense', 'ridge_dp_accel')):
            profiles.append((1.0, 0.25))
        for window, hop in profiles:
            n = round(window * recording.metadata['sample_rate_hz'])
            step = round(hop * recording.metadata['sample_rate_hz'])
            if len(data) < n:
                continue
            if 'reassigned' in variant:
                f, t, p = reassigned_power(data, recording.metadata['sample_rate_hz'], n, step)
            else:
                f, t, p = signal.spectrogram(data, fs=recording.metadata['sample_rate_hz'], nperseg=n, noverlap=n - step, window='hann', scaling='density')
            keep = (f >= 10) & (f <= 8200)
            f = f[keep]
            db = 10 * np.log10(np.maximum(p[keep], 1e-30))
            t = t + start
            local = db - median_filter(db, size=(31, 1), mode='nearest')
            for band, (low, high) in enumerate([(20.0, 1600.0), (1600.0, 8000.0)]):
                dense = variant.startswith(('ridge_dp_dense', 'ridge_dp_accel'))
                high_window = 0.5 if 'broad' in variant else 1.0
                if dense and (band == 1 and window != high_window or (band == 0 and window == 1.0)):
                    continue
                bins_per_octave = 576 if dense and band == 1 else 144
                band_local = local
                if 'broad' in variant and band == 1:
                    band_local = db - ndimage.gaussian_filter1d(db, 100.0 / (f[1] - f[0]), axis=0, mode='nearest')
                centres, image = log_image(f, band_local, low, high, bins_per_octave=bins_per_octave)
                accepted = 0
                rejected = {}
                last_score = None
                for iteration in range(32):
                    solver = best_sequence_curved if variant.startswith('ridge_dp_accel') else best_sequence
                    score, path = solver(image, hop, bins_per_octave=bins_per_octave, bias=1.5)
                    last_score = score
                    if score < 25.0:
                        break
                    cols, rows = path.T
                    z = image[rows, cols].copy()
                    for off in range(-2, 3):
                        image[np.clip(rows + off, 0, len(centres) - 1), cols] = -4.0
                    strong = np.flatnonzero(z > 1.0)
                    if len(strong) < 6:
                        rejected['few_observations'] = rejected.get('few_observations', 0) + 1
                        continue
                    cuts = np.flatnonzero(np.diff(cols[strong]) * hop > 1.0) + 1
                    for indices in np.split(strong, cuts):
                        chosen = path[indices]
                        ct = t[chosen[:, 0]]
                        if np.ptp(ct) < 2.5:
                            continue
                        points = []
                        snrs = []
                        flat = []
                        fixed = float(np.median(centres[chosen[:, 1]]))
                        fixed_row = int(np.argmin(np.abs(f - fixed)))
                        for col, row in chosen:
                            target = centres[row]
                            radius = max(2 / window, target * 0.008)
                            lo = int(np.searchsorted(f, target - radius))
                            hi = int(np.searchsorted(f, target + radius))
                            if hi <= lo:
                                continue
                            peak = lo + int(np.argmax(db[lo:hi, col]))
                            freq = float(f[peak])
                            if 0 < peak < len(f) - 1:
                                a, b, c = db[peak - 1:peak + 2, col]
                                den = a - 2 * b + c
                                if abs(den) > 1e-09:
                                    freq += float(np.clip(0.5 * (a - c) / den, -0.5, 0.5)) / window
                            points.append((float(t[col]), freq, float(max(z[np.flatnonzero(cols == col)[0]], 0.1))))
                            snrs.append(float(band_local[peak, col]))
                            flat.append(float(band_local[peak, col] - band_local[fixed_row, col]))
                        a = np.asarray(points)
                        if len(a) < 6:
                            continue
                        tt, ff, ww = a.T
                        lf = median_filter(np.log(ff), size=5, mode='nearest')
                        dt = np.diff(tt)
                        span = float(np.ptp(tt))
                        excursion = float(np.quantile(lf, 0.95) - np.quantile(lf, 0.05))
                        variation = float(np.sum(np.abs(np.diff(lf)))) / max(excursion, 1e-08)
                        moving = float(np.mean(np.abs(np.diff(lf) / dt) > max(0.001, excursion / span * 0.2)))
                        support = len(tt) / (span / hop + 1)
                        reason = None
                        if excursion < max(0.025, 3 / window / np.median(ff)):
                            reason = 'unresolved_motion'
                        elif variation > 3.0:
                            reason = 'wandering'
                        elif moving < 0.4:
                            reason = 'sparse_motion'
                        elif support < 0.6:
                            reason = 'sparse_support'
                        elif np.median(snrs) < 4.0:
                            reason = 'weak_tonality'
                        elif np.mean(flat) < 1.0:
                            reason = 'flat_alternative'
                        if reason:
                            rejected[reason] = rejected.get(reason, 0) + 1
                            continue
                        eid = f'dp-{chunk}-{window}-{band}-{iteration}-{len(events)}'
                        events.append(dict(event_id=eid, start_s=max(0.0, float(tt[0] - window / 2)), end_s=min(recording.metadata['duration_s'], float(tt[-1] + window / 2)), band_hz=[float(max(0.01, ff.min() - 2 / window)), float(ff.max() + 2 / window)], ridge=[dict(time_s=float(x), frequency_hz=float(y), weight=float(z)) for x, y, z in a], quality=dict(sequence_score=score, mean_foreground=float(np.mean(ww)), median_local_snr_db=float(np.median(snrs)), moving_vs_flat_db=float(np.mean(flat)), frequency_resolution_hz=1 / window, window_s=window, hop_s=hop, fractional_excursion=excursion, variation_ratio=variation, moving_fraction=moving, support_fraction=support)))
                        accepted += 1
                diagnostics.append(dict(chunk_start_s=start, window_s=window, band_hz=[low, high], accepted_tracks=accepted, rejections=rejected, last_sequence_score=last_score, iteration_limit=32, budget_reached=iteration == 31))
    return (events, diagnostics)

def reassigned_power(samples, sample_rate, n, step):
    frames = np.lib.stride_tricks.sliding_window_view(samples, n)[::step]
    position = np.arange(n) / n
    window = (0.5 - 0.5 * np.cos(2 * np.pi * position)).astype(np.float32)
    derivative = (np.pi * sample_rate / n * np.sin(2 * np.pi * position)).astype(np.float32)
    spectrum = rfft(frames * window, axis=1).T
    differentiated = rfft(frames * derivative, axis=1).T
    f = np.fft.rfftfreq(n, 1 / sample_rate)
    power = abs(spectrum) ** 2
    correction = np.imag(differentiated * np.conj(spectrum)) / np.maximum(power, 1e-30) / (2 * np.pi)
    destination = np.rint((f[:, None] - correction) / (sample_rate / n)).astype(np.int32)
    limit = np.maximum(100.0, 0.05 * f)[:, None]
    valid = (destination >= 0) & (destination < len(f)) & (abs(correction) <= limit)
    output = np.zeros_like(power)
    for col in range(power.shape[1]):
        at = valid[:, col]
        output[:, col] = np.bincount(destination[at, col], weights=power[at, col], minlength=len(f))
    output /= sample_rate * np.dot(window, window)
    output[1:-1] *= 2.0
    return (f, (np.arange(len(frames)) * step + n / 2) / sample_rate, output)
_best_sequence_numpy = best_sequence
from numba import njit

@njit(cache=True)
def _sequence_forward(image, hop, bins_per_octave, bias):
    nt, nf = image.shape
    back = np.full((nt, nf), 127, np.int8)
    previous = np.zeros(nf, np.float32)
    maximum_fractional_rate = 0.1 if bins_per_octave > 144 else 0.4
    dmax = max(2, int(np.ceil(maximum_fractional_rate * hop * bins_per_octave / np.log(2.0))))
    maximum = -np.inf
    best_t = 0
    best_j = 0
    for t in range(nt):
        current = np.empty(nf, np.float32)
        for j in range(nf):
            best = np.float32(0.0)
            choice = 127
            for shift in range(-dmax, dmax + 1):
                k = j + shift
                if 0 <= k < nf:
                    value = np.float32(previous[k] - np.float32(0.2 * 144 / bins_per_octave * abs(shift)))
                    if value > best:
                        best = value
                        choice = shift
            value = np.float32(np.float32(image[t, j] - np.float32(bias)) + best)
            current[j] = value
            back[t, j] = choice
            if value > maximum:
                maximum = value
                best_t = t
                best_j = j
        previous = current
    return (maximum, best_t, best_j, back)

def best_sequence(image, hop, bins_per_octave=144, bias=1.5):
    score, t, j, back = _sequence_forward(np.ascontiguousarray(image.T), hop, bins_per_octave, bias)
    path = []
    while True:
        path.append((t, j))
        shift = int(back[t, j])
        if shift == 127 or t == 0:
            break
        j += shift
        t -= 1
    return (float(score), np.asarray(path[::-1], dtype=int))

@njit(cache=True)
def _curved_forward(image, maximum_velocity, bias):
    nt, nf = image.shape
    nv = 2 * maximum_velocity + 1
    back = np.full((nt, nf, nv), 127, np.int8)
    previous = np.zeros((nf, nv), np.float32)
    maximum = -np.inf
    best_t = 0
    best_j = 0
    best_v = 0
    for t in range(nt):
        current = np.full((nf, nv), -1000.0, np.float32)
        for j in range(nf):
            for v in range(nv):
                velocity = v - maximum_velocity
                k = j - velocity
                if k < 0 or k >= nf:
                    continue
                best = np.float32(0.0)
                choice = 127
                for change in range(-1, 2):
                    old_v = v + change
                    if 0 <= old_v < nv:
                        value = previous[k, old_v] - 0.8 * abs(change) - 0.03 * abs(velocity)
                        if value > best:
                            best = value
                            choice = change
                value = np.float32(image[t, j] - bias + best)
                current[j, v] = value
                back[t, j, v] = choice
                if value > maximum:
                    maximum = value
                    best_t = t
                    best_j = j
                    best_v = v
        previous = current
    return (maximum, best_t, best_j, best_v, back)

def best_sequence_curved(image, hop, bins_per_octave=144, bias=1.5):
    rate = 0.05 if bins_per_octave > 144 else 0.3
    vmax = max(2, int(np.ceil(rate * hop * bins_per_octave / np.log(2.0))))
    score, t, j, v, back = _curved_forward(np.ascontiguousarray(image.T), vmax, bias)
    path = []
    while True:
        path.append((t, j))
        change = int(back[t, j, v])
        if change == 127 or t == 0:
            break
        j -= v - vmax
        v += change
        t -= 1
    return (float(score), np.asarray(path[::-1], dtype=int))

def verify_tracks(recording, events, variant):
    sr = recording.metadata['sample_rate_hz']
    x = np.memmap(recording.samples_path, dtype='<f4', mode='r')
    window = 0.5
    hop = 0.5
    n = round(sr * window)
    f, t, p = signal.spectrogram(x, fs=sr, nperseg=n, noverlap=0, window='hann', scaling='density')
    keep = (f >= 10) & (f <= 8200)
    f = f[keep]
    power = p[keep]
    db = 10 * np.log10(np.maximum(power, 1e-30))
    accepted = []
    rejected = []
    evidence = {}
    for event in events:
        path = ridge(event)
        tt, ff, ww = path.T
        columns = np.flatnonzero((t >= tt[0]) & (t <= tt[-1]))
        if len(columns) < 5:
            rejected.append(dict(event_id=event['event_id'], reason='too_few_independent_windows'))
            continue
        ft = np.interp(t[columns], tt, ff)
        smooth = median_filter(ff, size=5, mode='nearest')
        slope = np.interp(t[columns], tt, np.gradient(smooth, tt))
        rows = []
        snr = []
        dominance = []
        widths = []
        valid = []
        amplitude = []
        observations = []
        following = np.clip(np.searchsorted(tt, t[columns]), 0, len(tt) - 1)
        previous = np.maximum(0, following - 1)
        observed = np.minimum(abs(t[columns] - tt[following]), abs(t[columns] - tt[previous])) <= 0.5
        for col, predicted, rate, has_measurement in zip(columns, ft, slope, observed):
            radius = max(4.0, abs(rate) * window * 0.6)
            radius = min(radius, max(4.0, 0.03 * predicted))
            lo = int(np.searchsorted(f, predicted - radius))
            hi = int(np.searchsorted(f, predicted + radius))
            if hi <= lo:
                continue
            peak = lo + int(np.argmax(db[lo:hi, col]))
            level = float(db[peak, col])
            spread = max(24.0, 0.1 * predicted, 4 * radius)
            l = int(np.searchsorted(f, predicted - spread))
            r = int(np.searchsorted(f, predicted + spread))
            left = f[l:r] < predicted - radius - 2.0
            right = f[l:r] > predicted + radius + 2.0
            reference = db[l:r, col][left | right]
            if len(reference) < 6:
                continue
            noise = float(np.median(reference))
            upper = float(np.quantile(reference, 0.9))
            a = peak
            b = peak
            while a > 0 and db[a - 1, col] >= level - 3.0:
                a -= 1
            while b < len(f) - 1 and db[b + 1, col] >= level - 3.0:
                b += 1
            width = float(f[b] - f[a] + 2.0)
            coherent = has_measurement and level - noise >= 8.0 and (level - upper >= (0.0 if variant.startswith('consensus') else 3.0)) and (width <= max(12.0, 2 * abs(rate) * window + 4.0))
            valid.append(coherent)
            rows.append(peak)
            snr.append(level - noise)
            dominance.append(level - upper)
            widths.append(width)
            measured = float(f[peak])
            if variant.startswith(('clear', 'consensus')) and 0 < peak < len(f) - 1:
                a0, b0, c0 = db[peak - 1:peak + 2, col]
                den = a0 - 2 * b0 + c0
                if abs(den) > 1e-09:
                    measured += float(np.clip(0.5 * (a0 - c0) / den, -0.5, 0.5)) * 2.0
            amplitude.append(level)
            observations.append((float(t[col]), measured, float(max(level - noise, 0.01))))
        if len(valid) < 5:
            continue
        good = np.asarray(valid)
        points = np.asarray(observations)
        coverage = float(np.mean(good))
        runs = []
        first = None
        last = None
        for i, ok in enumerate(good):
            if ok:
                if first is None:
                    first = i
                last = i
            elif last is not None and i - last > 2:
                if last - first + 1 >= 5:
                    runs.append((first, last + 1))
                first = None
                last = None
        if first is not None and last - first + 1 >= 5:
            runs.append((first, last + 1))
        detail = dict(event_id=event['event_id'], independent_windows=len(good), visible_fraction=coverage, median_snr_db=float(np.median(snr)), median_dominance_db=float(np.median(dominance)), samples=[dict(time_s=float(a), frequency_hz=float(b), snr_db=float(c), visible=bool(v), dominance_db=float(d), width_hz=float(w)) for (a, b, c), v, d, w in zip(points, good, dominance, widths)])
        evidence[event['event_id']] = detail
        if coverage < 0.75 and (not variant.startswith(('clear', 'consensus'))) or not runs:
            rejected.append(dict(event_id=event['event_id'], reason='insufficient_visible_ridge', **{k: v for k, v in detail.items() if k != 'samples' and k != 'event_id'}))
            continue
        first, last = max(runs, key=lambda p: np.sum(good[p[0]:p[1]]))
        selected = points[first:last][good[first:last]]
        if np.ptp(selected[:, 0]) < 2.0:
            continue
        if variant.startswith('consensus') and np.mean(good[first:last]) < 0.6:
            continue
        if variant.startswith('clear'):
            fraction = float(np.mean(good[first:last]))
            if fraction < 0.75:
                rejected.append(dict(event_id=event['event_id'], reason='unsupported_core', visible_fraction=fraction))
                continue
            phase = phase_support(recording, selected)
            detail['phase_support'] = phase
            if phase['coherent_fraction'] < 0.75 or phase['median_concentration'] < 0.5:
                rejected.append(dict(event_id=event['event_id'], reason='incoherent_waveform', phase_support=phase))
                continue
            sigma = float(np.median([max(0.5, w / (2.355 * np.sqrt(max(10 ** (s / 10), 1.0)))) for w, s in zip(widths, snr)]))
            sf = median_filter(selected[:, 1], size=3, mode='nearest')
            excursion = float(np.quantile(sf, 0.95) - np.quantile(sf, 0.05))
            total = float(np.sum(abs(np.diff(sf))))
            regional = [float(np.ptp(sf[b])) if len(b) > 1 else 0.0 for b in np.array_split(np.arange(len(sf)), 3)]
            if excursion < max(4.0, 4 * sigma, 0.025 * np.median(sf)) or total / max(excursion, 1e-06) > 3.0 or sum((v >= max(1.5 * sigma, 0.1 * excursion) for v in regional)) < 2:
                rejected.append(dict(event_id=event['event_id'], reason='unresolved_independent_motion', excursion_hz=excursion, sigma_hz=sigma, regional_excursions=regional))
                continue
            coverage = fraction
        event = dict(event, original_start_s=event['start_s'], original_end_s=event['end_s'], start_s=max(0.0, float(selected[0, 0] - 0.25)), end_s=min(recording.metadata['duration_s'], float(selected[-1, 0] + 0.25)), ridge=[dict(time_s=float(a), frequency_hz=float(b), weight=float(c)) for a, b, c in selected], quality=dict(event['quality'], visible_fraction=coverage, independent_visible_windows=int(np.sum(good)), median_verified_snr_db=float(np.median(snr)), median_verified_dominance_db=float(np.median(dominance))))
        accepted.append(event)
        detail['amplitude_time_s'] = points[:, 0].tolist()
        detail['amplitude_db'] = amplitude
    return (accepted, rejected, evidence)

def phase_support(recording, points):
    sr = recording.metadata['sample_rate_hz']
    samples = np.memmap(recording.samples_path, dtype='<f4', mode='r')
    tt, ff, _ = points.T
    duration = min(1.5, max(1.0, np.ptp(tt) / 3))
    centres = np.arange(tt[0] + duration / 2, tt[-1] - duration / 2 + 0.01, duration)
    if len(centres) > 12:
        centres = np.linspace(centres[0], centres[-1], 12)
    entries = []
    n = round(sr * duration)
    window = signal.windows.hann(n, sym=False)
    frequency = np.fft.fftfreq(n, 1 / sr)
    for centre in centres:
        lo = round((centre - duration / 2) * sr)
        hi = lo + n
        if lo < 0 or hi > len(samples):
            continue
        at = (np.arange(n) + lo) / sr
        use = abs(tt - centre) <= max(1.5, duration)
        if np.sum(use) < 3:
            continue
        coef = np.polyfit(tt[use] - centre, ff[use], 2)
        instantaneous = np.polyval(coef, at - centre)
        phase = 2 * np.pi * np.cumsum(instantaneous) / sr
        transformed = np.fft.fft(np.asarray(samples[lo:hi]) * window * np.exp(-1j * phase))
        power = abs(transformed) ** 2
        search = np.flatnonzero(abs(frequency) <= 4.0)
        peak = int(search[np.argmax(power[search])])
        offset = float(frequency[peak])
        central = abs(frequency - offset) <= 1.1 / duration
        neighbourhood = abs(frequency - offset) <= 12.0
        concentration = float(power[central].sum() / max(power[neighbourhood].sum(), 1e-30))
        entries.append(dict(time_s=float(centre), concentration=concentration, frequency_offset_hz=offset))
    values = [e['concentration'] for e in entries]
    return dict(coherent_fraction=float(np.mean(np.asarray(values) >= 0.5)) if values else 0.0, median_concentration=float(np.median(values)) if values else 0.0, window_s=duration, windows=entries)

def episode_groups(events, evidence):
    paths = [ridge(e) for e in events]
    links = {}
    for i in range(len(events)):
        for j in range(i + 1, len(events)):
            shared = baseline._overlap(paths[i], paths[j])
            if shared is None:
                continue
            t, f, g = shared
            if len(t) < 5 or len(t) * 0.5 < 0.5 * min(np.ptp(paths[i][:, 0]), np.ptp(paths[j][:, 0])):
                continue
            qa, qb = (events[i]['quality'], events[j]['quality'])
            resolution = max(qa.get('frequency_resolution_hz', 0.5), qb.get('frequency_resolution_hz', 0.5), 2.0)
            scale = float(np.exp(np.median(np.log(g / f))))
            error = float(np.quantile(abs(g - scale * f), 0.9))
            excursion = min(float(np.ptp(f)) * scale, float(np.ptp(g)))
            same = error <= max(2.5 * resolution, 0.12 * excursion) and np.corrcoef(f, g)[0, 1] >= 0.85
            affine = None
            if not same and len(t) >= 8:
                ea = evidence[events[i]['event_id']]
                eb = evidence[events[j]['event_id']]
                aa = np.interp(t, ea['amplitude_time_s'], ea['amplitude_db'])
                bb = np.interp(t, eb['amplitude_time_s'], eb['amplitude_db'])
                slope, offset = np.polyfit(f, g, 1)
                residual = float(np.quantile(abs(g - (slope * f + offset)), 0.9))
                corr = float(np.corrcoef(aa, bb)[0, 1]) if min(np.std(aa), np.std(bb)) >= 1.5 else 0.0
                if slope > 0 and np.corrcoef(f, g)[0, 1] >= 0.94 and (residual <= max(2 * resolution, 0.08 * excursion)) and (corr >= 0.8):
                    same = True
                    affine = dict(scale=float(slope), offset_hz=float(offset), envelope_correlation=corr)
            if same:
                links[i, j] = dict(scale=scale, error_hz=error, affine=affine)
    groups = [{i} for i in range(len(events))]
    owners = {i: g for g in groups for i in g}
    decisions = []
    for (i, j), model in sorted(links.items(), key=lambda p: (p[1]['error_hz'], p[0])):
        a, b = (owners[i], owners[j])
        if a is b:
            continue
        compatible = True
        for x in a:
            for y in b:
                shared = baseline._overlap(paths[x], paths[y])
                if shared is not None and len(shared[0]) >= 5 and (tuple(sorted((x, y))) not in links):
                    compatible = False
        if not compatible:
            continue
        decisions.append(dict(left=[events[k]['event_id'] for k in sorted(a)], right=[events[k]['event_id'] for k in sorted(b)], evidence=model))
        a.update(b)
        for x in b:
            owners[x] = a
        groups.remove(b)
    proposals = []
    for group in groups:
        members = [events[i] for i in sorted(group)]
        token = '|'.join(sorted((e['event_id'] for e in members)))
        p = canonical(members, 'visible-' + hashlib.sha256(token.encode()).hexdigest()[:16], 'Independently visible ridges with shared evolution and measured envelope corroboration')
        p['minimum_visible_fraction'] = min((e['quality']['visible_fraction'] for e in members))
        proposals.append(p)
    return (proposals, decisions)

def refine(prior, recording, variant):
    if variant.startswith('pass_'):
        return anchored_catalog(prior, recording, variant)
    if variant.startswith('anchored'):
        return anchored_catalog(prior, recording, variant)
    if variant.startswith('consensus'):
        return consensus_catalog(prior, recording, variant)
    verified, rejected, evidence = verify_tracks(recording, prior['raw_events'], variant)
    proposals, decisions = episode_groups(verified, evidence)
    return dict(events=proposals, raw_events=verified, grouping_decisions=decisions, variant=variant, verification_rejections=rejected, verification_evidence=evidence, settings=dict(independent_window_s=0.5, minimum_snr_db=8.0, minimum_dominance_db=3.0, minimum_visible_fraction=0.75), scope='Clear observed spectral ridges; physical source and motion unverified')

def curve_evidence(event):
    a = ridge(event)
    t, f, _ = a.T
    if len(t) > 100:
        at = np.linspace(0, len(t) - 1, 100).astype(int)
        t = t[at]
        f = f[at]
    smooth = median_filter(f, size=3, mode='nearest')
    excursion = float(np.quantile(smooth, 0.95) - np.quantile(smooth, 0.05))
    sigma = max(0.5, event['quality'].get('frequency_resolution_hz', 2.0))
    monotonicity = abs(float(smooth[-1] - smooth[0])) / max(float(np.sum(abs(np.diff(smooth)))), 1e-09)
    x = (t - np.mean(t)) / np.ptp(t)

    def model(p):
        z = x - p[2]
        return p[0] + p[1] * z / np.sqrt(z * z + p[3] * p[3])
    best = None
    failures = []
    clipped_starts = 0
    for center in [-0.4, 0.0, 0.4]:
        lower = np.array([0.0, -3 * np.median(f), -2.0, 0.01])
        upper = np.array([3 * np.median(f), 3 * np.median(f), 2.0, 5.0])
        proposed = np.array([float(np.median(f)), float(f[-1] - f[0]), center, 0.3])
        initial = np.clip(proposed, np.nextafter(lower, upper), np.nextafter(upper, lower))
        clipped_starts += bool(np.any(initial != proposed))
        try:
            fit = optimize.least_squares(lambda p: (model(p) - f) / sigma, initial, bounds=(lower, upper), loss='soft_l1', max_nfev=60)
        except (ValueError, FloatingPointError) as exc:
            failures.append(dict(center=center, error_type=type(exc).__name__, message=str(exc)))
            continue
        residual = float(np.quantile(abs(model(fit.x) - f), 0.9))
        if best is None or residual < best['residual_q90_hz']:
            best = dict(residual_q90_hz=residual, center_s=float(np.mean(t) + fit.x[2] * np.ptp(t)), width_s=float(fit.x[3] * np.ptp(t)), direction='descending' if fit.x[1] < 0 else 'ascending')
            best['residual_q99_hz'] = float(np.quantile(abs(model(fit.x) - f), 0.99))
    if best is None:
        raise ValueError('No finite bounded pass fit: ' + str(failures))
    best.update(excursion_hz=excursion, monotonicity=monotonicity, fit_fraction=best['residual_q90_hz'] / max(excursion, sigma), motion_resolved=bool(excursion >= max(4 * sigma, 0.025 * np.median(f))), frequency_ratio=float(np.max(smooth) / np.min(smooth)), clipped_initializations=clipped_starts, fit_failures=failures)
    return best

def trajectory_relation(a, b):
    shared = baseline._overlap(ridge(a), ridge(b))
    if shared is None:
        return None
    t, f, g = shared
    if len(t) < 5 or np.ptp(t) < 0.25 * min(a['end_s'] - a['start_s'], b['end_s'] - b['start_s']):
        return None
    scale = float(np.median(g / f))
    excursion = min(np.ptp(f) * scale, np.ptp(g))
    error = float(np.quantile(abs(g - scale * f), 0.8))
    corr = float(np.corrcoef(f, g)[0, 1]) if min(np.std(f), np.std(g)) else 0.0
    if corr < 0.8 or error > max(4.0, 0.02 * np.median(g), 0.18 * excursion):
        return None
    return dict(scale=scale, residual_q80_hz=error, correlation=corr, shared_duration_s=float(np.ptp(t)))

def joint_pass_evidence(events):
    paths = []
    for event in events:
        a = ridge(event)
        if len(a) > 60:
            a = a[np.linspace(0, len(a) - 1, 60).astype(int)]
        paths.append(a)
    start = min((a[0, 0] for a in paths))
    end = max((a[-1, 0] for a in paths))
    span = end - start
    arrays = [((a[:, 0] - start) / span, a[:, 1]) for a in paths]

    def residual(p, details=False):
        values = []
        stats = []
        for (t, f), e in zip(arrays, events):
            z = t - p[0]
            shape = 1 - p[2] * z / np.sqrt(z * z + p[1] * p[1])
            scale = np.dot(shape, f) / np.dot(shape, shape)
            delta = scale * shape - f
            resolution = e['quality'].get('frequency_resolution_hz', 2.0)
            sigma = max(resolution, 0.005 * np.median(f))
            values.extend((delta / sigma).tolist())
            stats.append(dict(event_id=e['event_id'], residual_q90_hz=float(np.quantile(abs(delta), 0.9)), residual_q99_hz=float(np.quantile(abs(delta), 0.99)), excursion_hz=float(np.ptp(f)), scale_hz=float(scale), resolution_hz=resolution))
        return stats if details else np.asarray(values)
    best = None
    for center in [0.2, 0.5, 0.8]:
        fit = optimize.least_squares(residual, [center, 0.25, 0.2], bounds=([-0.5, 0.02, 0.01], [1.5, 3.0, 0.8]), loss='soft_l1', max_nfev=100)
        score = float(np.mean(np.minimum(residual(fit.x) ** 2, 25.0)))
        if best is None or score < best[0]:
            best = (score, fit.x)
    stats = residual(best[1], True)
    valid = all((s['residual_q90_hz'] <= max(2.0, 2 * s['resolution_hz'], 0.01 * s['scale_hz']) and s['residual_q90_hz'] <= max(2.0, s['resolution_hz'], 0.15 * s['excursion_hz']) for s in stats))
    return dict(compatible=bool(valid), center_s=float(start + best[1][0] * span), width_s=float(best[1][1] * span), fractional_shift=float(best[1][2]), track_residuals=stats)

def consensus_catalog(prior, recording, variant):
    verified, rejected, evidence = verify_tracks(recording, prior['raw_events'], variant)
    lookup = {e['event_id']: e for e in prior['raw_events']}
    base = [e for e in prior['raw_events'] if e['event_id'].startswith('event-')]
    original, _ = catalog(base, 'coherent_adaptive')
    curves = {}
    fit_rejections = []
    fitted = []
    for event in verified:
        try:
            curves[event['event_id']] = curve_evidence(event)
        except (ValueError, FloatingPointError) as exc:
            fit_rejections.append(dict(event_id=event['event_id'], reason='curve_fit_failed', error_type=type(exc).__name__, message=str(exc)))
            continue
        fitted.append(event)
    verified = fitted
    rejected += fit_rejections
    order = sorted(verified, key=lambda e: (-np.ptp(ridge(e)[:, 0]) * e['quality'].get('median_verified_snr_db', 0), e['event_id']))
    groups = []
    joins = []
    for e in order:
        compatible = []
        for i, members in enumerate(groups):
            relation = trajectory_relation(members[0], e)
            if relation is None:
                continue
            if any((trajectory_relation(m, e) is None and baseline._overlap(ridge(m), ridge(e)) is not None and (min(m['end_s'] - m['start_s'], e['end_s'] - e['start_s']) > 10.0) for m in members)):
                continue
            compatible.append((relation['residual_q80_hz'] / np.median(ridge(e)[:, 1]), i, relation))
        if compatible:
            _, i, relation = min(compatible)
            groups[i].append(e)
            joins.append(dict(track=e['event_id'], seed=groups[i][0]['event_id'], evidence=relation))
        else:
            groups.append([e])
    proposals = []
    decisions = []
    assigned = set()
    for members in groups:
        baseline_ids = {e['event_id'] for e in members if e['event_id'].startswith('event-')}
        independent = []
        for e in members:
            if not any((abs(np.log(np.median(ridge(e)[:, 1]) / np.median(ridge(m)[:, 1]))) < 0.08 for m in independent)):
                independent.append(e)
        strong = [e for e in members if curves[e['event_id']]['motion_resolved'] and curves[e['event_id']]['fit_fraction'] <= 0.15 and (curves[e['event_id']]['monotonicity'] >= 0.65) and (curves[e['event_id']]['frequency_ratio'] <= 3.0)]
        accept = bool(baseline_ids) or (bool(strong) and (len(independent) >= 2 or max((e['quality'].get('median_verified_snr_db', 0) for e in strong)) >= 24.0))
        decisions.append(dict(raw_event_ids=[e['event_id'] for e in members], independent_frequency_components=len(independent), smooth_curve_tracks=[e['event_id'] for e in strong], accepted=accept, baseline_supported=bool(baseline_ids)))
        if not accept:
            continue
        ids = {e['event_id'] for e in members}
        assigned.update(ids)
        token = '|'.join(sorted(ids))
        p = canonical(members, 'consensus-' + hashlib.sha256(token.encode()).hexdigest()[:16], 'Measured smooth shared-trajectory family; seed-based grouping, original baseline evidence retained')
        proposals.append(p)
    for p in original:
        remaining = [lookup[eid] for eid in p['raw_event_ids'] if eid not in assigned]
        if remaining:
            proposals.append(canonical(remaining, p['event_id'], 'Original baseline acceptance; no replacement support'))
            assigned.update((e['event_id'] for e in remaining))
    return dict(events=sorted(proposals, key=lambda p: (p['start_s'], p['event_id'])), raw_events=prior['raw_events'], verified_tracks=verified, verification_evidence=evidence, verification_rejections=rejected, grouping_decisions=joins, acceptance_decisions=decisions, curve_evidence=curves, variant=variant, settings=dict(minimum_visible_core_fraction=0.6, maximum_curve_residual_fraction=0.15, minimum_monotonicity=0.65), scope='Acoustic pattern proposals; source and physical motion unverified')

def anchored_catalog(prior, recording, variant):
    if variant.startswith(('pass_v22', 'pass_v23', 'pass_v24', 'pass_v25')):
        prior = dict(prior, raw_events=prior['raw_events'] + split_upper_motions(prior['raw_events']))
    result = consensus_catalog(prior, recording, 'consensus_v11')
    verified = result['verified_tracks']
    originals = {e['event_id']: e for e in prior['raw_events']}
    flexible = {e['event_id']: flexible_curve_evidence(e) for e in verified if variant.startswith(('pass_v22', 'pass_v23', 'pass_v24', 'pass_v25')) and np.median(ridge(e)[:, 1]) > 1600.0}
    result['flexible_curve_evidence'] = flexible
    base = [e for e in prior['raw_events'] if e['event_id'].startswith('event-')]
    groups, initial_joins = catalog(base, 'coherent_adaptive')
    families = [set(p['raw_event_ids']) for p in groups]
    joins = []
    for decision in initial_joins:
        left_id, right_id = decision['supporting_pair']
        relation = trajectory_relation(originals[left_id], originals[right_id])
        supporting_pairs = [] if relation is None else [[left_id, right_id, relation]]
        joins.append(dict(kind='baseline_family_reconciliation', left=decision['left'], right=decision['right'], supporting_pairs=supporting_pairs, initial_merge_evidence=decision['evidence']))
    changed = True
    while changed:
        changed = False
        for i in range(len(families)):
            for j in range(i + 1, len(families)):
                pairs = [(a, b, trajectory_relation(originals[a], originals[b])) for a in sorted(families[i]) for b in sorted(families[j])]
                supporting = [(a, b, r) for a, b, r in pairs if r is not None]
                joint_support = []
                if variant.startswith('pass_') and (not supporting):
                    left = [originals[eid] for eid in sorted(families[i])]
                    right = [originals[eid] for eid in sorted(families[j])]
                    gap = max(min((e['start_s'] for e in left)), min((e['start_s'] for e in right))) - min(max((e['end_s'] for e in left)), max((e['end_s'] for e in right)))
                    if -5.0 <= gap <= 5.0:
                        for a in left:
                            for b in right:
                                if max(a['end_s'] - a['start_s'], b['end_s'] - b['start_s']) >= 20.0:
                                    joint = joint_pass_evidence([a, b])
                                    if joint['compatible']:
                                        joint_support.append((a['event_id'], b['event_id'], joint))
                if not supporting and len(joint_support) < 2:
                    continue
                overlapping = sum((baseline._overlap(ridge(originals[a]), ridge(originals[b])) is not None for a, b, _ in pairs))
                if not joint_support and len(supporting) < max(1, 0.5 * overlapping):
                    continue
                joins.append(dict(kind='baseline_family_reconciliation', left=sorted(families[i]), right=sorted(families[j]), supporting_pairs=supporting, joint_pass_support=joint_support))
                families[i].update(families[j])
                families.pop(j)
                changed = True
                break
            if changed:
                break
    assigned = {eid for g in families for eid in g}
    unattached = []
    for e in verified:
        if e['event_id'] in assigned:
            continue
        candidates = []
        for i, family in enumerate(families):
            support = [(eid, trajectory_relation(originals[eid], e)) for eid in sorted(family)]
            support = [(eid, r) for eid, r in support if r is not None]
            if support:
                best = min(support, key=lambda p: p[1]['residual_q80_hz'])
                if len(support) >= 2 or best[1]['shared_duration_s'] >= 0.6 * (e['end_s'] - e['start_s']):
                    candidates.append((best[1]['residual_q80_hz'] / np.median(ridge(e)[:, 1]), i, best))
        if candidates:
            _, i, best = min(candidates)
            families[i].add(e['event_id'])
            assigned.add(e['event_id'])
            joins.append(dict(kind='sequence_attached_to_baseline_family', event_id=e['event_id'], family_seed=best[0], evidence=best[1]))
        else:
            unattached.append(e)
    selected = []
    contour_groups = []
    if variant.startswith(('pass_v23', 'pass_v24', 'pass_v25')):
        contour_groups, contour_decisions = group_moving_contours(unattached)
        joins += contour_decisions
        selected += contour_groups
        assigned.update((eid for group in contour_groups for eid in group))
    pool = result['acceptance_decisions'] if variant.startswith(('pass_v18', 'pass_v21', 'pass_v22', 'pass_v23', 'pass_v24', 'pass_v25')) else result['events']
    for p in pool:
        ids = set(p['raw_event_ids']) - assigned
        if ids and (not any((eid.startswith('event-') for eid in ids))):
            if variant.startswith(('anchored_tight', 'pass_')):
                candidates = [e for e in verified if e['event_id'] in ids]
                strong = []
                for e in candidates:
                    c = result['curve_evidence'][e['event_id']]
                    parametric = c['motion_resolved'] and c['fit_fraction'] <= 0.05 and (c['residual_q99_hz'] <= max(6.0, 0.12 * c['excursion_hz'])) and (c['monotonicity'] >= 0.8) and (c['frequency_ratio'] <= 3.0)
                    if parametric or flexible.get(e['event_id'], {}).get('accepted', False):
                        strong.append(e)
                independent = []
                for e in strong:
                    if not any((abs(np.log(np.median(ridge(e)[:, 1]) / np.median(ridge(m)[:, 1]))) < 0.08 for m in independent)):
                        independent.append(e)
                if variant.startswith('pass_'):
                    strong = [e for e in strong if e['quality']['median_verified_snr_db'] >= 12.0]
                    independent = [e for e in independent if e['quality']['median_verified_snr_db'] >= 12.0]
                    descending = [e for e in strong if result['curve_evidence'][e['event_id']]['direction'] == 'descending']
                    brief = [e for e in descending if e['end_s'] - e['start_s'] < 12.0 and e['quality']['median_verified_snr_db'] >= 18.0 and (result['curve_evidence'][e['event_id']]['fit_fraction'] <= 0.03)]
                    brief += [e for e in descending if flexible.get(e['event_id'], {}).get('accepted', False)]
                    if not descending or (len(independent) < 2 and (not brief) and (max((e['quality']['median_verified_snr_db'] for e in descending)) < 24.0)):
                        continue
                elif not strong or (len(independent) < 2 and max((e['quality']['median_verified_snr_db'] for e in strong)) < 24.0):
                    continue
                ids = {e['event_id'] for e in strong}
            selected.append(ids)
            assigned.update(ids)
    verified_lookup = {e['event_id']: e for e in verified}
    all_groups = families + selected
    proposals = []
    if variant.startswith(('pass_v21', 'pass_v22', 'pass_v23', 'pass_v24', 'pass_v25')):

        def representatives(ids):
            choices = sorted((verified_lookup.get(eid, originals[eid]) for eid in ids), key=lambda e: (result['curve_evidence'].get(e['event_id'], {}).get('fit_fraction', 1.0), e['event_id']))
            kept = []
            for e in choices:
                if not any((abs(np.log(np.median(ridge(e)[:, 1]) / np.median(ridge(a)[:, 1]))) < 0.08 for a in kept)):
                    kept.append(e)
                if len(kept) == 3:
                    break
            return kept
        retained = []
        for ids in all_groups:
            tracks = representatives(ids)
            directional = [result['curve_evidence'].get(e['event_id'], {}).get('direction') for e in tracks]
            if not any((eid.startswith('event-') for eid in ids)) and (not any((group <= ids for group in contour_groups))) and (len(tracks) > 1) and all((d == 'descending' for d in directional)):
                joint = joint_pass_evidence(tracks)
                if not joint['compatible']:
                    joins.append(dict(kind='inconsistent_joint_pass_rejected', raw_event_ids=sorted(ids), joint_pass=joint))
                    continue
            retained.append(ids)
        all_groups = retained
        changed = True
        while changed:
            changed = False
            for i in range(len(all_groups)):
                for j in range(i + 1, len(all_groups)):
                    if any((e.startswith('event-') for e in all_groups[i])) and any((e.startswith('event-') for e in all_groups[j])):
                        continue
                    a = representatives(all_groups[i])
                    b = representatives(all_groups[j])
                    begin = max(min((e['start_s'] for e in a)), min((e['start_s'] for e in b)))
                    end = min(max((e['end_s'] for e in a)), max((e['end_s'] for e in b)))
                    if begin - end > 2.0:
                        continue
                    joint = joint_pass_evidence(a + b)
                    if not joint['compatible']:
                        continue
                    joins.append(dict(kind='shared_joint_pass_reconciliation', left=sorted(all_groups[i]), right=sorted(all_groups[j]), joint_pass=joint))
                    all_groups[i].update(all_groups[j])
                    all_groups.pop(j)
                    changed = True
                    break
                if changed:
                    break
    for group in all_groups:
        members = [verified_lookup.get(eid, originals[eid]) for eid in sorted(group)]
        token = '|'.join(sorted(group))
        p = canonical(members, 'anchored-' + hashlib.sha256(token.encode()).hexdigest()[:16], 'Baseline family anchors plus measured shared sequence trajectories; no temporal-only suppression')
        if variant.startswith('pass_v25'):
            p.update(boundary_basis='Full independently measured support of reconciled tracks; no fitted extrapolation')
        elif variant.startswith('pass_'):
            anchor_ids = {eid for eid in group if eid.startswith('event-')}
            anchors = [q for q in groups if set(q['raw_event_ids']) & anchor_ids]
            if anchors:
                p.update(observed_support_start_s=p['start_s'], observed_support_end_s=p['end_s'], start_s=min((q['start_s'] for q in anchors)), end_s=max((q['end_s'] for q in anchors)), boundary_basis='Original baseline motion support of reconciled anchors; sequence context retained separately')
        proposals.append(p)
    result.update(events=sorted(proposals, key=lambda p: (p['start_s'], p['event_id'])), variant=variant, grouping_decisions=joins, scope='Acoustic pattern proposals; source and physical motion unverified')
    return result

def split_upper_motions(events):
    output = []
    for event in events:
        if event['event_id'].startswith('event-'):
            continue
        a = ridge(event)
        t, f, w = a.T
        if np.median(f) < 1600.0 or np.ptp(t) < 8.0:
            continue
        smooth = median_filter(f, size=5, mode='nearest')
        prominence = max(8.0, 0.03 * np.median(f))
        maxima = signal.find_peaks(smooth, prominence=prominence)[0]
        minima = signal.find_peaks(-smooth, prominence=prominence)[0]
        cuts = sorted(set([0, len(t) - 1, *maxima.tolist(), *minima.tolist()]))
        if len(cuts) == 2:
            continue
        for number, (first, last) in enumerate(zip(cuts, cuts[1:])):
            part = a[first:last + 1]
            if np.ptp(part[:, 0]) < 4.0 or np.ptp(part[:, 1]) < 0.025 * np.median(part[:, 1]):
                continue
            e = dict(event, event_id=event['event_id'] + f'-part-{number}', parent_event_id=event['event_id'], start_s=float(part[0, 0]), end_s=float(part[-1, 0]), band_hz=[float(part[:, 1].min() - 2), float(part[:, 1].max() + 2)], ridge=[dict(time_s=float(x), frequency_hz=float(y), weight=float(z)) for x, y, z in part])
            output.append(e)
    return output

def flexible_curve_evidence(event):
    a = ridge(event)
    t, f, _ = a.T
    if len(t) < 10:
        return dict(accepted=False, reason='few_measurements')
    smooth = median_filter(f, size=3, mode='nearest')
    excursion = float(np.quantile(smooth, 0.95) - np.quantile(smooth, 0.05))
    monotonicity = abs(float(smooth[-1] - smooth[0])) / max(float(np.sum(abs(np.diff(smooth)))), 1e-09)
    train = np.arange(0, len(t), 2)
    test = np.arange(1, len(t) - 1, 2)
    prediction = PchipInterpolator(t[train], f[train], extrapolate=False)(t[test])
    slope = np.gradient(smooth, t)[test]
    resolution = event['quality'].get('frequency_resolution_hz', 2.0)
    uncertainty = np.maximum(3 * resolution, np.abs(slope) * np.median(np.diff(t)) * 0.5)
    standardized = abs(prediction - f[test]) / uncertainty
    q90 = float(np.nanquantile(standardized, 0.9))
    accepted = bool(event.get('parent_event_id') and np.ptp(t) >= 5.0 and (monotonicity >= 0.85) and (excursion >= 0.05 * np.median(f)) and (q90 <= 2.0) and (event['quality']['median_verified_snr_db'] >= 12.0) and (event['quality']['visible_fraction'] >= 0.8))
    return dict(accepted=accepted, heldout_standardized_residual_q90=q90, monotonicity=monotonicity, excursion_hz=excursion, method='Alternating measured frames: monotone cubic forecast scaled by FFT and within-frame chirp uncertainty')

def group_moving_contours(events):
    candidates = []
    stats = {}
    for event in events:
        a = ridge(event)
        t, f, _ = a.T
        if np.ptp(t) < 12.0:
            continue
        lf = median_filter(np.log(f), size=5, mode='nearest')
        excursion = float(np.quantile(lf, 0.95) - np.quantile(lf, 0.05))
        moving = float(np.mean(abs(np.gradient(lf, t)) > 0.005))
        variation = float(np.sum(abs(np.diff(lf)))) / max(excursion, 1e-08)
        q = event['quality']
        if moving < 0.6 or variation > 3.0 or excursion < 0.06 or (q['visible_fraction'] < 0.8) or (q['median_verified_snr_db'] < 14.0):
            continue
        candidates.append(event)
        stats[event['event_id']] = dict(moving_fraction=moving, variation_ratio=variation, fractional_excursion=excursion)
    groups = []
    decisions = []
    used = set()
    for seed in sorted(candidates, key=lambda e: (-(e['end_s'] - e['start_s']) * e['quality']['median_verified_snr_db'], e['event_id'])):
        if seed['event_id'] in used or seed['quality']['median_verified_snr_db'] < 18.0 or seed['quality']['visible_fraction'] < 0.9:
            continue
        members = [seed]
        scales = [1.0]
        relationships = []
        for event in candidates:
            if event is seed or event['event_id'] in used:
                continue
            relation = trajectory_relation(seed, event)
            if relation is None or relation['shared_duration_s'] < 12.0 or relation['correlation'] < 0.98:
                continue
            resolution = max(seed['quality'].get('frequency_resolution_hz', 2.0), event['quality'].get('frequency_resolution_hz', 2.0))
            if relation['residual_q80_hz'] > max(2 * resolution, 0.005 * np.median(ridge(event)[:, 1])):
                continue
            members.append(event)
            relationships.append(dict(event_id=event['event_id'], **relation))
            if not any((abs(np.log(relation['scale'] / scale)) < 0.08 for scale in scales)):
                scales.append(relation['scale'])
        if len(scales) < 3:
            continue
        ids = {e['event_id'] for e in members}
        groups.append(ids)
        used.update(ids)
        decisions.append(dict(kind='measured_moving_harmonic_contour', seed=seed['event_id'], raw_event_ids=sorted(ids), independent_frequency_components=len(scales), trajectory_relationships=relationships, motion_evidence={e['event_id']: stats[e['event_id']] for e in members}, interpretation='Precisely shared moving harmonic contour; intrinsic FM versus physical Doppler remains unverified'))
    return (groups, decisions)
