/* Fitmon route library and route planner (specs/route-planner.md). Loaded before app.js, which
   calls window.fitmonRoutes(ctx) with its helpers and adds the returned views to its router:
   #routes (the library), #route/<id> (one route), #planner/<id>[,<km>] or #planner/new. */
window.fitmonRoutes = (ctx) => {
'use strict';
const {api, $, esc, fmt, toast, go, state, chart, statute} = ctx;

const ROUTE_COLORS = {run: '#e4572e', hike: '#d98e04', road: '#0b6bcb', gravel: '#8a5a2b', mtb: '#2a9d8f', bike: '#4a6fd1'};
const CAND_COLORS = ['#d81b60', '#8e24aa', '#f4511e', '#00897b', '#3949ab'];
const KIND_LABELS = {loop: 'loop', out_back: 'out and back', a_b: 'A to B'};
const routeColor = (sport) => ROUTE_COLORS[sport] || '#0b6bcb';
const unitM = () => (statute() ? 1609.344 : 1000);
const unitName = () => (statute() ? 'mi' : 'km');
const km = (m) => (m == null ? '' : fmt.dist(m, 'running'));
const isFoot = (sport) => sport === 'run' || sport === 'hike';
let SPORTS = {run: 'Run', hike: 'Hike / walk', road: 'Road bike', gravel: 'Gravel', mtb: 'Mountain bike', bike: 'Bike'};

// ---------------------------------------------------------------- shared helpers
function baseMap(id) {
    // OpenStreetMap's tiles, sending the origin as Referer as their policy asks (see app.js drawMap).
    const map = L.map(id);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19, referrerPolicy: 'strict-origin-when-cross-origin',
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    map.setView([50.0, 8.66], 12);
    return map;
}
const flatten = (plan) => { const out = []; (plan || []).forEach((s) => { out.push(...(out.length ? s.coords.slice(1) : s.coords)); }); return out; };
function hav(a, b) {
    const r = Math.PI / 180, p1 = a[1] * r, p2 = b[1] * r;
    const h = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin((b[0] - a[0]) * r / 2) ** 2;
    return 2 * 6371000 * Math.asin(Math.min(1, Math.sqrt(h)));
}
function cumul(coords) { const out = [0]; for (let i = 1; i < coords.length; i++) out.push(out[i - 1] + hav(coords[i - 1], coords[i])); return out; }
const latlngs = (coords) => coords.map((c) => [c[1], c[0]]);
function sportOptions(sel, value) {
    if (!sel.options.length) sel.innerHTML = Object.entries(SPORTS).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join('');
    if (value) sel.value = value;
}
function summary(r, st) {
    const parts = [SPORTS[r.sport] || r.sport, km(st ? st.distance_m : r.distance_m)];
    const asc = st ? st.ascent_m : r.ascent_m, unp = st ? st.unpaved_pct : r.unpaved_pct;
    if (asc != null) parts.push(`${fmt.elev(asc)} ↑`);
    if (unp != null) parts.push(`${Math.round(unp)} % unpaved`);
    if (r.kind) parts.push(KIND_LABELS[r.kind] || r.kind);
    return parts.filter(Boolean).join(' · ');
}
function elevChart(id, elevation) {
    const pts = (elevation || []).filter((e) => e[1] != null);
    if (!pts.length) { if (state.charts[id]) { state.charts[id].destroy(); delete state.charts[id]; } return; }
    chart(id, {type: 'line', data: {labels: pts.map((e) => (e[0] / unitM()).toFixed(1)),
        datasets: [{label: statute() ? 'elevation (ft)' : 'elevation (m)', data: pts.map((e) => (statute() ? e[1] * 3.28084 : e[1])),
            borderColor: '#6a994e', backgroundColor: '#6a994e33', fill: true, pointRadius: 0, borderWidth: 1.3}]},
        options: {plugins: {legend: {display: false}}, scales: {x: {ticks: {maxTicksLimit: 10}, title: {display: true, text: unitName()}}}}});
}
function svgPreview(coords) {
    if (!coords || coords.length < 2) return '';
    const k = Math.cos(coords[0][1] * Math.PI / 180), xs = coords.map((c) => c[0] * k), ys = coords.map((c) => -c[1]);
    const x0 = Math.min(...xs), y0 = Math.min(...ys), w = Math.max(...xs) - x0 || 1e-6, h = Math.max(...ys) - y0 || 1e-6;
    const pad = Math.max(w, h) * 0.06;
    const pts = coords.map((c, i) => `${(xs[i] - x0 + pad).toFixed(6)},${(ys[i] - y0 + pad).toFixed(6)}`).join(' ');
    return `<svg viewBox="0 0 ${(w + 2 * pad).toFixed(6)} ${(h + 2 * pad).toFixed(6)}" preserveAspectRatio="xMidYMid meet"><polyline points="${pts}"/></svg>`;
}

// ---------------------------------------------------------------- the library
const lib = {map: null, layers: {}, lines: null};
async function loadRoutes() {
    if (!lib.map) {
        lib.map = baseMap('rt-map');
        let t; $('rt-search').addEventListener('input', () => { clearTimeout(t); t = setTimeout(loadRoutes, 250); });
        $('rt-sport').addEventListener('change', loadRoutes);
        $('rt-new').addEventListener('click', () => go('planner', 'new'));
    }
    setTimeout(() => lib.map.invalidateSize(), 30);
    const q = new URLSearchParams();
    if ($('rt-sport').value) q.set('sport', $('rt-sport').value);
    if ($('rt-search').value.trim()) q.set('q', $('rt-search').value.trim());
    const d = await api('/api/routes?' + q);
    if (!d._ok) return;
    SPORTS = d.sports || SPORTS;
    if ($('rt-sport').options.length <= 1) $('rt-sport').insertAdjacentHTML('beforeend', Object.entries(SPORTS).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join(''));
    // The first visit indexes older activities a few seconds at a time; keep asking until done.
    $('rt-pending').textContent = d.pending ? `Comparing ${d.pending} older activities with your routes…` : '';
    if (d.pending) setTimeout(() => { if (state.view === 'routes') loadRoutes(); }, 800);

    const ids = new Set(d.routes.map((r) => r.id)), kids = {};
    d.routes.forEach((r) => { if (r.parent_id && ids.has(r.parent_id)) (kids[r.parent_id] = kids[r.parent_id] || []).push(r); });
    const item = (r, variant) => `<div class="rt-item${variant ? ' variant' : ''}" data-id="${r.id}" style="--c:${routeColor(r.sport)}">
        <span class="rt-times" title="Activities that followed this route">${r.times ? `×${r.times}` : ''}</span>
        ${r.starred ? '★ ' : ''}<b>${esc(r.name)}</b> <span class="muted">${km(r.distance_m)}</span>
        <div class="rt-meta">${esc(summary(r).split(' · ').slice(2).join(' · '))}${r.last_done ? ` · last ${esc(fmt.day(r.last_done))}` : ''}</div></div>`;
    const html = d.routes.filter((r) => !(r.parent_id && ids.has(r.parent_id)))
        .map((r) => item(r) + (kids[r.id] || []).sort((a, b) => a.distance_m - b.distance_m).map((v) => item(v, true)).join('')).join('');
    $('rt-list').innerHTML = html || `<p class="muted">No routes yet. Open a run or ride and press <b>Save as route</b>, save one of the suggestions below, or start a <a data-goto="planner">new route</a>.</p>`;
    $('rt-list').querySelectorAll('.rt-item').forEach((el) => {
        el.addEventListener('click', () => go('route', el.dataset.id));
        el.addEventListener('mouseenter', () => hotLine(+el.dataset.id, true));
        el.addEventListener('mouseleave', () => hotLine(+el.dataset.id, false));
    });
    drawLibraryLines(d.routes);
    loadSuggestions();
}
async function drawLibraryLines(routes) {
    const d = await api('/api/routes/lines', {quiet: true});
    Object.values(lib.layers).forEach((l) => l.remove()); lib.layers = {};
    const shown = new Set(routes.map((r) => r.id)), group = L.featureGroup();
    (d.lines || []).filter((x) => shown.has(x.id)).forEach((x) => {
        const line = L.polyline(latlngs(x.coords), {color: routeColor(x.sport), weight: 3, opacity: 0.75});
        line.on('click', () => go('route', x.id));
        line.on('mouseover', () => { hotLine(x.id, true); const el = $('rt-list').querySelector(`[data-id="${x.id}"]`); if (el) el.classList.add('hot'); });
        line.on('mouseout', () => { hotLine(x.id, false); const el = $('rt-list').querySelector(`[data-id="${x.id}"]`); if (el) el.classList.remove('hot'); });
        lib.layers[x.id] = line; group.addLayer(line);
    });
    group.addTo(lib.map);
    // Fit after the map has re-measured itself (it may have been hidden), and only when the set
    // of routes shown changed - not on every refresh while older activities are being indexed.
    const key = [...shown].sort().join(',');
    if (group.getLayers().length && key !== lib.fitted) {
        lib.fitted = key;
        setTimeout(() => { lib.map.invalidateSize(); lib.map.fitBounds(group.getBounds(), {padding: [20, 20]}); }, 0);
    }
}
function hotLine(id, on) { const l = lib.layers[id]; if (l) { l.setStyle({weight: on ? 6 : 3, opacity: on ? 1 : 0.75}); if (on) l.bringToFront(); } }
async function loadSuggestions() {
    const d = await api('/api/routes/suggestions', {quiet: true});
    const list = d.suggestions || [];
    $('rt-suggest-panel').style.display = list.length ? '' : 'none';
    $('rt-suggest').innerHTML = list.map((s) => `<div class="rt-sugg" data-key="${esc(s.key)}">
        <b>${km(s.distance_m)} ${esc(SPORTS[s.sport] || '')} <span class="muted">${esc(KIND_LABELS[s.kind] || '')}</span></b>
        <div class="muted">${s.count} times · ${esc(fmt.day(s.first))} to ${esc(fmt.day(s.last))}</div>
        ${svgPreview(s.preview)}
        <div class="row"><button class="primary" data-save="${s.session_id}">Save as route</button>
            <button data-open="${s.session_id}">Latest</button><button data-hide="${esc(s.key)}" title="Not a route: stop suggesting it">Hide</button></div></div>`).join('');
    $('rt-suggest').querySelectorAll('[data-save]').forEach((b) => b.addEventListener('click', async () => {
        const name = prompt('Name for this route', `${km(list.find((s) => String(s.session_id) === b.dataset.save).distance_m)} route`);
        if (name == null) return;
        const r = await api(`/api/routes/from-session/${b.dataset.save}`, {method: 'POST', json: {name}});
        if (r._ok) { toast('Saved to your routes.'); go('route', r.route.id); }
    }));
    $('rt-suggest').querySelectorAll('[data-open]').forEach((b) => b.addEventListener('click', () => go('detail', b.dataset.open)));
    $('rt-suggest').querySelectorAll('[data-hide]').forEach((b) => b.addEventListener('click', async () => {
        await api('/api/routes/suggestions/hide', {method: 'POST', json: {key: b.dataset.hide}}); loadSuggestions();
    }));
}

// ---------------------------------------------------------------- one route
const one = {map: null, layer: null, data: null};
function wireRoutePage() {
    one.map = baseMap('rd-map');
    $('rd-edit').addEventListener('click', () => go('planner', one.data.route.id));
    $('rd-fit').addEventListener('click', () => { const v = Number($('rd-target').value); if (!v) { toast(`Give a length in ${unitName()}.`); return; } go('planner', `${one.data.route.id},${v}`); });
    $('rd-target').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('rd-fit').click(); });
    $('rd-star').addEventListener('click', () => updateRoute({starred: !one.data.route.starred}));
    const rename = () => { const n = prompt('Name for this route', one.data.route.name); if (n && n.trim()) updateRoute({name: n.trim()}).then((r) => { if (r._ok) toast('Renamed.'); }); };
    ['rd-rename', 'rd-name', 'rd-name-edit'].forEach((id) => $(id).addEventListener('click', rename));
    $('rd-sport').addEventListener('change', (e) => updateRoute({sport: e.target.value}));
    $('rd-notes-save').addEventListener('click', () => updateRoute({notes: $('rd-notes').value}).then(() => toast('Notes saved.')));
    $('rd-delete').addEventListener('click', async () => {
        if (!confirm(`Delete the route “${one.data.route.name}”? Its variants are kept. Your activities are not touched.`)) return;
        const r = await api(`/api/routes/${one.data.route.id}/delete`, {method: 'POST'}); if (r._ok) { toast('Route deleted.'); go('routes'); }
    });
    $('rd-rematch').addEventListener('click', async () => {
        const r = await api(`/api/routes/${one.data.route.id}/rematch`, {method: 'POST'});
        if (r._ok) { toast(`${r.matches} activit${r.matches === 1 ? 'y' : 'ies'} on this route.`); loadRoute(one.data.route.id); }
    });
}
async function updateRoute(fields) {
    const r = await api(`/api/routes/${one.data.route.id}/update`, {method: 'POST', json: fields});
    if (r._ok) loadRoute(one.data.route.id);
    return r;
}
async function loadRoute(id) {
    if (!one.map) wireRoutePage();
    setTimeout(() => one.map.invalidateSize(), 30);
    const d = await api(`/api/routes/${id}`); if (!d._ok) { go('routes'); return; }
    one.data = d; const r = d.route;
    $('rd-name').textContent = (r.starred ? '★ ' : '') + r.name;
    $('rd-summary').textContent = summary(r, d.stats);
    $('rd-star').textContent = r.starred ? '★ Starred' : '☆ Star';
    sportOptions($('rd-sport'), r.sport);
    $('rd-target').value = (d.stats.distance_m / unitM()).toFixed(1);
    document.querySelectorAll('.rt-unit').forEach((el) => { el.textContent = unitName(); });
    $('rd-gpx').href = `/api/routes/${r.id}/gpx`;
    $('rd-notes').value = r.notes || '';
    $('rd-crumbs').innerHTML = [d.parent ? `variant of <a data-route="${d.parent.id}">${esc(d.parent.name)}</a>` : '',
        d.source_activity ? `from your <a data-act="${d.source_activity.session_id}">activity of ${esc(fmt.day(d.source_activity.start_time))}</a>` : ''].filter(Boolean).join(' · ');
    $('rd-variants').innerHTML = d.variants.length ? d.variants.map((v) => `<div><a data-route="${v.id}">${esc(v.name)}</a> <span class="muted">${km(v.distance_m)}</span></div>`).join('')
        : '<span class="muted">None yet. <b>Make it … km</b> or <b>Edit in planner</b>, then <b>Save as new</b>.</span>';
    document.querySelectorAll('#view-route [data-route]').forEach((a) => a.addEventListener('click', () => go('route', a.dataset.route)));
    document.querySelectorAll('#view-route [data-act]').forEach((a) => a.addEventListener('click', () => go('detail', a.dataset.act)));

    if (one.layer) one.layer.remove();
    const coords = flatten(d.plan), group = L.featureGroup();
    L.polyline(latlngs(coords), {color: routeColor(r.sport), weight: 4}).addTo(group);
    if (coords.length) {
        L.circleMarker([coords[0][1], coords[0][0]], {radius: 6, color: '#fff', weight: 2, fillColor: '#1a8a4a', fillOpacity: 1}).bindTooltip('Start').addTo(group);
        const e = coords[coords.length - 1];
        if (hav(coords[0], e) > 30) L.circleMarker([e[1], e[0]], {radius: 6, color: '#fff', weight: 2, fillColor: '#c0392b', fillOpacity: 1}).bindTooltip('Finish').addTo(group);
    }
    one.layer = group.addTo(one.map);
    setTimeout(() => { one.map.invalidateSize(); if (coords.length) one.map.fitBounds(group.getBounds(), {padding: [16, 16]}); }, 60);
    elevChart('rd-elev', d.elevation);
    drawTimes(d);
}
function drawTimes(d) {
    const h = d.history.slice().reverse(), foot = isFoot(d.route.sport);
    $('rd-times-sub').textContent = h.length ? `${h.length} time${h.length > 1 ? 's' : ''}` : '';
    const paceOf = (x) => (x.distance_m && x.timer_s ? x.timer_s / (x.distance_m / unitM()) : null);
    const ys = h.map((x) => (foot ? paceOf(x) : x.avg_speed ? x.avg_speed * (statute() ? 2.23694 : 3.6) : null));
    if (!h.length) {
        if (state.charts['rd-times']) { state.charts['rd-times'].destroy(); delete state.charts['rd-times']; }
        $('rd-best').textContent = 'No activity has followed this route yet. Activities count once they are imported, in either direction.';
    } else {
        chart('rd-times', {data: {labels: h.map((x) => fmt.day(x.start_time)), datasets: [
            {type: 'line', label: foot ? `pace (/${unitName()})` : `speed (${statute() ? 'mph' : 'km/h'})`, data: ys, borderColor: routeColor(d.route.sport),
             backgroundColor: routeColor(d.route.sport), pointRadius: 4, showLine: h.length > 1, borderWidth: 1.3, spanGaps: true},
            {type: 'line', label: 'avg HR', data: h.map((x) => x.avg_hr), borderColor: '#c0392b', backgroundColor: '#c0392b', pointRadius: 2, borderWidth: 1, borderDash: [4, 3], yAxisID: 'y1', spanGaps: true}]},
            options: {scales: {y: {reverse: foot, ticks: {callback: (v) => (foot ? fmt.dur(v) : v)}}, y1: {position: 'right', grid: {drawOnChartArea: false}}},
                plugins: {tooltip: {callbacks: {label: (c) => (c.datasetIndex === 0 && foot ? `pace ${fmt.dur(c.parsed.y)} /${unitName()}` : `${c.dataset.label} ${Math.round(c.parsed.y * 10) / 10}`),
                    footer: (items) => { const x = h[items[0].dataIndex]; return `${fmt.dur(x.timer_s)} · ${km(x.distance_m)}${x.direction === 'rev' ? ' · reversed' : ''}`; }}}},
                onClick: (ev, els) => { if (els.length) go('detail', h[els[0].index].session_id); }}});
        const best = h.filter((x) => x.timer_s).sort((a, b) => a.timer_s - b.timer_s)[0], last = h[h.length - 1];
        $('rd-best').innerHTML = best ? `Fastest <b>${fmt.dur(best.timer_s)}</b> (${esc(fmt.day(best.start_time))}) · latest ${fmt.dur(last.timer_s)} (${esc(fmt.day(last.start_time))})` : '';
    }
    ctx.table('rd-history', [{label: 'When', get: (x) => fmt.when(x.start_time, x.utc_offset_s)}, {label: 'Name', get: (x) => x.name || ''},
        {label: 'Time', num: 1, get: (x) => fmt.dur(x.timer_s)}, {label: 'Distance', num: 1, get: (x) => km(x.distance_m)},
        {label: foot ? 'Pace' : 'Speed', num: 1, get: (x) => fmt.speed(x.avg_speed, foot ? 'running' : 'cycling')},
        {label: 'Avg HR', num: 1, get: (x) => x.avg_hr}, {label: 'Way round', get: (x) => (x.direction === 'rev' ? 'reversed' : 'as saved')},
        {label: 'On route', num: 1, get: (x) => (x.match_pct != null ? `${Math.round(x.match_pct)} %` : '')}], d.history, (x) => go('detail', x.session_id));
}

// ---------------------------------------------------------------- the planner
const P = {map: null, plan: [], stats: null, elevation: [], sport: 'run', route: null, undo: [], redo: [], cands: [],
           layer: null, candLayers: [], via: null, dirty: false, busy: false, engine: null};

function setBusy(on, text) {
    P.busy = on; document.body.classList.toggle('pl-busy', on);
    $('pl-status').textContent = on ? (text || 'Routing…') : '';
    ['pl-suggest', 'pl-save', 'pl-save-new', 'pl-close', 'pl-reverse'].forEach((id) => { $(id).disabled = on; });
}
function snapshot() { return {plan: P.plan, stats: P.stats, elevation: P.elevation}; }
function restore(s) { P.plan = s.plan; P.stats = s.stats; P.elevation = s.elevation; }
function commit(next) {
    // Every change goes through here, so undo always has the state before it.
    P.undo.push(snapshot()); if (P.undo.length > 100) P.undo.shift(); P.redo = [];
    restore(next); P.dirty = true; clearCands(); render();
}
function plannerLength() { const c = flatten(P.plan); return c.length > 1 ? cumul(c)[c.length - 1] : 0; }
async function edit(body) {
    if (P.busy) return;
    setBusy(true);
    const r = await api('/api/plan/edit', {method: 'POST', json: {...body, plan: P.plan, sport: P.sport}, quiet: true});
    setBusy(false);
    if (!r._ok) { engineProblem(r); return; }
    commit(r);
}
function engineProblem(r) {
    if (r.error === 'engine_unreachable') { $('pl-engine').style.display = ''; toast('The routing engine is not running.'); }
    else toast(r.message || r.error || `Request failed (${r._status})`);
}
function alongAt(latlng) {
    // Distance along the route of the point nearest to latlng (vertex-accurate is plenty here).
    const coords = flatten(P.plan), cum = cumul(coords); let best = 0, bd = Infinity;
    coords.forEach((c, i) => { const dd = P.map.distance(latlng, [c[1], c[0]]); if (dd < bd) { bd = dd; best = i; } });
    return cum[best] || 0;
}
function render() {
    const coords = flatten(P.plan), len = plannerLength(), target = Number($('pl-target').value) * unitM();
    $('pl-length').textContent = len ? km(len) : (coords.length ? 'start set' : '–');
    $('pl-delta').textContent = len && target ? `(${len >= target ? '+' : '−'}${km(Math.abs(len - target)) || '0'} vs target)` : '';
    $('pl-undo').disabled = !P.undo.length; $('pl-redo').disabled = !P.redo.length;
    $('pl-save').textContent = P.route ? 'Save' : 'Save route';
    $('pl-save-new').style.display = P.route ? '' : 'none';
    $('pl-save').disabled = $('pl-save-new').disabled = coords.length < 2;
    if (P.layer) P.layer.remove();
    const group = L.featureGroup();
    if (coords.length > 1) {
        const ll = latlngs(coords);
        L.polyline(ll, {color: routeColor(P.sport), weight: 5, opacity: 0.9, interactive: false}).addTo(group);
        // The grab line: wide and invisible, so the route is easy to catch.
        const grab = L.polyline(ll, {weight: 22, opacity: 0, bubblingMouseEvents: false}).addTo(group);
        grab.on('mousedown', startLineDrag);
        grab.on('click', (e) => { L.DomEvent.stop(e); placeVia(e.latlng); });
    }
    // Control points: every join between segments, plus the two ends.
    const ctl = [];
    P.plan.forEach((s) => ctl.push(s.coords[0]));
    if (coords.length > 1) ctl.push(coords[coords.length - 1]);
    const loop = coords.length > 1 && hav(coords[0], coords[coords.length - 1]) < 30;
    ctl.forEach((c, k) => {
        const first = k === 0, last = k === ctl.length - 1 && ctl.length > 1;
        if (loop && last) return;                                   // drawn as the start
        const cls = `pl-ctl${first ? ' start' : ''}${last || (loop && first) ? ' end' : ''}`;
        const m = L.marker([c[1], c[0]], {draggable: true, keyboard: false,
            icon: L.divIcon({className: '', html: `<div class="${cls}"></div>`, iconSize: [14, 14], iconAnchor: [7, 7]})});
        m.bindTooltip(first ? 'Start: drag to move' : last ? 'Finish: drag to move' : 'Drag to move · click to delete', {direction: 'top'});
        m.on('dragend', (e) => { const p = e.target.getLatLng();
            // Only a start so far: just move it, there is nothing to re-route.
            if (coords.length < 2) commit({plan: [{kind: 'routed', sport: P.sport, coords: [[p.lng, p.lat, null]]}], stats: null, elevation: []});
            else edit({op: 'move', k, to: [p.lng, p.lat]}); });
        if (!first && !last) m.on('click', () => edit({op: 'delete', k}));
        m.addTo(group);
    });
    P.layer = group.addTo(P.map);
    elevChart('pl-elev', P.elevation);
}
function startLineDrag(e) {
    if (P.busy || e.originalEvent.button) return;
    L.DomEvent.stop(e);
    const d = alongAt(e.latlng), map = P.map, from = e.latlng;
    map.dragging.disable();
    const ghost = L.circleMarker(from, {radius: 7, color: '#fff', weight: 2, fillColor: routeColor(P.sport), fillOpacity: 1, interactive: false}).addTo(map);
    const move = (ev) => ghost.setLatLng(ev.latlng);
    const up = (ev) => {
        map.off('mousemove', move); map.off('mouseup', up); map.dragging.enable(); ghost.remove();
        if (map.distance(from, ev.latlng) > 8) edit({op: 'drag', d, to: [ev.latlng.lng, ev.latlng.lat]});
    };
    map.on('mousemove', move); map.on('mouseup', up);
}
function placeVia(latlng) {
    // A click on the line (touch screens have no drag-the-line): a handle to drag instead.
    if (P.via) P.via.remove();
    const d = alongAt(latlng);
    P.via = L.marker(latlng, {draggable: true, icon: L.divIcon({className: '', html: '<div class="pl-ctl" style="border-color:#d81b60"></div>', iconSize: [14, 14], iconAnchor: [7, 7]})})
        .bindTooltip('Drag me to route through somewhere else', {permanent: true, direction: 'top'}).addTo(P.map);
    P.via.on('dragend', (ev) => { const p = ev.target.getLatLng(); P.via.remove(); P.via = null; edit({op: 'drag', d, to: [p.lng, p.lat]}); });
}
function mapClick(e) {
    if (P.busy) return;
    if (P.via) { P.via.remove(); P.via = null; }
    const to = [e.latlng.lng, e.latlng.lat];
    if (!flatten(P.plan).length) { commit({plan: [{kind: 'routed', sport: P.sport, coords: [[to[0], to[1], null]]}], stats: null, elevation: []});
        toast(`Start set. Click on to add points, or give a length and press Suggest for loops from here.`); return; }
    if (!$('pl-draw').checked) return;
    edit({op: 'append', to});
}
function clearCands() { P.candLayers.forEach((l) => l.remove()); P.candLayers = []; P.cands = []; $('pl-cands').innerHTML = '<span class="muted">Set a target length and press Suggest.</span>'; $('pl-cands-sub').textContent = ''; }
async function suggest() {
    const target = Number($('pl-target').value) * unitM();
    if (!target) { toast(`Give a target length in ${unitName()}.`); return; }
    const coords = flatten(P.plan);
    if (!coords.length) { toast('Click the map to set a start first.'); return; }
    const tol = Number($('pl-tol').value) || 2;
    setBusy(true, coords.length < 2 ? 'Finding loops…' : 'Finding routes of that length…');
    const r = coords.length < 2
        ? await api('/api/plan/loop', {method: 'POST', quiet: true, json: {start: coords[0], target_m: target, sport: P.sport, rotation: (P.rotation = (P.rotation || 0) + 1.05)}})
        : await api('/api/plan/fit', {method: 'POST', quiet: true, json: {plan: P.plan, target_m: target, tolerance_pct: tol, sport: P.sport, route_id: P.route && P.route.id}});
    setBusy(false);
    clearCands();
    if (!r._ok) { engineProblem(r); return; }
    P.cands = r.candidates;
    $('pl-cands-sub').textContent = `for ${km(target)}`;
    if (!P.cands.length) { $('pl-cands').innerHTML = '<span class="muted">Nothing found near that length. Try a smaller change, or edit by hand.</span>'; return; }
    const base = plannerLength();
    P.cands.forEach((c, i) => {
        const line = L.polyline(latlngs(flatten(c.plan)), {color: CAND_COLORS[i % CAND_COLORS.length], weight: 4, opacity: 0.8, dashArray: '8 6',
            bubblingMouseEvents: false}).addTo(P.map);
        line.on('click', () => useCand(i)); line.on('mouseover', () => hotCand(i, true)); line.on('mouseout', () => hotCand(i, false));
        P.candLayers.push(line);
    });
    $('pl-cands').innerHTML = P.cands.map((c, i) => {
        const delta = base ? c.stats.distance_m - base : null, st = c.stats;
        const bits = [delta != null ? `${delta >= 0 ? '+' : '−'}${km(Math.abs(delta))}` : '', c.known_pct != null ? `${c.known_pct} % on ground you know` : '',
            st.ascent_m != null ? `${fmt.elev(st.ascent_m)} ↑` : '', st.unpaved_pct != null ? `${st.unpaved_pct} % unpaved` : ''].filter(Boolean);
        return `<div class="pl-cand" data-i="${i}" style="--c:${CAND_COLORS[i % CAND_COLORS.length]}"><div class="pl-cand-head"><span>${String.fromCharCode(65 + i)} · ${esc(c.label)}</span><span>${km(st.distance_m)}</span></div>
            <div class="muted">${esc(bits.join(' · '))}</div><button class="primary">Use ${String.fromCharCode(65 + i)}</button></div>`;
    }).join('');
    $('pl-cands').querySelectorAll('.pl-cand').forEach((el) => {
        const i = +el.dataset.i;
        el.addEventListener('mouseenter', () => hotCand(i, true)); el.addEventListener('mouseleave', () => hotCand(i, false));
        el.querySelector('button').addEventListener('click', (ev) => { ev.stopPropagation(); useCand(i); });
        el.addEventListener('click', () => { const l = P.candLayers[i]; if (l) P.map.fitBounds(l.getBounds(), {padding: [20, 20]}); });
    });
    // The route itself on top: where a suggestion follows it, the route shows; where it leaves, the dash does.
    if (P.layer) P.layer.bringToFront();
    const all = L.featureGroup(P.candLayers.concat(P.layer ? [P.layer] : []));
    P.map.fitBounds(all.getBounds(), {padding: [20, 20]});
}
function hotCand(i, on) {
    const l = P.candLayers[i]; if (!l) return;
    l.setStyle({weight: on ? 7 : 4, opacity: on ? 1 : 0.8}); if (on) l.bringToFront();
    const el = $('pl-cands').querySelector(`[data-i="${i}"]`); if (el) el.classList.toggle('hot', on);
}
function useCand(i) { const c = P.cands[i]; if (!c) return; commit({plan: c.plan, stats: c.stats, elevation: c.elevation}); toast(`Using ${String.fromCharCode(65 + i)}: ${km(c.stats.distance_m)}. Save it, or keep editing.`); }
function undo() { if (!P.undo.length) return; P.redo.push(snapshot()); restore(P.undo.pop()); P.dirty = true; clearCands(); render(); }
function redo() { if (!P.redo.length) return; P.undo.push(snapshot()); restore(P.redo.pop()); P.dirty = true; clearCands(); render(); }
async function save(asNew) {
    if (flatten(P.plan).length < 2) return;
    const len = plannerLength();
    if (P.route && !asNew) {
        if (!confirm(`Replace the line of “${P.route.name}” with this ${km(len)} version? Save as new keeps the original.`)) return;
        const r = await api(`/api/routes/${P.route.id}/update`, {method: 'POST', json: {plan: P.plan, sport: P.sport}});
        if (r._ok) { P.dirty = false; toast('Saved.'); go('route', P.route.id); }
        return;
    }
    const suggested = P.route ? `${P.route.name} ${(len / unitM()).toFixed(1)} ${unitName()}` : `${SPORTS[P.sport]} ${(len / unitM()).toFixed(1)} ${unitName()}`;
    const name = prompt('Name for the new route', suggested); if (!name) return;
    const r = await api('/api/routes', {method: 'POST', json: {name, sport: P.sport, plan: P.plan, parent_id: P.route ? P.route.id : null,
        source: P.route ? 'planned' : 'drawn'}});
    if (r._ok) { P.dirty = false; toast(P.route ? 'Saved as a variant.' : 'Route saved.'); go('route', r.route.id); }
}
function wirePlanner() {
    P.map = baseMap('pl-map');
    P.map.on('click', mapClick);
    sportOptions($('pl-sport'));
    $('pl-sport').addEventListener('change', (e) => { P.sport = state.lastPlannerSport = e.target.value; render(); });
    $('pl-suggest').addEventListener('click', suggest);
    $('pl-target').addEventListener('keydown', (e) => { if (e.key === 'Enter') suggest(); });
    $('pl-target').addEventListener('input', render);
    $('pl-tol').addEventListener('change', (e) => { const v = Number(e.target.value); if (v > 0) { if (state.settings) state.settings.route_tolerance_pct = v; api('/api/settings', {method: 'POST', json: {route_tolerance_pct: v}, quiet: true}); } });
    $('pl-undo').addEventListener('click', undo); $('pl-redo').addEventListener('click', redo);
    $('pl-reverse').addEventListener('click', () => edit({op: 'reverse'}));
    $('pl-close').addEventListener('click', () => edit({op: 'close'}));
    $('pl-save').addEventListener('click', () => save(false)); $('pl-save-new').addEventListener('click', () => save(true));
    $('pl-back').addEventListener('click', () => { if (P.dirty && !confirm('Leave without saving this route?')) return; P.dirty = false; if (P.route) go('route', P.route.id); else go('routes'); });
    document.addEventListener('keydown', (e) => {
        if (state.view !== 'planner' || e.target.closest('input, select, textarea')) return;
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); if (e.shiftKey) redo(); else undo(); }
        else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') { e.preventDefault(); redo(); }
    });
    window.addEventListener('beforeunload', (e) => { if (state.view === 'planner' && P.dirty) { e.preventDefault(); e.returnValue = ''; } });
}
async function loadPlanner(arg) {
    if (!P.map) wirePlanner();
    setTimeout(() => P.map.invalidateSize(), 30);
    const [idPart, kmPart] = String(arg || 'new').split(',');
    P.undo = []; P.redo = []; P.dirty = false; P.rotation = 0; clearCands();
    if (P.via) { P.via.remove(); P.via = null; }
    document.querySelectorAll('.rt-unit').forEach((el) => { el.textContent = unitName(); });
    $('pl-tol').value = (state.settings && state.settings.route_tolerance_pct) || 2;
    $('pl-engine').style.display = 'none';
    api('/api/plan/status', {quiet: true}).then((s) => { if (s._ok && !s.engine) $('pl-engine').style.display = ''; });
    if (idPart && idPart !== 'new') {
        const d = await api(`/api/routes/${idPart}`); if (!d._ok) { go('routes'); return; }
        P.route = d.route; P.plan = d.plan; P.stats = d.stats; P.elevation = d.elevation; P.sport = d.route.sport;
        $('pl-title').textContent = `Editing ${d.route.name}`;
        $('pl-target').value = kmPart || (d.stats.distance_m / unitM()).toFixed(1);
        $('pl-draw').checked = false;
    } else {
        P.route = null; P.plan = []; P.stats = null; P.elevation = [];
        P.sport = (state.lastPlannerSport) || 'run';
        $('pl-title').textContent = 'New route: click the map to set the start';
        $('pl-target').value = '';
        $('pl-draw').checked = true;
    }
    sportOptions($('pl-sport'), P.sport);
    render();
    const coords = flatten(P.plan);
    if (coords.length > 1) setTimeout(() => P.map.fitBounds(P.layer.getBounds(), {padding: [24, 24]}), 60);
    else if (lib.map && lib.map.getBounds) setTimeout(() => P.map.setView(lib.map.getCenter(), Math.max(13, lib.map.getZoom())), 60);
    if (kmPart && coords.length > 1) suggest();
}

// ---------------------------------------------------------------- the activity page
// "Save as route", and which routes this activity followed.
async function activityRoutes(sessionId) {
    const btn = $('det-save-route'); btn.hidden = true;
    const d = await api(`/api/sessions/${sessionId}/routes`, {quiet: true});
    if (!d._ok || String(state.arg) !== String(sessionId)) return;
    btn.hidden = !d.savable;
    btn.onclick = async () => {
        const s = state.session && state.session.session;
        const guess = s ? (s.name || `${km(s.distance_m)} ${s.sport === 'cycling' ? 'ride' : s.sport === 'running' ? 'run' : 'walk'}`) : '';
        const name = prompt('Name for this route', guess); if (name == null) return;
        const r = await api(`/api/routes/from-session/${sessionId}`, {method: 'POST', json: {name: name.trim()}});
        if (r._ok) { toast('Saved to your routes.'); go('route', r.route.id); }
    };
    if (d.routes.length) $('det-banner').insertAdjacentHTML('beforeend', `<div class="banner good">On ${d.routes.length > 1 ? 'routes' : 'route'}: ${d.routes.map((r) => `<a data-route="${r.id}">${esc(r.name)}</a>${r.direction === 'rev' ? ' (reversed)' : ''}`).join(', ')}</div>`);
    $('det-banner').querySelectorAll('[data-route]').forEach((a) => a.addEventListener('click', () => go('route', a.dataset.route)));
}

return {views: {routes: loadRoutes, route: loadRoute, planner: loadPlanner}, activityRoutes,
        tabFor: {route: 'routes', planner: 'routes'}};
};
