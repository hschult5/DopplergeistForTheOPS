'use strict';
const $ = id => document.getElementById(id);
const terminal = new Set(['needs_input', 'succeeded', 'inconclusive', 'failed', 'cancelled']);
let activeJob = null, renderedJob = null, timer = null, maxBytes = 100 * 1024 * 1024;
const node = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
// Older services and unavailable capability responses expose only legacy CONUS.
const CONUS_PROFILE = {country: 'US', coverage: 'conus', name: 'Contiguous United States (CONUS)', bounds_wsen: [-125, 24, -66, 50]};
let countryProfiles = [CONUS_PROFILE], areaControl = null, countrySelectionSupported = false;
const BOUND_INPUTS = ['bbox-w', 'bbox-s', 'bbox-e', 'bbox-n'];

function selectedProfile() {
  const profile = countryProfiles.find(item => item.country === $('country').value);
  if (!profile) throw new Error('Choose a country supported by this service.');
  return profile;
}

function setCountryProfiles(values, defaultCountry = 'US') {
  const profiles = values === undefined ? [CONUS_PROFILE] : values;
  const countries = new Set();
  if (!Array.isArray(profiles) || !profiles.length || profiles.some(profile => {
    if (!profile || !/^[A-Z]{2}$/.test(profile.country) || typeof profile.coverage !== 'string' || !profile.coverage
        || typeof profile.name !== 'string' || !profile.name.trim() || countries.has(profile.country)) return true;
    countries.add(profile.country);
    const b = profile.bounds_wsen;
    return !Array.isArray(b) || b.length !== 4 || b.some(value => !Number.isFinite(value))
      || !(-180 <= b[0] && b[0] < b[2] && b[2] <= 180 && -90 <= b[1] && b[1] < b[3] && b[3] <= 90);
  })) throw new Error('The service returned an invalid country catalog.');
  const previous = countryProfiles.find(profile => profile.country === $('country').value);
  countryProfiles = profiles.map(profile => ({...profile, bounds_wsen: profile.bounds_wsen.slice()}));
  countrySelectionSupported = values !== undefined;
  $('country').replaceChildren(...countryProfiles.map(profile => {
    const option = node('option', profile.name); option.value = profile.country; return option;
  }));
  $('country').value = countries.has(previous?.country) ? previous.country
    : (countries.has(defaultCountry) ? defaultCountry : countryProfiles[0].country);
  const current = selectedProfile();
  const changed = !previous || previous.country !== current.country
    || previous.bounds_wsen.some((value, index) => value !== current.bounds_wsen[index]);
  if (areaControl) areaControl.refreshCountry(changed);
  $('supported-areas').textContent = countryProfiles.map(profile => profile.name).join('; ');
  if (renderedJob) renderCoverage(renderedJob.coverage, renderedJob.status);
}

function renderCoverage(coverage, status) {
  $('coverage').replaceChildren();
  const card = node('div', undefined, 'candidate');
  card.append(node('strong', 'Search & data coverage'));
  if (!coverage) {
    card.append(node('p', 'Search and data coverage are not established.'));
    $('coverage').append(card);
    return;
  }
  const area = coverage.requested_area;
  if (area) {
    const profile = countryProfiles.find(item => (!area.country || item.country === area.country)
      && (!area.coverage || item.coverage === area.coverage));
    const name = profile?.name || ({US: CONUS_PROFILE.name, DE: 'Germany'})[area.country]
      || ({conus: CONUS_PROFILE.name, germany: 'Germany'})[area.coverage] || area.country || area.coverage || 'unspecified';
    const bounds = area.bounds_wsen;
    const full = bounds && profile && bounds.every((value, index) => value === profile.bounds_wsen[index]);
    const label = bounds && !full
      ? `Requested ${name} bounds (west, south, east, north): ${bounds.map(value => typeof value === 'number' ? value.toFixed(6) : value).join(', ')}`
      : `Requested area: ${name}`;
    card.append(node('p', label));
  }
  if (status === 'queued' || status === 'running') {
    card.append(node('p', status === 'queued'
      ? 'Search queued · data coverage will be assessed after processing starts.'
      : 'Search in progress · final data coverage will be reported when processing finishes.'));
  } else {
    card.append(node('p', `Search ${coverage.search_complete ? 'complete' : 'incomplete'} · Data ${coverage.data_status}`));
    for (const limitation of coverage.limitations || []) card.append(node('p', limitation));
    if (coverage.missing_data?.length) card.append(node('p', `Missing: ${coverage.missing_data.join('; ')}`));
  }
  $('coverage').append(card);
}
async function api(path, options) {
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
  return value;
}
function remember(id) {
  try {
    const recent = JSON.parse(localStorage.getItem('dopplergeist-jobs') || '[]');
    localStorage.setItem('dopplergeist-jobs', JSON.stringify([id, ...recent.filter(x => x !== id)].slice(0, 8)));
    renderRecent();
  } catch (_) { /* Private browser storage may be disabled. */ }
}
function renderRecent() {
  try {
    const ids = JSON.parse(localStorage.getItem('dopplergeist-jobs') || '[]');
    $('recent').replaceChildren();
    for (const id of ids) {
      if (!/^[0-9a-f]{32}$/.test(id)) continue;
      const li = node('li'); const a = node('a', id.slice(0, 12)); a.href = `/?job=${id}`;
      li.append(a); $('recent').append(li);
    }
    $('recent-panel').hidden = !$('recent').children.length;
  } catch (_) { /* Optional recent-job links. */ }
}
async function artifacts(id) {
  const response = await api(`/api/jobs/${id}/artifacts`);
  if (id !== activeJob) return;
  $('resume').hidden = !['cancelled', 'failed', 'inconclusive'].includes($('status').textContent) || !response.artifacts.some(item => item.path === 'resume.json');
  $('artifacts').replaceChildren(); $('diagnostics').replaceChildren();
  for (const item of response.artifacts) {
    const href = `/api/jobs/${id}/artifacts/${item.path.split('/').map(encodeURIComponent).join('/')}`;
    const li = node('li'); const a = node('a', item.path); a.href = href; a.download = '';
    li.append(a, node('span', ` · ${(item.bytes / 1024).toFixed(1)} KB`, 'muted')); $('artifacts').append(li);
    if (/\.png$/i.test(item.path) && /event|spectro|discovery|model|location/i.test(item.path)) {
      const figure = node('figure', undefined, 'diagnostic'); const img = node('img');
      img.src = href; img.alt = `Analysis evidence: ${item.path}`; img.loading = 'lazy';
      figure.append(img, node('figcaption', item.path)); $('diagnostics').append(figure);
    }
  }
}
function coordinateCopyControl(lat, lon) {
  const control = node('div', undefined, 'coordinate-copy');
  const button = node('button', 'Copy coordinates', 'secondary');
  const status = node('span', '', 'help');
  const coordinates = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
  let resetTimer = null;

  button.type = 'button';
  button.setAttribute('aria-label', `Copy coordinates ${coordinates}`);
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  status.setAttribute('aria-atomic', 'true');

  button.addEventListener('click', async () => {
    clearTimeout(resetTimer);
    status.textContent = '';
    button.disabled = true;
    try {
      if (!navigator.clipboard
          || typeof navigator.clipboard.writeText !== 'function') {
        throw new Error('Clipboard unavailable');
      }
      await navigator.clipboard.writeText(coordinates);
      status.textContent = 'Copied!';
      resetTimer = setTimeout(() => { status.textContent = ''; }, 2500);
    } catch (_) {
      status.textContent = 'Copy failed. Select and copy the coordinates above.';
    } finally {
      button.disabled = false;
    }
  });

  control.append(button, status);
  return control;
}

function render(value) {
  renderedJob = value;
  $('empty').hidden = true; $('job').hidden = false;
  $('job-id').textContent = `JOB ${value.job_id}`;
  $('status').textContent = value.status.replaceAll('_', ' ');
  $('stage').textContent = value.stage || '';
  const progress = $('progress');
  if (value.progress_stale) {
    progress.removeAttribute('value');
    progress.setAttribute('aria-label', 'Progress unavailable because the saved percentage is stale');
  } else {
    progress.value = Number.isFinite(value.progress) ? value.progress : (terminal.has(value.status) ? 1 : 0);
    progress.setAttribute('aria-label', 'Analysis progress');
  }
  $('cancel').hidden = terminal.has(value.status);
  $('resume').hidden = true;
  const staleAge = Number.isFinite(value.progress_age_seconds)
    ? ` Last progress update was ${Math.floor(value.progress_age_seconds)} seconds ago.` : '';
  $('message').textContent = value.progress_stale
    ? `${value.message || 'The saved percentage is stale and hidden.'}${staleAge}`
    : (value.message || 'Analysis is running. You can return to this job using its saved link.');
  $('required').hidden = !value.required_fields?.length;
  $('required').textContent = value.required_fields?.length ? `Required input: ${value.required_fields.join(', ')}. Add the evidence to a new analysis; this run is preserved.` : '';
  $('guidance').replaceChildren(); $('guidance').hidden = true;
  renderCoverage(value.coverage, value.status);
  $('candidates').replaceChildren();
  if (value.candidates?.length) {
    $('candidates').append(node('h3', value.status === 'succeeded' ? 'Supported location & alternatives' : 'Location hypotheses · result remains inconclusive'));
    let additional = null;
    value.candidates.forEach((candidate, i) => {
      const card = node('div', undefined, 'candidate');
      card.append(node('strong', `${i + 1}. ${candidate.lat.toFixed(6)}, ${candidate.lon.toFixed(6)}`));
      if (candidate.uncertainty) {
        const u = candidate.uncertainty;
        card.append(node('p', `${u.kind === 'calibrated' ? 'Calibrated uncertainty' : 'Sensitivity radius'}: ${u.radius_m.toFixed(0)} m · ${u.method.replaceAll('_', ' ')}`));
        if (u.coverage_probability !== undefined) card.append(node('p', `Calibrated coverage: ${(100 * u.coverage_probability).toFixed(0)}% · ${u.calibration_reference}`));
      } else card.append(node('p', 'Location uncertainty was not established.'));
      card.append(node('p', `Score ${candidate.score.toPrecision(5)} (${candidate.score_kind.replaceAll('_', ' ')}); not a probability.`));
      card.append(node('p', `Flights: ${candidate.flight_ids.join(', ')} · Recording start: ${candidate.recording_start_utc}`));
      const map = node('a', 'View coordinate on OpenStreetMap');
      map.href = `https://www.openstreetmap.org/?mlat=${candidate.lat}&mlon=${candidate.lon}#map=14/${candidate.lat}/${candidate.lon}`;
      map.target = '_blank'; map.rel = 'noopener noreferrer'; card.append(map);
      if (Number.isFinite(candidate.lat) && Number.isFinite(candidate.lon)) {
        card.append(coordinateCopyControl(candidate.lat, candidate.lon));
      }
      if (i < 5) $('candidates').append(card);
      else {
        if (!additional) { additional = node('details'); additional.append(node('summary', `${value.candidates.length - 5} additional hypotheses`)); $('candidates').append(additional); }
        additional.append(card);
      }
    });
  }
  $('events').replaceChildren();
  if (value.events?.length) {
    const groups = new Set(value.events.map(event => event.harmonic_group_id || event.event_id)).size;
    $('events').append(node('h3', `${groups} acoustic proposal groups · ${value.events.length} ridge records`));
    for (const event of value.events) $('events').append(node('div', `${event.event_id} · ${event.start_s.toFixed(1)}–${event.end_s.toFixed(1)} s · ${event.band_hz.map(x => x.toFixed(0)).join('–')} Hz${event.harmonic_group_id ? ` · group ${event.harmonic_group_id}` : ''}`, 'event'));
  }
}
async function readGuidance(value) {
  const id = value.job_id;
  try {
    const advice = await api(`/api/jobs/${id}/guidance`);
    if (id !== activeJob || renderedJob !== value || advice.status !== value.status || advice.reason_code !== (value.reason_code || null)) return;
    if (!advice.items?.length) return;
    const panel = $('guidance');
    panel.replaceChildren(node('h3', 'Next steps'));
    panel.append(node('p', 'These suggestions are not ranked. Their benefit has not been measured.', 'help'));
    for (const item of advice.items) {
      const card = node('div', undefined, 'candidate');
      card.append(node('p', item.observation), node('p', item.suggestion));
      const detail = node('details'); detail.append(node('summary', 'Evidence behind this suggestion'));
      const list = node('ul');
      for (const reference of item.evidence || []) {
        const li = node('li');
        const [artifact] = reference.split('#');
        // Only link the two local artifacts emitted by the guidance producer.
        if (artifact === 'result.json' || artifact === 'decision.json') {
          const link = node('a', reference);
          link.href = `/api/jobs/${id}/artifacts/${artifact}`; link.download = '';
          li.append(link);
        } else li.textContent = reference;
        list.append(li);
      }
      detail.append(list); card.append(detail); panel.append(card);
    }
    panel.hidden = false;
  } catch (_) {
    if (id !== activeJob || renderedJob !== value) return;
    $('guidance').replaceChildren(node('p', 'Next steps could not be read. The saved result and artifacts remain available.', 'help'));
    $('guidance').hidden = false;
  }
}
async function poll(id) {
  clearTimeout(timer);
  try {
    const value = await api(`/api/jobs/${id}`);
    if (id !== activeJob) return;
    $('connection').textContent = ''; render(value);
    if (terminal.has(value.status)) await Promise.all([artifacts(id), readGuidance(value)]);
    else timer = setTimeout(() => poll(id), 1000);
  } catch (error) {
    if (id !== activeJob) return;
    $('connection').textContent = `Could not read this job: ${error.message}. Start the service with the same jobs directory to reconnect.`;
    timer = setTimeout(() => poll(id), 5000);
  }
}
function selectJob(id) {
  activeJob = id;
  history.replaceState(null, '', `/?job=${encodeURIComponent(id)}`);
  remember(id); poll(id);
}
const defaultFileLabel = $('file-label').textContent;
$('audio').addEventListener('change', () => {
  $('file-label').textContent = $('audio').files[0]?.name || defaultFileLabel;
});
$('upload-form').addEventListener('submit', async event => {
  event.preventDefault(); $('upload-error').textContent = '';
  const file = $('audio').files[0];
  if (!file || file.size <= 0 || file.size > maxBytes) { $('upload-error').textContent = `Choose a nonempty recording up to ${(maxBytes / 1024 / 1024).toFixed(0)} MB.`; return; }
  const start = $('start').value, end = $('end').value;
  if (end && !start) { $('upload-error').textContent = 'Enter the earliest recording start as well.'; return; }
  if (start && end && end < start) { $('upload-error').textContent = 'The latest start must be at or after the earliest start.'; return; }
  let bounds;
  try { bounds = readSearchBounds(); }
  catch (error) { $('upload-error').textContent = error.message; return; }
  const profile = selectedProfile();
  const request = {schema_version: 1, search: {coverage: profile.coverage, bounds_wsen: bounds}};
  if (countrySelectionSupported) request.search.country = profile.country;
  if (start) {
    request.time_window = {start_utc: start + 'Z', end_utc: (end || start) + 'Z', source: 'user'};
    if ($('time-source').value.trim()) request.time_window.source_detail = $('time-source').value.trim();
  }
  // JSON escapes preserve Unicode notes in the ASCII-only HTTP metadata header.
  const requestMetadata = JSON.stringify(request).replace(/[^\x20-\x7e]/g, character => '\\u' + character.charCodeAt(0).toString(16).padStart(4, '0'));
  $('submit').disabled = true;
  try {
    const value = await api('/api/jobs', {method: 'POST', headers: {'Content-Type': 'application/octet-stream', 'X-Dopplergeist-Request': requestMetadata, 'X-Filename': 'input.' + (file.name.split('.').pop() || 'mp3').replace(/[^a-z0-9]/gi, '')}, body: file});
    render(value); selectJob(value.job_id);
  } catch (error) { $('upload-error').textContent = error.message; }
  finally { $('submit').disabled = false; }
});
$('cancel').addEventListener('click', async () => {
  try { const value = await api(`/api/jobs/${activeJob}/cancel`, {method: 'POST'}); render(value); await artifacts(activeJob); }
  catch (error) { $('connection').textContent = error.message; }
});
$('resume').addEventListener('click', async () => {
  try { const value = await api(`/api/jobs/${activeJob}/resume`, {method: 'POST'}); render(value); poll(activeJob); }
  catch (error) { $('connection').textContent = error.message; }
});
api('/api/capabilities').then(value => {
  setCountryProfiles(value.countries, value.default_country);
  maxBytes = value.max_upload_bytes;
  $('limits').textContent = `Up to ${(maxBytes / 1024 / 1024).toFixed(0)} MB per file. Analysis budget: ${(value.max_runtime_s / 60).toFixed(0)} minutes per job.`;
  if (value.provider_configured === false) {
    $('data-setup').textContent = 'Historical flight data is not configured. Restart the service with --online to download public data, or provide an offline configuration. Downloads and indexes can require tens of gigabytes or more and hours.';
    $('data-setup').hidden = false;
  } else if (value.online_enabled) {
    $('data-setup').textContent = 'Public flight and terrain data may be downloaded to the local cache. Archives and indexes can require tens of gigabytes or more and hours.';
    $('data-setup').hidden = false;
  }
}).catch(error => {
  setCountryProfiles(undefined);
  $('upload-error').textContent = `Could not read service settings: ${error.message}. Only CONUS is available.`;
});
renderRecent();
const savedJob = new URLSearchParams(location.search).get('job');
if (savedJob && /^[0-9a-f]{32}$/.test(savedJob)) selectJob(savedJob);

function validateSearchBounds(bounds) {
  if (bounds.length !== 4 || bounds.some(value => !Number.isFinite(value))) {
    throw new Error('Enter all four search coordinates as finite numbers.');
  }
  const [w, s, e, n] = bounds;
  if (w >= e || s >= n) {
    throw new Error('West must be less than east and south less than north. Antimeridian crossing is unsupported.');
  }
  const profile = selectedProfile();
  const [west, south, east, north] = profile.bounds_wsen;
  if (w < west || s < south || e > east || n > north) {
    throw new Error(`Choose an area within ${profile.name}: west ${west}°, south ${south}°, east ${east}°, north ${north}°.`);
  }
  return bounds;
}

function readSearchBounds() {
  const values = BOUND_INPUTS.map(id => $(id).value.trim());
  if (values.some(value => value === '')) throw new Error('Enter all four search coordinates.');
  return validateSearchBounds(values.map(Number));
}

function boundsMessage(message = '') {
  $('bounds-error').textContent = message;
  $('bounds-error').hidden = !message;
}

function initMap() {
  let map, rectangle, preview, drawOrigin, panStart;
  let selectedBounds = selectedProfile().bounds_wsen.slice();

  function setBounds(bounds, fit = false, updateInputs = true) {
    selectedBounds = validateSearchBounds(bounds.slice());
    if (updateInputs) BOUND_INPUTS.forEach((id, index) => { $(id).value = String(selectedBounds[index]); });
    if (map) {
      const [w, s, e, n] = selectedBounds;
      const corners = [[s, w], [n, e]];
      if (rectangle) rectangle.setBounds(corners);
      else rectangle = L.rectangle(corners, {
        color: '#baf078', weight: 2, fillColor: '#baf078', fillOpacity: 0.18, interactive: false
      }).addTo(map);
      if (fit) map.fitBounds(corners, {padding: [20, 20]});
    }
    boundsMessage();
  }

  function refreshCountry(reset = true) {
    const profile = selectedProfile();
    const [w, s, e, n] = profile.bounds_wsen;
    BOUND_INPUTS.forEach((id, index) => {
      $(id).min = String(index % 2 === 0 ? w : s);
      $(id).max = String(index % 2 === 0 ? e : n);
    });
    $('country-help').textContent = `Choose the full ${profile.name} area or a smaller rectangle.`
      + (profile.country === 'DE' ? ' Only locations within Germany are searched; nearby aircraft across its borders remain eligible.' : '');
    if (profile.country === 'DE') {
      const source = node('a', '© BKG 2026'); source.href = 'https://www.bkg.bund.de';
      const license = node('a', 'dl-de/by-2-0'); license.href = 'https://www.govdata.de/dl-de/by-2-0';
      for (const link of [source, license]) { link.target = '_blank'; link.rel = 'noopener noreferrer'; }
      $('country-help').append(' Boundary data: ', source, ', ', license, '.');
    }
    if (map) {
      const dx = Math.max(1, (e - w) * 0.1), dy = Math.max(1, (n - s) * 0.15);
      map.setMaxBounds([[Math.max(-90, s - dy), Math.max(-180, w - dx)],
        [Math.min(90, n + dy), Math.min(180, e + dx)]]);
    }
    if (reset) {
      panStart = null;
      clearDrawing();
      setBounds(profile.bounds_wsen, true);
    }
  }
  areaControl = {refreshCountry};
  $('country').addEventListener('change', () => refreshCountry(true));
  BOUND_INPUTS.forEach(id => {
    $(id).addEventListener('input', () => {
      try { setBounds(readSearchBounds(), false, false); }
      catch (error) { boundsMessage(error.message); }
    });
  });
  $('reset-bounds').addEventListener('click', () => setBounds(selectedProfile().bounds_wsen, true));
  refreshCountry(true);

  if (typeof L === 'undefined') {
    $('map-container').hidden = true;
    $('map-status').textContent = 'The map could not load. Enter search coordinates below.';
    return;
  }
  map = L.map('map', {maxBoundsViscosity: 1});
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18, noWrap: true,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).on('tileerror', () => {
    $('map-status').textContent = 'Map tiles are unavailable. Coordinates and the selection rectangle still work.';
  }).addTo(map);
  map.dragging.disable(); // Left-drag draws; right-drag or Shift-drag pans.
  refreshCountry(true);

  const container = map.getContainer();
  container.addEventListener('contextmenu', event => event.preventDefault());
  container.addEventListener('mousedown', event => {
    if (event.button === 2 || (event.button === 0 && event.shiftKey)) {
      panStart = {x: event.clientX, y: event.clientY};
      container.style.cursor = 'grabbing';
      event.preventDefault();
      event.stopPropagation();
    }
  });
  window.addEventListener('mousemove', event => {
    if (!panStart) return;
    map.panBy([panStart.x - event.clientX, panStart.y - event.clientY], {animate: false});
    panStart = {x: event.clientX, y: event.clientY};
  });

  function clearDrawing() {
    if (preview) map.removeLayer(preview);
    preview = null;
    drawOrigin = null;
  }
  window.addEventListener('mouseup', () => {
    panStart = null;
    container.style.cursor = '';
    // Releasing outside the map cancels an unfinished selection.
    clearDrawing();
  });
  window.addEventListener('blur', () => {
    panStart = null;
    container.style.cursor = '';
    clearDrawing();
  });

  map.on('mousedown', event => {
    if (event.originalEvent.button !== 0 || event.originalEvent.shiftKey) return;
    drawOrigin = event.latlng;
    preview = L.rectangle([drawOrigin, drawOrigin], {
      color: '#baf078', weight: 2, dashArray: '4, 4', fillOpacity: 0.25, interactive: false
    }).addTo(map);
  });
  map.on('mousemove', event => {
    if (preview && drawOrigin) preview.setBounds([drawOrigin, event.latlng]);
  });
  map.on('mouseup', event => {
    if (!drawOrigin || !preview) return;
    const origin = drawOrigin;
    clearDrawing();
    if (origin.equals(event.latlng)) return;
    const bounds = [Math.min(origin.lng, event.latlng.lng), Math.min(origin.lat, event.latlng.lat),
      Math.max(origin.lng, event.latlng.lng), Math.max(origin.lat, event.latlng.lat)]
      .map(value => Number(value.toFixed(6)));
    try { setBounds(bounds); }
    catch (error) { boundsMessage('Search area was not changed. ' + error.message); }
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initMap);
else initMap();
