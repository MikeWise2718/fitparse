/* Fitmon single-page UI. Vanilla JS, no build step. Every action is a JSON call to /api/... */
(() => {
'use strict';
const $ = (id) => document.getElementById(id);
const state = {csrf: window.FITMON.csrf, user: window.FITMON.user, settings: null, view: null,
               session: null, charts: {}, map: null, act: {offset: 0, sort: 'start_time', dir: 'desc'},
               exp: {file: null, message: null, page: 1}, poll: null};
const SPORT_COLORS = {running: '#e4572e', cycling: '#0b6bcb', swimming: '#17a2b8', training: '#7d4cdb',
                      walking: '#d98e04', hiking: '#6a994e', fitness_equipment: '#7d4cdb', transition: '#8a93a6'};
const color = (sport, i = 0) => SPORT_COLORS[sport] || ['#8a93a6', '#c05299', '#2a9d8f', '#e9c46a'][i % 4];

// ---------------------------------------------------------------- plumbing
async function api(url, opts = {}) {
    const init = {method: opts.method || 'GET', headers: {}};
    if (init.method !== 'GET') init.headers['X-CSRF-Token'] = state.csrf;
    if (opts.form) init.body = opts.form;
    else if (init.method !== 'GET') { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.json || {}); }
    const resp = await fetch(url, init);
    if (resp.status === 401) { location.href = '/login'; throw new Error('login required'); }
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok && !opts.quiet) { toast(body.message || body.error || `Request failed (${resp.status})`); }
    body._ok = resp.ok; body._status = resp.status;
    return body;
}
function toast(text) { const t = $('toast'); t.textContent = text; t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 4000); }
function esc(v) { return String(v ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])); }
function chart(id, config) {
    if (state.charts[id]) state.charts[id].destroy();
    const el = $(id); if (!el) return null;
    config.options = Object.assign({responsive: true, maintainAspectRatio: false, animation: false,
        interaction: {mode: 'index', intersect: false}}, config.options || {});
    state.charts[id] = new Chart(el, config);
    return state.charts[id];
}
function table(id, columns, rows, onClick) {
    const el = $(id);
    const head = '<thead><tr>' + columns.map((c) => `<th class="${c.num ? 'num ' : ''}${c.sort ? 'sortable' : ''}" data-sort="${c.sort || ''}">${esc(c.label)}</th>`).join('') + '</tr></thead>';
    const body = rows.length ? rows.map((r, i) => `<tr class="${onClick ? 'click' : ''}" data-i="${i}">` +
        columns.map((c) => `<td class="${c.num ? 'num' : ''}">${c.html ? c.html(r) : esc(c.get(r) ?? '')}</td>`).join('') + '</tr>').join('')
        : `<tr><td colspan="${columns.length}" class="muted">Nothing here yet.</td></tr>`;
    el.innerHTML = head + '<tbody>' + body + '</tbody>';
    if (onClick) el.querySelectorAll('tbody tr.click').forEach((tr) => tr.addEventListener('click', (ev) => {
        if (ev.target.closest('button, a, input')) return; onClick(rows[+tr.dataset.i]); }));
    return el;
}

// ---------------------------------------------------------------- formatting
const statute = () => state.settings && state.settings.units === 'statute';
const fmt = {
    dur(s) { if (s == null) return ''; s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
             return h ? `${h}:${String(m).padStart(2, '0')}:${String(x).padStart(2, '0')}` : `${m}:${String(x).padStart(2, '0')}`; },
    dist(m, sport) { if (m == null || !m) return ''; if (sport === 'swimming') return statute() ? `${Math.round(m * 1.09361)} yd` : `${Math.round(m)} m`;
             return statute() ? `${(m / 1609.344).toFixed(2)} mi` : `${(m / 1000).toFixed(2)} km`; },
    speed(ms, sport) { if (!ms) return '';
             if (sport === 'swimming') return `${fmt.dur(100 / ms)} /100m`;
             if (['running', 'walking', 'hiking'].includes(sport)) return `${fmt.dur((statute() ? 1609.344 : 1000) / ms)} /${statute() ? 'mi' : 'km'}`;
             return statute() ? `${(ms * 2.23694).toFixed(1)} mph` : `${(ms * 3.6).toFixed(1)} km/h`; },
    elev(m) { return m == null ? '' : statute() ? `${Math.round(m * 3.28084)} ft` : `${Math.round(m)} m`; },
    weight(kg) { return kg == null ? '' : statute() ? `${(kg * 2.20462).toFixed(1)} lb` : `${kg.toFixed(1)} kg`; },
    num(v, d = 0) { return v == null ? '' : Number(v).toFixed(d); },
    // Stored times are UTC; an activity is shown in the local time it was recorded in.
    when(iso, offsetS) { if (!iso) return ''; const d = new Date(iso);
             if (offsetS != null) { const l = new Date(d.getTime() + offsetS * 1000); return l.toISOString().slice(0, 16).replace('T', ' '); }
             return d.toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}); },
    day(iso) { return iso ? String(iso).slice(0, 10) : ''; },
    sport(s, sub) { const name = (sub && sub !== 'generic' ? sub : s || '').replace(/_/g, ' '); return name; },
    bytes(n) { return n > 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} kB`; },
    window(kind, w) { if (kind === 'power') return w < 60 ? `${w} s` : `${w / 60} min`;
             return {400: '400 m', 1000: '1 km', 1609: '1 mile', 5000: '5 km', 10000: '10 km', 21097: 'half marathon', 42195: 'marathon'}[w] || `${w} m`; },
};
const tag = (s, sub) => `<span class="tag ${esc(s)}">${esc(fmt.sport(s, sub))}</span>`;
const tile = (label, value, sub = '', cls = '', help = '') => `<div class="tile"${help ? ` title="${esc(help)}"` : ''}><div class="label">${esc(label)}${help ? ' <span class="muted" style="cursor:help">ⓘ</span>' : ''}</div><div class="value ${cls}">${value || '–'}</div><div class="sub">${sub}</div></div>`;

function decouplingHelp(s) {
    // The textbook reading assumes HR drifts UP at a held pace. The opposite pattern - pace
    // collapsing while HR falls - degrades the same ratio for a different reason, so say which
    // one this activity actually was rather than quoting the threshold at every case.
    const lines = ['Aerobic decoupling: how well output per heartbeat held up.',
        'The activity is split in half; each half gets ' + (s.has_power ? 'power' : 'speed') + ' ÷ heart rate.',
        `Here that ratio ${s.decoupling >= 0 ? 'fell' : 'rose'} by ${Math.abs(s.decoupling).toFixed(1)} % from the first half to the second.`,
        'Under 5 % is the usual mark of an aerobically durable effort (Friel).'];
    if (s.decoupling > 5) lines.push('', 'Above that, something gave way - fuel, heat, or simply going beyond the duration your aerobic base supports.',
        'Which one shows in the shape: heart rate drifting UP at a held pace points to heat and dehydration, while pace collapsing as heart rate FALLS points to running out of fuel, because a depleted muscle cannot demand the output.');
    lines.push('', 'Caveat: a rule of thumb from steady training efforts. A triathlon run leg fades somewhat whatever you do, and any time left recording after you stopped is counted in.');
    return lines.join('\n');
}

// ---------------------------------------------------------------- filters and routing
function range() { const p = new URLSearchParams(); if ($('flt-from').value) p.set('from', $('flt-from').value);
    if ($('flt-to').value) p.set('to', $('flt-to').value); return p; }
function query(extra = {}) { const p = range(); if ($('flt-sport').value) p.set('sport', $('flt-sport').value);
    Object.entries(extra).forEach(([k, v]) => { if (v !== '' && v != null) p.set(k, v); }); return p.toString(); }
// The default window ends at the NEWEST ACTIVITY, not at today: an archive that stops two years
// ago must not open as an empty app. The note says so, and any manual change clears it.
async function initialRange() {
    const days = state.settings.default_range_days || 365;
    const d = await api('/api/dashboard', {quiet: true});
    const last = d.last_activity_at ? new Date(d.last_activity_at) : null;
    if (!last || (Date.now() - last) / 86400000 <= days) { setPreset(days); return; }
    $('flt-from').value = new Date(last.getTime() - days * 86400000).toISOString().slice(0, 10);
    $('flt-to').value = last.toISOString().slice(0, 10);
    state.rangeNote = `showing the ${days} days up to your newest activity (${$('flt-to').value})`;
}
function setPreset(days) { if (days === 'all') { $('flt-from').value = ''; $('flt-to').value = ''; return; }
    const d = new Date(Date.now() - days * 86400000); $('flt-from').value = d.toISOString().slice(0, 10); $('flt-to').value = ''; }

function showNote() { $('flt-note').textContent = [state.rangeNote, state.staleNote].filter(Boolean).join(' · '); }
const VIEWS = {dashboard: loadDashboard, activities: () => loadActivities(true), detail: loadDetail, trends: loadTrends,
               body: loadBody, gear: loadGear, explorer: loadExplorer, import: loadImport, settings: loadSettings, admin: loadAdmin};
function go(view, arg) { location.hash = arg != null ? `#${view}/${arg}` : `#${view}`; }
function route() {
    const [view, arg] = (location.hash.slice(1) || 'dashboard').split('/');
    const name = VIEWS[view] ? view : 'dashboard';
    state.view = name; state.arg = arg;
    document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
    document.querySelectorAll('#tabs a').forEach((a) => a.classList.toggle('active', a.dataset.view === (name === 'detail' ? 'activities' : name)));
    $('filters').style.display = ['dashboard', 'activities', 'trends'].includes(name) ? '' : 'none';
    clearInterval(state.poll); state.poll = null;
    try { localStorage.setItem('fitmon.view', name); } catch (e) { /* private mode */ }
    VIEWS[name](arg);
}
const reload = () => VIEWS[state.view](state.arg);

// ---------------------------------------------------------------- dashboard
function volumeChart(id, data, metric) {
    chart(id, {type: 'bar', data: {labels: data.buckets, datasets: data.sports.map((sp, i) => ({label: fmt.sport(sp), backgroundColor: color(sp, i),
        data: data.data[sp].map((c) => (c ? c[metric] : 0))}))},
        options: {scales: {x: {stacked: true}, y: {stacked: true, title: {display: true, text: metric}}}}});
}
async function loadDashboard() {
    const [d, fit, vol, cal] = await Promise.all([api('/api/dashboard'), api('/api/fitness?' + range()),
        api('/api/trends/volume?bucket=week&' + range()), api('/api/trends/calendar')]);
    $('dash-empty').style.display = d.total_sessions ? 'none' : '';
    const delta = (v) => v == null ? '' : `<span class="${v >= 0 ? 'up' : 'down'}">${v >= 0 ? '+' : ''}${v} in 90 d</span>`;
    const vs = (now, avg, unit) => avg ? `4-wk avg ${avg} ${unit}` : '';
    const form = d.form.tsb;
    const tiles = [
        tile('VO2 max · run', fmt.num(d.vo2max.run?.value, 1), delta(d.vo2max.run?.delta_90d)),
        tile('VO2 max · bike', fmt.num(d.vo2max.bike?.value, 1), delta(d.vo2max.bike?.delta_90d)),
        tile('Fitness (CTL)', fmt.num(d.form.ctl, 0), `fatigue ${fmt.num(d.form.atl, 0)}`),
        tile('Form (TSB)', fmt.num(form, 0), form == null ? '' : form < -30 ? 'very fatigued' : form < -10 ? 'building' : form < 5 ? 'neutral' : form < 25 ? 'fresh' : 'detraining',
             form < -30 ? 'down' : form > 5 && form < 25 ? 'up' : ''),
        tile('This week', `${d.this_week.hours} h`, vs(d.this_week.hours, d.avg_4_weeks.hours, 'h')),
        tile('Distance', fmt.dist(d.this_week.km * 1000), vs(1, fmt.num(d.avg_4_weeks.km, 0), statute() ? 'km eq.' : 'km')),
        tile('Load', fmt.num(d.this_week.load, 0), vs(1, fmt.num(d.avg_4_weeks.load, 0), '')),
        tile('FTP', d.body.ftp ? `${d.body.ftp} W` : '', d.body.ftp && d.body.weight_kg ? `${(d.body.ftp / d.body.weight_kg).toFixed(2)} W/kg` : ''),
        tile('Resting HR', d.health?.resting_hr || d.body.resting_hr || '', 'bpm'),
        tile('Weight', fmt.weight(d.body.weight_kg)),
    ];
    if (d.health) tiles.push(tile('HRV', fmt.num(d.health.hrv_avg, 0), 'ms last night'), tile('Sleep score', d.health.sleep_score || ''),
                             tile('Readiness', d.health.readiness || '', d.health.day));
    $('dash-tiles').innerHTML = tiles.join('');
    const stale = d.last_activity_at && (Date.now() - new Date(d.last_activity_at)) / 86400000;
    state.staleNote = stale > 30 ? `newest activity is ${Math.round(stale)} days old - sync Garmin or upload under Import` : '';
    showNote();

    chart('dash-fitness-chart', {data: {labels: fit.days, datasets: [
        {type: 'bar', label: 'daily load', data: fit.load, backgroundColor: 'rgba(138,147,166,.35)', yAxisID: 'y1'},
        {type: 'line', label: 'fitness (CTL)', data: fit.ctl, borderColor: '#0b6bcb', pointRadius: 0, borderWidth: 2},
        {type: 'line', label: 'fatigue (ATL)', data: fit.atl, borderColor: '#e4572e', pointRadius: 0, borderWidth: 1.5},
        {type: 'line', label: 'form (TSB)', data: fit.tsb, borderColor: '#1a8a4a', pointRadius: 0, borderWidth: 1.5, fill: {target: 'origin', above: 'rgba(26,138,74,.10)', below: 'rgba(192,57,43,.10)'}}]},
        options: {scales: {x: {ticks: {maxTicksLimit: 12}}, y1: {position: 'right', grid: {drawOnChartArea: false}}}}});
    state.dashVolume = vol; volumeChart('dash-volume-chart', vol, $('dash-volume-metric').value);

    const year = new Date().getFullYear(), byDay = Object.fromEntries(cal.days.map((c) => [c.day, c]));
    const first = new Date(Date.UTC(year, 0, 1)); let cells = '';
    const pad = (first.getUTCDay() + 6) % 7; for (let i = 0; i < pad; i++) cells += '<i style="visibility:hidden"></i>';
    for (let t = first; t.getUTCFullYear() === year; t = new Date(t.getTime() + 86400000)) {
        const key = t.toISOString().slice(0, 10), c = byDay[key];
        const a = c ? Math.min(1, 0.25 + c.hours / 3) : 0;
        cells += `<i title="${key}${c ? ` · ${c.hours} h · load ${c.load}` : ''}" style="${c ? `background:rgba(11,107,203,${a})` : ''}"></i>`;
    }
    $('dash-calendar').innerHTML = cells; $('dash-cal-year').textContent = `${year} · ${cal.days.length} days`;
    const recent = await api('/api/activities?limit=8');
    table('dash-recent', [{label: 'When', get: (r) => fmt.when(r.start_time, r.utc_offset_s)}, {label: 'Sport', html: (r) => tag(r.sport, r.sub_sport)},
        {label: 'Dist', num: 1, get: (r) => fmt.dist(r.distance_m, r.sport)}, {label: 'Time', num: 1, get: (r) => fmt.dur(r.timer_s)},
        {label: 'Load', num: 1, get: (r) => fmt.num(r.load)}], recent.activities, (r) => go('detail', r.id));
}

// ---------------------------------------------------------------- activities
const ACT_COLS = [
    {label: 'When', sort: 'start_time', get: (r) => fmt.when(r.start_time, r.utc_offset_s)},
    {label: 'Sport', sort: 'sport', html: (r) => tag(r.sport, r.sub_sport) + (r.legs > 1 ? ` <span class="muted">leg ${r.idx + 1}/${r.legs}</span>` : '') + (r.parse_status !== 'ok' ? ` <span class="tag ${esc(r.parse_status)}">${esc(r.parse_status)}</span>` : '') + (r.excluded ? ' <span class="tag" title="Not counted in any analysis">not training</span>' : '')},
    {label: 'Name', get: (r) => r.name || ''},
    {label: 'Distance', sort: 'distance_m', num: 1, get: (r) => fmt.dist(r.distance_m, r.sport)},
    {label: 'Time', sort: 'timer_s', num: 1, get: (r) => fmt.dur(r.timer_s)},
    {label: 'Pace / speed', sort: 'avg_speed', num: 1, get: (r) => fmt.speed(r.avg_speed, r.sport)},
    // Separate columns, not "avg / max" in one: each is independently sortable and filterable.
    {label: 'Avg HR', sort: 'avg_hr', num: 1, get: (r) => r.avg_hr},
    {label: 'Max HR', sort: 'max_hr', num: 1, get: (r) => r.max_hr},
    {label: 'Power', sort: 'avg_power', num: 1, get: (r) => fmt.num(r.avg_power)},
    {label: 'NP', num: 1, get: (r) => fmt.num(r.norm_power)}, {label: 'Climb', sort: 'ascent_m', num: 1, get: (r) => fmt.elev(r.ascent_m)},
    {label: 'TE', sort: 'te_aerobic', num: 1, get: (r) => r.te_aerobic != null ? `${fmt.num(r.te_aerobic, 1)} / ${fmt.num(r.te_anaerobic, 1)}` : ''},
    {label: 'VO2', sort: 'vo2max', num: 1, get: (r) => fmt.num(r.vo2max, 1)},
    {label: 'Load', sort: 'load', num: 1, html: (r) => r.load != null ? `${fmt.num(r.load)} <span class="muted">${r.load_model === 'tss' ? 'W' : '♥'}</span>` : ''},
];
// Range filters offered above the activities table. `to`/`from` convert between the unit a
// person types and the metres/seconds/m-per-s the API stores.
const ACT_FILTERS = [
    {key: 'distance_m', label: 'Distance', unit: () => statute() ? 'mi' : 'km',
     to: (v) => v * (statute() ? 1609.344 : 1000), from: (v) => v / (statute() ? 1609.344 : 1000)},
    {key: 'timer_s', label: 'Time', unit: () => 'min', to: (v) => v * 60, from: (v) => v / 60},
    {key: 'avg_hr', label: 'Avg HR', unit: () => 'bpm'},
    {key: 'max_hr', label: 'Max HR', unit: () => 'bpm'},
    {key: 'avg_power', label: 'Power', unit: () => 'W'},
    {key: 'ascent_m', label: 'Climb', unit: () => statute() ? 'ft' : 'm',
     to: (v) => statute() ? v / 3.28084 : v, from: (v) => statute() ? v * 3.28084 : v},
    {key: 'load', label: 'Load', unit: () => ''},
    {key: 'vo2max', label: 'VO2 max', unit: () => ''},
    {key: 'te_aerobic', label: 'Training effect', unit: () => ''},
];

function actFilterParams() {
    const out = {};
    ACT_FILTERS.forEach((f) => ['min', 'max'].forEach((side) => {
        const el = $(`act-f-${side}-${f.key}`);
        if (el && el.value !== '') {
            const v = Number(el.value);
            if (!isNaN(v)) out[`${side}_${f.key}`] = f.to ? f.to(v) : v;
        }
    }));
    return out;
}

function activeFilterCount() { return Object.keys(actFilterParams()).length; }

function drawActFilters() {
    const box = $('act-filters');
    if (box.dataset.built) return;
    box.dataset.built = '1';
    box.innerHTML = ACT_FILTERS.map((f) => `<span class="fgroup">${esc(f.label)}
        <input id="act-f-min-${f.key}" type="number" step="any" placeholder="min" title="minimum ${esc(f.label)}">
        <input id="act-f-max-${f.key}" type="number" step="any" placeholder="max" title="maximum ${esc(f.label)}">
        <span class="muted">${esc(f.unit())}</span></span>`).join('')
        + '<button id="act-f-clear">Clear filters</button>';
    let timer;
    box.querySelectorAll('input').forEach((el) => el.addEventListener('input', () => {
        clearTimeout(timer); timer = setTimeout(() => loadActivities(true), 350);
    }));
    $('act-f-clear').addEventListener('click', () => {
        box.querySelectorAll('input').forEach((el) => { el.value = ''; });
        loadActivities(true);
    });
}


async function loadActivities(reset) {
    if (reset) { state.act.offset = 0; state.act.rows = []; }
    drawActFilters();
    const q = query({q: $('act-search').value, sort: state.act.sort, dir: state.act.dir, offset: state.act.offset, limit: 100,
                     hide_transitions: $('act-transitions').checked ? '0' : '1',
                     show_excluded: $('act-excluded').checked ? '1' : '0', ...actFilterParams()});
    const data = await api('/api/activities?' + q);
    state.act.rows = state.act.rows.concat(data.activities);
    const nf = activeFilterCount();
    $('act-count').textContent = `${state.act.rows.length} of ${data.total}` + (nf ? ` · ${nf} filter${nf > 1 ? 's' : ''} active` : '');
    state.act.filtered = !data.total && (nf || $('flt-from').value || $('flt-to').value || $('flt-sport').value || $('act-search').value);
    $('act-more').style.display = state.act.rows.length < data.total ? '' : 'none';
    $('act-export').href = '/api/export/activities.csv?' + query(actFilterParams());
    const el = table('act-table', ACT_COLS, state.act.rows, (r) => go('detail', r.id));
    if (state.act.filtered) { el.querySelector('tbody td').innerHTML = 'No activities match the current filters. <a id="act-show-all">Clear them all</a>';
        $('act-show-all').addEventListener('click', () => { setPreset('all'); $('flt-sport').value = ''; $('act-search').value = '';
            $('act-filters').querySelectorAll('input').forEach((el) => { el.value = ''; });
            state.rangeNote = ''; showNote(); loadActivities(true); }); }
    el.querySelectorAll('th.sortable').forEach((th) => th.addEventListener('click', () => {
        const s = th.dataset.sort; state.act.dir = state.act.sort === s && state.act.dir === 'desc' ? 'asc' : 'desc'; state.act.sort = s; loadActivities(true); }));
}

// ---------------------------------------------------------------- activity detail
const SERIES = [['hr', 'Heart rate (bpm)', '#c0392b'], ['speed', 'Speed', '#0b6bcb'], ['power', 'Power (W)', '#7d4cdb'],
                ['cad', 'Cadence', '#d98e04'], ['alt', 'Altitude', '#6a994e'], ['temp', 'Temperature (°C)', '#17a2b8']];
const DYNAMICS = {running: [['vo', 'Vertical oscillation (mm)'], ['vr', 'Vertical ratio (%)'], ['gct', 'Ground contact (ms)'], ['gct_bal', 'GCT balance (% left)'], ['step_len', 'Step length (mm)']],
                  cycling: [['lrb', 'Left share of power (%)'], ['l_te', 'Torque effectiveness L (%)'], ['r_te', 'Torque effectiveness R (%)'], ['l_ps', 'Pedal smoothness L (%)'], ['r_ps', 'Pedal smoothness R (%)']]};
async function loadDetail(id) {
    const d = await api(`/api/sessions/${id}`); if (!d._ok) { go('activities'); return; }
    const s = d.session; state.session = d;
    $('det-title').innerHTML = `${tag(s.sport, s.sub_sport)} ${esc(s.name || '')}`;
    $('det-when').textContent = `${fmt.when(s.start_time, d.file.utc_offset_s)} · ${d.file.product || d.file.manufacturer || ''} · ${d.file.name || ''}`;
    $('det-legs').innerHTML = d.siblings.length > 1 ? d.siblings.map((x) => `<a data-leg="${x.id}" style="${x.id === s.id ? 'font-weight:700' : ''}">${esc(fmt.sport(x.sport, x.sub_sport))} ${fmt.dur(x.timer_s)}</a>`).join(' · ') : '';
    $('det-legs').querySelectorAll('a').forEach((a) => a.addEventListener('click', () => go('detail', a.dataset.leg)));
    $('det-banner').innerHTML = (d.file.parse_status !== 'ok' ? `<div class="banner">This file could only be read partly: <code>${esc(d.file.parse_error)}</code>. Totals and samples up to that point are shown.</div>` : '')
        + (s.excluded ? '<div class="banner">Marked <b>not training</b>: kept here in full, but left out of load, volume, trends and records.</div>' : '');
    $('det-exclude').textContent = s.excluded ? 'Count as training' : 'Not training';
    $('det-tiles').innerHTML = [
        tile('Distance', fmt.dist(s.distance_m, s.sport)), tile('Time', fmt.dur(s.timer_s), s.elapsed_s ? `elapsed ${fmt.dur(s.elapsed_s)}` : ''),
        tile('Pace / speed', fmt.speed(s.avg_speed, s.sport), s.max_speed ? `max ${fmt.speed(s.max_speed, s.sport)}` : ''),
        tile('Heart rate', s.avg_hr, s.max_hr ? `max ${s.max_hr}` : ''),
        s.has_power ? tile('Power', `${fmt.num(s.avg_power)} W`, `NP ${fmt.num(s.norm_power)} · max ${fmt.num(s.max_power)}`) : '',
        s.tss != null ? tile('TSS / IF', fmt.num(s.tss), `IF ${fmt.num(s.intensity_factor, 2)}`) : '',
        tile('Load', fmt.num(s.load), s.load_model === 'tss' ? 'from power' : s.load_model ? 'from heart rate' : ''),
        tile('Training effect', s.te_aerobic != null ? fmt.num(s.te_aerobic, 1) : '', s.te_anaerobic != null ? `anaerobic ${fmt.num(s.te_anaerobic, 1)}` : ''),
        s.vo2max ? tile('VO2 max', fmt.num(s.vo2max, 1)) : '', s.ascent_m ? tile('Climb', fmt.elev(s.ascent_m), `down ${fmt.elev(s.descent_m)}`) : '',
        tile('Cadence', fmt.num(s.avg_cadence), s.max_cadence ? `max ${fmt.num(s.max_cadence)}` : ''), tile('Calories', s.calories),
        // A large NEGATIVE value is not durability: it means the second half was faster per
        // heartbeat, i.e. a negative split or a warm-up followed by the real effort.
        s.decoupling != null ? tile('Decoupling', `${fmt.num(s.decoupling, 1)} %`,
            s.decoupling < -5 ? `${s.has_power ? 'power' : 'speed'} per heartbeat rose - negative split or a slow start`
                : s.decoupling <= 5 ? 'held up · under 5 % is durable'
                : `${s.has_power ? 'power' : 'speed'} per heartbeat fell in the second half`,
            s.decoupling > 5 ? 'down' : s.decoupling >= -5 ? 'up' : '', decouplingHelp(s)) : '',
        s.avg_temp != null ? tile('Temperature', `${fmt.num(s.avg_temp)} °C`) : '',
    ].join('');

    const zoneNames = {hr: 'Heart rate', power: 'Power'};
    $('det-zones').innerHTML = ['hr', 'power'].map((kind) => { const z = d.zones.filter((x) => x.kind === kind); if (!z.length) return '';
        const total = z.reduce((a, x) => a + x.seconds, 0);
        return `<div class="muted" style="margin:6px 0 2px">${zoneNames[kind]}</div><div class="bars">` + z.map((x) => `<span>Z${x.zone}</span><span><span class="bar" style="display:block;width:${(x.seconds / total * 100).toFixed(1)}%;background:${kind === 'hr' ? '#c0392b' : '#7d4cdb'}"></span></span><span class="num">${fmt.dur(x.seconds)}</span>`).join('') + '</div>';
    }).join('') || '<span class="muted">No zone data (no max HR / FTP known for this date).</span>';
    $('det-best').innerHTML = d.best_efforts.length ? '<div class="row">' + d.best_efforts.map((b) => `<span class="tile" style="padding:6px 10px"><span class="label">${fmt.window(b.kind, b.window)}</span><br><b>${b.kind === 'power' ? fmt.num(b.value) + ' W' : fmt.dur(b.value)}</b></span>`).join('') + '</div>' : '<span class="muted">None for this activity.</span>';

    const lapCols = [{label: '#', get: (l) => l.idx + 1}, {label: 'Time', num: 1, get: (l) => fmt.dur(l.timer_s)}, {label: 'Dist', num: 1, get: (l) => fmt.dist(l.distance_m, s.sport)},
        {label: 'Pace / speed', num: 1, get: (l) => fmt.speed(l.avg_speed, s.sport)}, {label: 'HR', num: 1, get: (l) => l.avg_hr}, {label: 'Max HR', num: 1, get: (l) => l.max_hr},
        {label: 'Power', num: 1, get: (l) => fmt.num(l.avg_power)}, {label: 'NP', num: 1, get: (l) => fmt.num(l.norm_power)}, {label: 'Cad', num: 1, get: (l) => fmt.num(l.avg_cadence)},
        {label: 'Climb', num: 1, get: (l) => fmt.elev(l.ascent_m)}, {label: 'Type', get: (l) => [l.intensity, l.swim_stroke, l.trigger].filter(Boolean).join(' · ')}];
    table('det-laps', lapCols, d.laps);
    table('det-devices', [{label: 'Device', get: (x) => [x.manufacturer, x.product].filter(Boolean).join(' ') || x.device_type}, {label: 'Type', get: (x) => x.device_index === 'creator' ? 'watch / head unit' : x.device_type},
        {label: 'Serial', get: (x) => x.serial}, {label: 'Firmware', get: (x) => x.sw_version}, {label: 'Battery', html: (x) => x.battery_status ? `<span class="tag ${['low', 'critical'].includes(x.battery_status) ? 'bad' : 'good'}">${esc(x.battery_status)}</span> ${x.battery_voltage ? fmt.num(x.battery_voltage, 2) + ' V' : ''}` : ''}],
        d.devices.filter((x) => x.product || x.device_type || x.serial));
    $('det-download').href = `/api/files/${d.file.id}/download`;
    drawTrim(d);

    const dyn = DYNAMICS[s.sport] || [];
    const fields = SERIES.map((x) => x[0]).concat(dyn.map((x) => x[0]), ['dist']);
    const rec = await api(`/api/sessions/${id}/records?downsample=1200&fields=${fields.join(',')}`);
    state.records = rec.series; drawSeries(); drawSportPanel(d, rec.series, dyn);
    drawMap(id, s.has_gps);
}
function drawSeries() {
    const r = state.records, s = state.session.session, byDist = $('det-xaxis').value === 'dist' && r.dist.some((v) => v != null);
    const x = byDist ? r.dist.map((v) => v == null ? null : +(v / (statute() ? 1609.344 : 1000)).toFixed(2)) : r.t.map((t) => fmt.dur(t));
    const wrap = $('det-series'); wrap.innerHTML = '';
    Object.keys(state.charts).filter((k) => k.startsWith('det-series-')).forEach((k) => { state.charts[k].destroy(); delete state.charts[k]; });
    SERIES.forEach(([key, label, col]) => {
        let data = r[key]; if (!data || !data.some((v) => v != null)) return;
        if (key === 'speed') { data = data.map((v) => v ? (statute() ? v * 2.23694 : v * 3.6) : null); label = statute() ? 'Speed (mph)' : 'Speed (km/h)'; }
        if (key === 'alt' && statute()) { data = data.map((v) => v == null ? null : v * 3.28084); label = 'Altitude (ft)'; } else if (key === 'alt') label = 'Altitude (m)';
        if (key === 'cad') label = ['running', 'walking', 'hiking'].includes(s.sport) ? 'Cadence (steps/min)' : 'Cadence (rpm)';
        const id = `det-series-${key}`; wrap.insertAdjacentHTML('beforeend', `<div class="chart short"><canvas id="${id}"></canvas></div>`);
        chart(id, {type: 'line', data: {labels: x, datasets: [{label, data, borderColor: col, backgroundColor: col + '22', fill: key === 'alt', pointRadius: 0, borderWidth: 1.3, spanGaps: true}]},
            options: {plugins: {legend: {display: true, position: 'left', labels: {boxWidth: 8}}}, scales: {x: {ticks: {maxTicksLimit: 10}}}}});
    });
    if (!wrap.children.length) wrap.innerHTML = '<span class="muted">This activity has no sample data.</span>';
}
function drawSportPanel(d, r, dyn) {
    const s = d.session, panel = $('det-sport-panel'); let html = '';
    if (d.lengths.length) {
        const active = d.lengths.filter((l) => l.length_type === 'active'), strokes = {};
        active.forEach((l) => { strokes[l.stroke || 'unknown'] = (strokes[l.stroke || 'unknown'] || 0) + 1; });
        html += `<h2>Swim lengths <small>${active.length} lengths · pool ${fmt.num(s.pool_length_m)} m · avg SWOLF ${fmt.num(s.avg_swolf, 1)} · ${Object.entries(strokes).map(([k, v]) => `${k} ${v}`).join(', ')}</small></h2><div class="chart short"><canvas id="det-swim-chart"></canvas></div>`;
    }
    if (d.sets.length) html += '<h2>Strength sets</h2><table id="det-sets"></table>';
    const have = dyn.filter(([k]) => r[k] && r[k].some((v) => v != null));
    if (have.length) html += `<h2>${s.sport === 'running' ? 'Running dynamics' : 'Pedalling'}</h2><div class="tiles">` + have.map(([k, label]) => { const v = r[k].filter((x) => x != null); return tile(label, fmt.num(v.reduce((a, b) => a + b, 0) / v.length, 1)); }).join('') + '</div><div id="det-dyn"></div>';
    panel.style.display = html ? '' : 'none'; panel.innerHTML = html;
    if (d.lengths.length) { const act = d.lengths.filter((l) => l.length_type === 'active');
        chart('det-swim-chart', {data: {labels: act.map((l, i) => i + 1), datasets: [{type: 'bar', label: 'seconds', data: act.map((l) => l.timer_s), backgroundColor: '#17a2b8aa'},
            {type: 'line', label: 'SWOLF', data: act.map((l) => l.swolf), borderColor: '#e4572e', pointRadius: 0, yAxisID: 'y1'}, {type: 'line', label: 'strokes', data: act.map((l) => l.strokes), borderColor: '#7d4cdb', pointRadius: 0, yAxisID: 'y1'}]},
            options: {scales: {y1: {position: 'right', grid: {drawOnChartArea: false}}}}}); }
    if (d.sets.length) { const active = d.sets.filter((x) => x.set_type === 'active');
        table('det-sets', [{label: '#', get: (x) => active.indexOf(x) + 1}, {label: 'Exercise', get: (x) => fmt.sport(x.category)}, {label: 'Reps', num: 1, get: (x) => x.reps}, {label: 'Weight', num: 1, get: (x) => x.weight_kg ? fmt.weight(x.weight_kg) : 'body'},
            {label: 'Volume', num: 1, get: (x) => x.reps && x.weight_kg ? fmt.num(x.reps * x.weight_kg) : ''}, {label: 'Time', num: 1, get: (x) => fmt.dur(x.duration_s)}], active); }
    have.forEach(([k, label]) => { const id = `det-series-dyn-${k}`; $('det-dyn').insertAdjacentHTML('beforeend', `<div class="chart short"><canvas id="${id}"></canvas></div>`);
        chart(id, {type: 'line', data: {labels: r.t.map((t) => fmt.dur(t)), datasets: [{label, data: r[k], borderColor: '#2a9d8f', pointRadius: 0, borderWidth: 1.2, spanGaps: true}]}, options: {plugins: {legend: {position: 'left', labels: {boxWidth: 8}}}, scales: {x: {ticks: {maxTicksLimit: 10}}}}}); });
}
async function drawTrim(d) {
    const panel = $('det-trim-panel'), s = d.session;
    const info = await api(`/api/sessions/${s.id}/trim/suggest`, {quiet: true});
    state.trim = info;
    const sug = info.suggestion;
    // Only speak up when there is something to say: a live trim, or a credible suggestion.
    if (!sug && info.current == null && !s.trimmed_s) { panel.style.display = 'none'; return; }
    panel.style.display = '';
    let html = '<h2>Recording length</h2>';
    if (info.current != null) {
        html += `<div class="banner good">Trimmed to <b>${fmt.dur(info.current)}</b> — figures above are computed to there.
            The file itself is untouched. <button id="trim-undo">Undo trim</button></div>`;
    } else if (sug) {
        html += `<div class="banner">The watch looks like it kept recording after you finished:
            ${esc(sug.reason)}.<br>Suggested end: <b>${fmt.dur(sug.end_t)}</b> of ${fmt.dur(info.last_t)},
            dropping the last ${fmt.dur(sug.dropped_s)}.
            <button class="primary" id="trim-accept">Trim to ${fmt.dur(sug.end_t)}</button></div>`;
    }
    html += `<div class="row"><label>End the activity at <input id="trim-manual" type="text" size="8"
        value="${fmt.dur(info.current ?? sug?.end_t ?? info.last_t)}" placeholder="h:mm:ss"></label>
        <button id="trim-set">Apply</button>
        <span class="muted">Nothing is deleted — this only changes what the figures are computed over.</span></div>`;
    panel.innerHTML = html;

    const apply = async (end_t) => {
        const r = await api(`/api/sessions/${s.id}/trim`, {method: 'POST', json: {end_t}});
        if (r._ok) { toast(end_t == null ? 'Trim removed.' : 'Trimmed.'); go('detail', r.session_id || s.id); reload(); }
    };
    if ($('trim-accept')) $('trim-accept').addEventListener('click', () => apply(sug.end_t));
    if ($('trim-undo')) $('trim-undo').addEventListener('click', () => apply(null));
    $('trim-set').addEventListener('click', () => {
        const parts = $('trim-manual').value.split(':').map(Number);
        if (parts.some(isNaN)) { toast('Use h:mm:ss or mm:ss.'); return; }
        apply(parts.reduce((acc, p) => acc * 60 + p, 0));
    });
}

async function drawMap(id, hasGps) {
    $('det-map-panel').style.display = hasGps ? '' : 'none'; if (!hasGps) return;
    const track = await api(`/api/sessions/${id}/track`); state.track = track;
    // NOT tile.openstreetmap.org: those are volunteer servers whose usage policy forbids
    // unidentified apps, and they answer with "Access blocked" tiles. CARTO serves the same
    // OSM data from a CDN that permits this, and its muted basemap keeps the track readable.
    if (!state.map) {
        state.map = L.map('map');
        L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
            maxZoom: 20, subdomains: 'abcd',
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
        }).addTo(state.map);
    }
    paintTrack();
}
function paintTrack() {
    const track = state.track; if (!track || !state.map) return;
    if (state.trackLayer) state.trackLayer.remove();
    const pts = track.geometry.coordinates.map(([lon, lat]) => [lat, lon]), by = $('det-map-color').value, vals = by && track.properties[by];
    const group = L.featureGroup();
    if (vals && vals.some((v) => v != null)) { const clean = vals.filter((v) => v != null).sort((a, b) => a - b), lo = clean[Math.floor(clean.length * .05)], hi = clean[Math.floor(clean.length * .95)] || lo + 1;
        for (let i = 1; i < pts.length; i++) { const f = Math.max(0, Math.min(1, ((vals[i] ?? lo) - lo) / (hi - lo || 1))); L.polyline([pts[i - 1], pts[i]], {color: `hsl(${(1 - f) * 220}, 85%, 45%)`, weight: 4}).addTo(group); } }
    else L.polyline(pts, {color: '#0b6bcb', weight: 4}).addTo(group);
    state.trackLayer = group.addTo(state.map); setTimeout(() => { state.map.invalidateSize(); if (pts.length) state.map.fitBounds(group.getBounds(), {padding: [16, 16]}); }, 50);
}

// ---------------------------------------------------------------- trends
const scatterLine = (label, pts, col, extra = {}) => Object.assign({label, data: pts, borderColor: col, backgroundColor: col, pointRadius: 3, showLine: true, borderWidth: 1.3, tension: .2}, extra);
const timeAxis = () => ({type: 'category', ticks: {maxTicksLimit: 12}});
async function loadTrends() {
    const q = query(), rq = range().toString(), sportOnly = $('flt-sport').value ? `sport=${$('flt-sport').value}&` : '';
    const [vo2, vol, pc, best, te, swim, strength, body] = await Promise.all([api('/api/trends/vo2max?' + q), api(`/api/trends/volume?bucket=${$('tr-volume-bucket').value}&` + rq),
        api('/api/trends/power-curve?' + sportOnly + rq), api('/api/trends/best-efforts?' + rq), api('/api/trends/training-effect?' + q), api('/api/trends/swim?' + rq), api('/api/trends/strength?' + rq), api('/api/body')]);
    const sports = [...new Set(vo2.points.map((p) => p.sport))], days = [...new Set(vo2.points.map((p) => fmt.day(p.at)))].sort();
    const vo2Days = [...new Set(days.concat((vo2.garmin || []).map((g) => g.day)))].sort();
    const vo2Sets = sports.map((sp, i) => scatterLine(fmt.sport(sp) + ' (watch)', vo2Days.map((d) => {
        const p = vo2.points.filter((x) => x.sport === sp && fmt.day(x.at) === d).pop(); return p ? p.vo2max : null; }), color(sp, i), {spanGaps: true}));
    // Garmin's own smoothed figure, dashed, for comparison with the per-activity readings.
    [['run', 'running'], ['bike', 'cycling']].forEach(([key, sp]) => {
        const pts = (vo2.garmin || []).filter((g) => g[key] != null);
        if (pts.length) vo2Sets.push(scatterLine(`${fmt.sport(sp)} (Garmin)`, vo2Days.map((d) => {
            const p = pts.filter((g) => g.day === d).pop(); return p ? p[key] : null; }),
            color(sp), {spanGaps: true, borderDash: [4, 3], pointRadius: 0}));
    });
    chart('tr-vo2-chart', {type: 'line', data: {labels: vo2Days, datasets: vo2Sets}, options: {scales: {x: timeAxis()}}});
    state.trVolume = vol; volumeChart('tr-volume-chart', vol, $('tr-volume-metric').value);
    const windows = pc.all_time.map((p) => p.window);
    chart('tr-power-chart', {type: 'line', data: {labels: windows.map((w) => fmt.window('power', w)), datasets: [scatterLine('all time', pc.all_time.map((p) => p.value), '#7d4cdb'),
        scatterLine('selected range', windows.map((w) => (pc.range.find((p) => p.window === w) || {}).value ?? null), '#e4572e', {borderDash: [4, 3]})]}, options: {scales: {y: {title: {display: true, text: 'W'}}}}});
    table('tr-best-table', [{label: 'Distance', get: (b) => fmt.window('pace', b.window)}, {label: 'Best ever', num: 1, get: (b) => fmt.dur(b.value)}, {label: 'Pace', num: 1, get: (b) => fmt.speed(b.window / b.value, 'running')}, {label: 'On', get: (b) => fmt.day(b.at)},
        {label: 'In range', num: 1, get: (b) => { const r = best.range.find((x) => x.window === b.window); return r ? fmt.dur(r.value) : ''; }}], best.all_time, (b) => go('detail', b.session_id));
    const prSel = $('tr-pr-kind'); if (!prSel.options.length) prSel.innerHTML = [['power', 1200], ['power', 300], ['power', 60], ['pace', 5000], ['pace', 10000], ['pace', 1000]].map(([k, w]) => `<option value="${k}:${w}">${k === 'power' ? 'power ' : 'run '}${fmt.window(k, w)}</option>`).join('');
    loadRecordProgression(); loadEfficiency();
    chart('tr-te-chart', {type: 'scatter', data: {datasets: [...new Set(te.points.map((p) => p.sport))].map((sp, i) => ({label: fmt.sport(sp), backgroundColor: color(sp, i), data: te.points.filter((p) => p.sport === sp).map((p) => ({x: p.aerobic, y: p.anaerobic || 0}))}))},
        options: {interaction: {mode: 'nearest'}, scales: {x: {min: 0, max: 5, title: {display: true, text: 'aerobic'}}, y: {min: 0, max: 5, title: {display: true, text: 'anaerobic'}}}}});
    chart('tr-swim-chart', {type: 'line', data: {labels: swim.points.map((p) => fmt.day(p.at)), datasets: [scatterLine('s / 100 m', swim.points.map((p) => p.pace_100m_s), '#17a2b8'), scatterLine('SWOLF', swim.points.map((p) => p.swolf), '#e4572e', {yAxisID: 'y1', spanGaps: true})]},
        options: {scales: {x: timeAxis(), y: {reverse: true, title: {display: true, text: 'faster ↑'}}, y1: {position: 'right', grid: {drawOnChartArea: false}}}}});
    const sdays = [...new Set(Object.values(strength.categories).flat().map((x) => x.day))].sort();
    chart('tr-strength-chart', {type: 'bar', data: {labels: sdays, datasets: Object.entries(strength.categories).map(([cat, rows], i) => ({label: fmt.sport(cat), backgroundColor: `hsl(${i * 47 % 360}, 60%, 50%)`, data: sdays.map((d) => (rows.find((r) => r.day === d) || {}).volume_kg || 0)}))},
        options: {scales: {x: {stacked: true, ticks: {maxTicksLimit: 12}}, y: {stacked: true}}}});
    const daily = body.series.daily; $('tr-recovery-panel').style.display = daily.length ? '' : 'none';
    if (daily.length) { const fit = await api('/api/fitness?' + rq), load = Object.fromEntries(fit.days.map((d, i) => [d, fit.atl[i]])), rows = daily.filter((h) => (!$('flt-from').value || h.day >= $('flt-from').value));
        chart('tr-recovery-chart', {data: {labels: rows.map((h) => h.day), datasets: [{type: 'line', label: 'fatigue (ATL)', data: rows.map((h) => load[h.day] ?? null), borderColor: '#e4572e', pointRadius: 0, yAxisID: 'y1', spanGaps: true},
            {type: 'line', label: 'HRV (ms)', data: rows.map((h) => h.hrv_avg), borderColor: '#1a8a4a', pointRadius: 0, spanGaps: true}, {type: 'line', label: 'resting HR', data: rows.map((h) => h.resting_hr), borderColor: '#c0392b', pointRadius: 0, spanGaps: true},
            {type: 'bar', label: 'sleep score', data: rows.map((h) => h.sleep_score), backgroundColor: 'rgba(11,107,203,.18)'}]}, options: {scales: {x: timeAxis(), y1: {position: 'right', grid: {drawOnChartArea: false}}}}}); }
}
async function loadRecordProgression() { const [kind, w] = $('tr-pr-kind').value.split(':'); const d = await api(`/api/trends/records?kind=${kind}&window=${w}` + (kind === 'pace' ? '&sport=running' : '&sport=cycling'));
    chart('tr-pr-chart', {type: 'line', data: {labels: d.progression.map((p) => fmt.day(p.at)), datasets: [{label: kind === 'power' ? 'W' : 'seconds', data: d.progression.map((p) => p.value), stepped: true, borderColor: '#d98e04', pointRadius: 4}]}, options: {scales: {y: {reverse: kind === 'pace'}}, plugins: {legend: {display: false}}}}); }
async function loadEfficiency() { const d = await api(`/api/trends/efficiency?sport=${$('tr-eff-sport').value}&` + range());
    chart('tr-eff-chart', {type: 'line', data: {labels: d.points.map((p) => fmt.day(p.at)), datasets: [scatterLine('efficiency', d.points.map((p) => p.efficiency), '#0b6bcb'), scatterLine('decoupling %', d.points.map((p) => p.decoupling), '#e4572e', {yAxisID: 'y1', showLine: false})]},
        options: {scales: {x: timeAxis(), y1: {position: 'right', grid: {drawOnChartArea: false}}}}}); }

// ---------------------------------------------------------------- body, gear
async function loadBody() {
    const b = await api('/api/body'), s = b.series, l = b.latest, daily = s.daily;
    const over = state.settings.zones, maxHr = over.max_hr || l.max_hr, ftp = over.ftp || l.ftp;
    $('body-tiles').innerHTML = [tile('Weight', fmt.weight(l.weight_kg)), tile('Resting HR', l.resting_hr, 'bpm'), tile('Max HR', maxHr, over.max_hr ? 'your override' : 'from watch'), tile('Threshold HR', over.threshold_hr || l.threshold_hr),
        tile('FTP', ftp ? `${ftp} W` : '', over.ftp ? 'your override' : 'from watch'), tile('W/kg', ftp && l.weight_kg ? (ftp / l.weight_kg).toFixed(2) : '')].join('');
    const line = (id, sets) => { const days = [...new Set(sets.flatMap((x) => x.pts.map((p) => fmt.day(p.at || p.day))))].sort();
        chart(id, {type: 'line', data: {labels: days, datasets: sets.map((x) => Object.assign({label: x.label, borderColor: x.col, backgroundColor: x.col, pointRadius: 2, stepped: x.stepped, spanGaps: true, borderWidth: 1.4,
            data: days.map((d) => { const p = x.pts.filter((q) => fmt.day(q.at || q.day) === d).pop(); return p ? p.value : null; })}, x.extra || {}))}, options: {scales: {x: timeAxis(), y1: {display: sets.some((x) => x.extra), position: 'right', grid: {drawOnChartArea: false}}}}}); };
    const dailyPts = (k) => daily.filter((h) => h[k] != null).map((h) => ({day: h.day, value: h[k]}));
    const w = (pts) => pts.map((p) => ({...p, value: statute() ? +(p.value * 2.20462).toFixed(1) : p.value}));
    line('body-weight-chart', [{label: 'from activities', pts: w(s.weight_kg), col: '#0b6bcb', stepped: true}, {label: 'weigh-ins', pts: w(dailyPts('weight_kg')), col: '#2a9d8f'}]);
    line('body-rhr-chart', [{label: 'watch profile', pts: s.resting_hr, col: '#c0392b', stepped: true}, {label: 'daily', pts: dailyPts('resting_hr'), col: '#e9967a'}]);
    line('body-ftp-chart', [{label: 'FTP (W)', pts: s.ftp, col: '#7d4cdb', stepped: true}, {label: 'W/kg', pts: s.w_per_kg, col: '#d98e04', stepped: true, extra: {yAxisID: 'y1'}}]);
    line('body-hr-chart', [{label: 'max HR', pts: s.max_hr, col: '#c0392b', stepped: true}, {label: 'threshold HR', pts: s.threshold_hr, col: '#d98e04', stepped: true}]);
    const hrB = [.5, .6, .7, .8, .9, 1.0], pB = [0, .55, .75, .9, 1.05, 1.2, 1.5];
    table('body-hr-zones', [{label: 'HR zone', get: (z) => `Z${z.n}`}, {label: 'bpm', get: (z) => z.r}], maxHr ? hrB.slice(0, 5).map((lo, i) => ({n: i + 1, r: `${Math.round(lo * maxHr)} – ${Math.round(hrB[i + 1] * maxHr)}`})) : []);
    table('body-power-zones', [{label: 'Power zone', get: (z) => `Z${z.n}`}, {label: 'W', get: (z) => z.r}], ftp ? pB.map((lo, i) => ({n: i + 1, r: i < 6 ? `${Math.round(lo * ftp)} – ${Math.round(pB[i + 1] * ftp)}` : `> ${Math.round(lo * ftp)}`})) : []);
}
async function loadGear() { const g = await api('/api/gear');
    table('gear-table', [{label: 'Device', html: (d) => `<b>${esc([d.manufacturer, d.product].filter(Boolean).join(' ') || d.device_type || 'unknown')}</b>${d.is_watch ? ' <span class="tag">recorder</span>' : ''}`}, {label: 'Type', get: (d) => fmt.sport(d.device_type)}, {label: 'Serial', get: (d) => d.serial},
        {label: 'Activities', num: 1, get: (d) => d.activities}, {label: 'First seen', get: (d) => fmt.day(d.first_seen)}, {label: 'Last seen', get: (d) => fmt.day(d.last_seen)},
        {label: 'Firmware', get: (d) => d.firmware.map((f) => f.version).slice(-3).join(' → ')}, {label: 'Battery', html: (d) => d.battery_now ? `<span class="tag ${['low', 'critical'].includes(d.battery_now.status) ? 'bad' : 'good'}">${esc(d.battery_now.status || 'ok')}</span> ${d.battery_now.voltage ? fmt.num(d.battery_now.voltage, 2) + ' V' : ''}` : ''},
        {label: 'Hours used', num: 1, get: (d) => d.operating_time_s ? Math.round(d.operating_time_s / 3600) : ''}], g.devices); }

// ---------------------------------------------------------------- explorer
async function loadExplorer(fileId) {
    const list = await api('/api/explorer/files?q=' + encodeURIComponent($('exp-file-search').value));
    $('exp-file').innerHTML = list.files.map((f) => `<option value="${f.id}">${esc(fmt.day(f.start_time))} ${esc(f.sports.join('+'))} · ${esc(f.name)}</option>`).join('');
    const pick = fileId || state.exp.file || (list.files[0] && list.files[0].id); if (pick) { $('exp-file').value = pick; openExplorerFile(+pick); }
}
async function openExplorerFile(id) {
    state.exp.file = id; state.exp.message = null; $('exp-crc-result').textContent = '';
    const d = await api(`/api/explorer/${id}/messages`); if (!d._ok) return;
    $('exp-header').innerHTML = `${esc(d.file.manufacturer || '')} ${esc(d.file.product || '')} · serial ${esc(d.file.serial_number || '–')}<br>FIT protocol ${esc(d.header.protocol_version)}, profile ${esc(d.header.profile_version)} · ${fmt.bytes(d.header.file_size)}` + (d.file.parse_status !== 'ok' ? `<br><span class="tag ${esc(d.file.parse_status)}">${esc(d.file.parse_status)}</span> ${esc(d.file.parse_error)}` : '');
    table('exp-messages', [{label: 'Message', html: (m) => `<span class="${m.unknown ? 'unknown' : ''}">${esc(m.name)}</span>`}, {label: 'Rows', num: 1, get: (m) => m.count}], d.messages, (m) => { state.exp.message = m.name; state.exp.page = 1; loadExplorerRows(); });
    $('exp-rows').innerHTML = ''; $('exp-title').textContent = 'Pick a message type'; $('exp-count').textContent = ''; $('exp-chart-wrap').style.display = 'none';
}
async function loadExplorerRows() {
    const e = state.exp; if (!e.message) return;
    const units = $('exp-standard').checked ? '&units=standard' : '';
    const d = await api(`/api/explorer/${e.file}/messages/${e.message}?page=${e.page}&per_page=100${units}`); if (!d._ok) return;
    $('exp-title').textContent = d.message; $('exp-count').textContent = `${d.total} rows · ${d.fields.length} fields`;
    $('exp-error').innerHTML = d.error ? `<div class="banner">${esc(d.error)}</div>` : '';
    const hide = $('exp-hide-unknown').checked, raw = $('exp-raw').checked;
    const cols = d.fields.map((f, i) => ({f, i})).filter((c) => !(hide && c.f.unknown));
    table('exp-rows', cols.map((c) => ({label: c.f.name + (c.f.units ? ` (${c.f.units})` : '') + (c.f.developer ? ' ◆dev' : '') + (c.f.def_num != null ? ` #${c.f.def_num}` : ''),
        html: (row) => { const v = row.v[c.i], r = row.r[c.i]; const show = (x) => x == null ? '' : typeof x === 'object' ? JSON.stringify(x) : x;
            return `<span class="${c.f.unknown ? 'unknown' : ''}">${esc(show(v))}</span>` + (raw && JSON.stringify(r) !== JSON.stringify(v) ? ` <span class="muted">[${esc(show(r))}]</span>` : ''); }})),
        d.rows.map((v, i) => ({v, r: d.raw[i]})));
    $('exp-page').textContent = `page ${d.page} of ${Math.max(1, Math.ceil(d.total / d.per_page))}`;
    $('exp-prev').disabled = d.page <= 1; $('exp-next').disabled = d.page * d.per_page >= d.total;
    $('exp-chart-field').innerHTML = '<option value="">Chart a field…</option>' + d.fields.filter((f) => f.numeric).map((f) => `<option>${esc(f.name)}</option>`).join('');
    $('exp-json').href = `/api/explorer/${e.file}/dump/${e.message}.json${units.replace('&', '?')}`; $('exp-csv').href = `/api/explorer/${e.file}/dump/${e.message}.csv${units.replace('&', '?')}`;
}
async function chartExplorerField() { const field = $('exp-chart-field').value; $('exp-chart-wrap').style.display = field ? '' : 'none'; if (!field) return;
    const d = await api(`/api/explorer/${state.exp.file}/series?message=${state.exp.message}&field=${field}`);
    chart('exp-chart', {type: 'line', data: {labels: d.index, datasets: [{label: `${d.field}${d.units ? ` (${d.units})` : ''}`, data: d.values, borderColor: '#0b6bcb', pointRadius: 0, borderWidth: 1.2, spanGaps: true}]}, options: {scales: {x: {ticks: {maxTicksLimit: 10}}}}}); }

// ---------------------------------------------------------------- import + garmin sync
function renderJob(job) { if (!job) return ''; const pct = job.total ? Math.round(job.progress / job.total * 100) : 0, c = job.result && job.result.counts;
    return `<div><b>${esc(job.kind)}</b> <span class="tag ${job.status === 'failed' ? 'bad' : job.status === 'done' ? 'good' : 'warn'}">${esc(job.status)}</span> ${job.total ? `${job.progress}/${job.total}` : ''} <span class="muted">${esc(job.message || '')}</span>` +
        (['queued', 'running'].includes(job.status) ? `<progress max="100" value="${pct}"></progress>` : '') + (c ? `<div>${Object.entries(c).map(([k, v]) => `${esc(k)}: <b>${v}</b>`).join(' · ')}</div>` : '') +
        (job.result && job.result.problems ? job.result.problems.slice(0, 8).map((p) => `<div class="muted">${esc(p.status)} · ${esc(p.name)} · ${esc(p.reason || '')}</div>`).join('') : '') + '</div>'; }
function watchJob(id) { clearInterval(state.poll); const tick = async () => { const d = await api(`/api/jobs/${id}`, {quiet: true}); if (!d._ok) { clearInterval(state.poll); return; }
        $('imp-job').innerHTML = renderJob(d.job); if (['done', 'failed'].includes(d.job.status)) { clearInterval(state.poll); state.poll = null; loadImport(true); loadSports(); } };
    tick(); state.poll = setInterval(tick, 1200); }
async function upload(files) { const form = new FormData(); [...files].forEach((f) => form.append('files', f));
    $('imp-job').innerHTML = `Uploading ${files.length} file(s)…`; const d = await api('/api/import/upload', {method: 'POST', form}); if (d._ok) watchJob(d.job.id); else $('imp-job').innerHTML = ''; }
async function loadImport(keepJob) {
    const [probs, jobs, sync] = await Promise.all([api('/api/import/problems'), api('/api/jobs'), api('/api/sync/status')]);
    table('imp-problems', [{label: 'File', get: (f) => f.name}, {label: 'Activity', get: (f) => fmt.day(f.start_time)}, {label: 'Status', html: (f) => `<span class="tag ${esc(f.status)}">${esc(f.status)}</span>`}, {label: 'Why', get: (f) => f.error},
        {label: '', html: (f) => `<button data-x="${f.id}">Explore</button>`}], probs.files);
    $('imp-problems').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => go('explorer', b.dataset.x)));
    table('imp-jobs', [{label: '#', get: (j) => j.id}, {label: 'Kind', get: (j) => j.kind}, {label: 'Status', html: (j) => `<span class="tag ${j.status === 'failed' ? 'bad' : j.status === 'done' ? 'good' : 'warn'}">${esc(j.status)}</span>`},
        {label: 'Progress', get: (j) => j.total ? `${j.progress}/${j.total}` : ''}, {label: 'Result', get: (j) => j.result ? JSON.stringify(j.result.counts || j.result).slice(0, 120) : j.message}, {label: 'When', get: (j) => fmt.when(j.created_at)}], jobs.jobs);
    const active = jobs.jobs.find((j) => ['queued', 'running'].includes(j.status)); if (active && !state.poll && !keepJob) watchJob(active.id);
    renderSync(sync);
}
function renderSync(s) {
    const c = s.counts, counts = Object.keys(c).length ? Object.entries(c).map(([k, v]) => `${esc(k)}: <b>${v}</b>`).join(' · ') : 'nothing listed yet';
    let html = '';
    if (s.auth === 'ok') html += `<div class="banner good">Connected. Last sync ${s.last_sync_at ? fmt.when(s.last_sync_at) : 'never'} · back-fill ${s.backfill_done ? 'complete' : 'in progress'}${s.health_synced_to ? ` · health to ${s.health_synced_to}` : ''}</div>
        <div class="row"><button class="primary" id="sync-run">Sync now</button><button id="sync-full">Full re-list</button><button id="sync-disconnect" class="danger">Disconnect</button></div>`;
    else { html += s.auth === 'login_required' ? `<div class="banner bad">Garmin needs you to sign in again${s.last_error ? `: <code>${esc(s.last_error)}</code>` : '.'}</div>` : '<div class="banner">Not connected. You can always just upload files or a Garmin export zip instead.</div>';
        if (s.web_connect.allowed) html += `<p class="muted">Connecting means this server keeps an (encrypted) token with full access to your Garmin account until you disconnect. Your password is used once and never stored.</p>
            <label class="field"><span>Garmin email</span><input id="sync-email" autocomplete="off"></label><label class="field"><span>Garmin password</span><input id="sync-password" type="password" autocomplete="off"></label>
            <div id="sync-mfa-wrap" style="display:none"><label class="field"><span>Code from Garmin (MFA)</span><input id="sync-mfa" inputmode="numeric"></label></div><button class="primary" id="sync-connect">Connect Garmin</button>`;
        else html += `<p class="muted">${s.web_connect.reason === 'https_required' ? 'Connecting from the browser is only offered over HTTPS - use the Tailscale address.' : 'Connecting from the browser is switched off for your account - ask the admin to enable it.'}${state.user.role === 'admin' ? ` On the server: <code>${esc(s.cli_hint)}</code>` : ''}</p>`; }
    html += `<p>Activities known from Garmin: ${counts}</p>` + (s.running ? renderJob(s.running) : s.last_job ? renderJob(s.last_job) : '') + '<div id="sync-failed"></div>';
    $('sync-panel').innerHTML = html;
    const on = (id, fn) => { if ($(id)) $(id).addEventListener('click', fn); };
    const run = async (full) => { const d = await api('/api/sync/run', {method: 'POST', json: {full, health: true}}); if (d._ok) watchJob(d.job.id); };
    on('sync-run', () => run(false)); on('sync-full', () => run(true));
    on('sync-disconnect', async () => { if (confirm('Remove the stored Garmin tokens?')) { await api('/api/sync/disconnect', {method: 'POST'}); loadImport(); } });
    on('sync-connect', async () => { const mfa = $('sync-mfa-wrap').style.display !== 'none';
        const d = mfa ? await api('/api/sync/login/mfa', {method: 'POST', json: {code: $('sync-mfa').value}}) : await api('/api/sync/login', {method: 'POST', json: {email: $('sync-email').value, password: $('sync-password').value}});
        $('sync-password').value = ''; if (d.status === 'needs_mfa') { $('sync-mfa-wrap').style.display = ''; toast('Garmin sent you a code.'); } else if (d.status === 'ok') { toast('Garmin connected.'); loadImport(); } });
    if (s.running && !state.poll) watchJob(s.running.id);
    if (c.failed) api('/api/sync/activities?status=failed').then((d) => { $('sync-failed').innerHTML = '<table id="sync-failed-table"></table>';
        table('sync-failed-table', [{label: 'Failed activity', get: (a) => `${fmt.day(a.start_time)} ${a.name || a.activity_id}`}, {label: 'Why', get: (a) => a.error}, {label: '', html: (a) => `<button data-a="${esc(a.activity_id)}">Retry</button>`}], d.activities);
        $('sync-failed').querySelectorAll('button').forEach((b) => b.addEventListener('click', async () => { await api(`/api/sync/activities/${b.dataset.a}/retry`, {method: 'POST'}); loadImport(); })); });
}

// ---------------------------------------------------------------- settings, admin
async function loadSettings() {
    const s = state.settings = await api('/api/settings');
    $('set-units').value = s.units; $('set-week').value = s.week_start; $('set-range').value = s.default_range_days; $('set-load').value = s.load_model;
    $('set-maxhr').value = s.zones.max_hr || ''; $('set-lthr').value = s.zones.threshold_hr || ''; $('set-rhr').value = s.zones.resting_hr || ''; $('set-ftp').value = s.zones.ftp || '';
    const d = await api('/api/auth/devices');
    table('set-devices', [{label: 'Browser', get: (t) => (t.device_name || '').slice(0, 60)}, {label: 'Signed in', get: (t) => fmt.when(t.created_at)}, {label: 'Last used', get: (t) => fmt.when(t.last_used_at)}, {label: 'Expires', get: (t) => fmt.day(t.expires_at)},
        {label: '', html: (t) => t.current ? '<span class="tag good">this one</span>' : `<button data-t="${t.id}">Sign out</button>`}], d.devices);
    $('set-devices').querySelectorAll('button').forEach((b) => b.addEventListener('click', async () => { await api(`/api/auth/devices/${b.dataset.t}/revoke`, {method: 'POST'}); loadSettings(); }));
}
async function loadAdmin() {
    if (!$('view-admin')) return; const [st, users, inv, gs] = await Promise.all([api('/api/admin/status'), api('/api/admin/users'), api('/api/admin/invites'), api('/api/admin/settings')]);
    $('adm-tiles').innerHTML = [tile('Users', st.users), tile('Files', st.files), tile('Sessions', st.sessions), tile('Jobs', `${st.jobs_running} running`, `${st.jobs_queued} queued`), tile('Database', fmt.bytes(st.db_bytes)), tile('Disk free', fmt.bytes(st.disk_free_bytes), `of ${fmt.bytes(st.disk_total_bytes)}`)].join('');
    const post = (u, json) => api(`/api/admin/users/${u.id}`, {method: 'POST', json}).then(loadAdmin);
    table('adm-users', [{label: 'User', html: (u) => `<b>${esc(u.username)}</b> <span class="muted">${esc(u.display_name || '')}</span>`}, {label: 'Role', get: (u) => u.role}, {label: 'Files', num: 1, get: (u) => u.files}, {label: 'Size', num: 1, get: (u) => fmt.bytes(u.bytes)},
        {label: 'Last login', get: (u) => fmt.when(u.last_login_at)}, {label: '', html: (u) => `<button data-u="${u.id}" data-a="active">${u.is_active ? 'Disable' : 'Enable'}</button> <button data-u="${u.id}" data-a="role">${u.role === 'admin' ? 'Make user' : 'Make admin'}</button> <button data-u="${u.id}" data-a="garmin">Garmin web connect: ${u.garmin_web_connect ? 'on' : 'off'}</button>`}], users.users);
    $('adm-users').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => { const u = users.users.find((x) => x.id === +b.dataset.u);
        post(u, b.dataset.a === 'active' ? {is_active: !u.is_active} : b.dataset.a === 'role' ? {role: u.role === 'admin' ? 'user' : 'admin'} : {garmin_web_connect: !u.garmin_web_connect}); }));
    table('adm-invites', [{label: 'For', get: (i) => i.note}, {label: 'State', html: (i) => `<span class="tag ${i.state === 'open' ? 'good' : ''}">${esc(i.state)}</span>`}, {label: 'Expires', get: (i) => fmt.day(i.expires_at)},
        {label: '', html: (i) => i.state === 'open' ? `<button data-i="${i.id}">Revoke</button>` : ''}], inv.invites);
    $('adm-invites').querySelectorAll('button').forEach((b) => b.addEventListener('click', async () => { await api(`/api/admin/invites/${b.dataset.i}/revoke`, {method: 'POST'}); loadAdmin(); }));
    $('adm-base-url').value = gs.public_base_url; $('adm-quota-mb').value = gs.user_quota_mb; $('adm-budget').value = gs.garmin_daily_request_budget; loadEvents();
}
async function loadEvents() { const d = await api('/api/admin/events?prefix=' + encodeURIComponent($('adm-event-prefix').value));
    table('adm-events', [{label: 'Time', get: (e) => fmt.when(e.timestamp)}, {label: 'Event', get: (e) => e.event}, {label: 'Message', get: (e) => e.message}, {label: 'User', get: (e) => e.user_id ?? e.username ?? ''}, {label: 'From', get: (e) => e.tailscale_user || e.ip || ''}], d.events.slice().reverse()); }

async function loadSports() { const d = await api('/api/sports'), cur = $('flt-sport').value;
    $('flt-sport').innerHTML = '<option value="">All sports</option>' + d.sports.map((s) => `<option value="${esc(s.sport)}">${esc(fmt.sport(s.sport))} (${s.count})</option>`).join(''); $('flt-sport').value = cur; }

// ---------------------------------------------------------------- wiring
function wire() {
    document.querySelectorAll('#tabs a').forEach((a) => a.addEventListener('click', () => go(a.dataset.view)));
    document.body.addEventListener('click', (ev) => { const a = ev.target.closest('[data-goto]'); if (a) go(a.dataset.goto); });
    ['flt-from', 'flt-to', 'flt-sport'].forEach((id) => $(id).addEventListener('change', () => { if (id !== 'flt-sport') state.rangeNote = ''; showNote(); reload(); }));
    $('flt-preset').addEventListener('change', (ev) => { if (ev.target.value) { setPreset(ev.target.value === 'all' ? 'all' : +ev.target.value); ev.target.value = ''; state.rangeNote = ''; showNote(); reload(); } });
    $('logout').addEventListener('click', async () => { await api('/api/auth/logout', {method: 'POST'}); location.href = '/login'; });
    $('dash-volume-metric').addEventListener('change', (ev) => volumeChart('dash-volume-chart', state.dashVolume, ev.target.value));
    let t; $('act-search').addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => loadActivities(true), 250); });
    $('act-transitions').addEventListener('change', () => loadActivities(true));
    $('act-excluded').addEventListener('change', () => loadActivities(true)); $('act-more').addEventListener('click', () => { state.act.offset = state.act.rows.length; loadActivities(false); });
    $('det-xaxis').addEventListener('change', drawSeries); $('det-map-color').addEventListener('change', paintTrack);
    $('det-trim').addEventListener('click', () => { const p = $('det-trim-panel');
        p.style.display = ''; if (!p.innerHTML) drawTrim(state.session); p.scrollIntoView({behavior: 'smooth'}); });
    $('det-exclude').addEventListener('click', async () => {
        const s = state.session.session;
        const r = await api(`/api/sessions/${s.id}/exclude`, {method: 'POST', json: {excluded: !s.excluded}});
        if (r._ok) { toast(r.excluded ? 'Left out of analyses.' : 'Counted as training again.'); reload(); }
    });
    $('det-explore').addEventListener('click', () => go('explorer', state.session.file.id));
    $('det-rename').addEventListener('click', async () => { const name = prompt('Activity name', state.session.session.name || ''); if (name != null) { await api(`/api/sessions/${state.session.session.id}/name`, {method: 'POST', json: {name}}); reload(); } });
    $('det-reparse').addEventListener('click', async () => { const d = await api(`/api/files/${state.session.file.id}/reparse`, {method: 'POST'}); toast(`Re-parsed: ${d.parse_status}`); go('activities'); });
    $('det-delete').addEventListener('click', async () => { if (confirm('Delete this file and everything derived from it?')) { await api(`/api/files/${state.session.file.id}/delete`, {method: 'POST'}); go('activities'); } });
    ['tr-volume-bucket'].forEach((id) => $(id).addEventListener('change', loadTrends)); $('tr-volume-metric').addEventListener('change', (ev) => volumeChart('tr-volume-chart', state.trVolume, ev.target.value));
    $('tr-pr-kind').addEventListener('change', loadRecordProgression); $('tr-eff-sport').addEventListener('change', loadEfficiency);
    $('exp-file').addEventListener('change', (ev) => openExplorerFile(+ev.target.value)); let t2; $('exp-file-search').addEventListener('input', () => { clearTimeout(t2); t2 = setTimeout(() => loadExplorer(), 250); });
    ['exp-standard', 'exp-raw', 'exp-hide-unknown'].forEach((id) => $(id).addEventListener('change', loadExplorerRows));
    $('exp-prev').addEventListener('click', () => { state.exp.page--; loadExplorerRows(); }); $('exp-next').addEventListener('click', () => { state.exp.page++; loadExplorerRows(); });
    $('exp-chart-field').addEventListener('change', chartExplorerField);
    $('exp-crc').addEventListener('click', async () => { $('exp-crc-result').textContent = 'checking…'; const d = await api(`/api/explorer/${state.exp.file}/messages?crc=1`);
        $('exp-crc-result').innerHTML = d.crc.crc_ok ? '<span class="tag good">CRC ok</span>' : `<span class="tag bad">CRC ${d.crc.crc_ok === false ? 'mismatch' : 'unreadable'}</span> <span class="muted">${esc(d.crc.detail || '')}</span>`; });
    $('exp-search').addEventListener('click', async () => { const d = await api('/api/explorer/search?message=' + encodeURIComponent($('exp-search-name').value.trim())); if (!d._ok) return;
        $('exp-search-result').innerHTML = d.files.length ? d.files.map((f) => `<div><a data-f="${f.id}">${esc(fmt.day(f.start_time))} ${esc(f.name)}</a> <span class="muted">${f.count} rows</span></div>`).join('') : '<span class="muted">In none of your files.</span>';
        $('exp-search-result').querySelectorAll('a').forEach((a) => a.addEventListener('click', () => { $('exp-file').value = a.dataset.f; openExplorerFile(+a.dataset.f); })); });
    const drop = $('imp-drop'); $('imp-browse').addEventListener('click', () => $('imp-files').click()); $('imp-files').addEventListener('change', (ev) => { if (ev.target.files.length) upload(ev.target.files); ev.target.value = ''; });
    ['dragenter', 'dragover'].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add('over'); })); ['dragleave', 'drop'].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', (ev) => { if (ev.dataTransfer.files.length) upload(ev.dataTransfer.files); });
    if ($('imp-scan')) $('imp-scan').addEventListener('click', async () => { const d = await api('/api/import/scan', {method: 'POST', json: {directory: $('imp-scan-dir').value}}); if (d._ok) watchJob(d.job.id); });
    $('imp-reindex').addEventListener('click', async () => { const d = await api('/api/import/reindex', {method: 'POST', json: {force: true}}); if (d._ok) watchJob(d.job.id); });
    const n = (id) => $(id).value === '' ? null : +$(id).value;
    $('set-save').addEventListener('click', async () => { const d = await api('/api/settings', {method: 'POST', json: {units: $('set-units').value, week_start: $('set-week').value, default_range_days: n('set-range') || 365, load_model: $('set-load').value,
        zones: {max_hr: n('set-maxhr'), threshold_hr: n('set-lthr'), resting_hr: n('set-rhr'), ftp: n('set-ftp')}}}); if (d._ok) { state.settings = d; toast('Saved. Re-index under Import to apply new zones to past activities.'); } });
    $('set-pw-save').addEventListener('click', async () => { const d = await api('/api/auth/password', {method: 'POST', json: {old_password: $('set-pw-old').value, new_password: $('set-pw-new').value}});
        if (d._ok) { state.csrf = d.csrf; $('set-pw-old').value = $('set-pw-new').value = ''; toast('Password changed.'); loadSettings(); } });
    $('set-logout-all').addEventListener('click', async () => { await api('/api/auth/logout-all', {method: 'POST'}); location.href = '/login'; });
    $('set-delete').addEventListener('click', async () => { if (!confirm('This permanently deletes your account, every file and all derived data. Continue?')) return;
        const d = await api('/api/account/delete', {method: 'POST', json: {password: $('set-del-pw').value}}); if (d._ok) location.href = '/login'; });
    if ($('adm-invite')) { $('adm-invite').addEventListener('click', async () => { const d = await api('/api/admin/invites', {method: 'POST', json: {note: $('adm-invite-note').value}});
            if (d._ok) { $('adm-invite-url').innerHTML = `<div class="banner good">Send this link (shown once, valid 7 days):<br><code>${esc(d.url)}</code></div>`; loadAdmin(); } });
        $('adm-save').addEventListener('click', async () => { const d = await api('/api/admin/settings', {method: 'POST', json: {public_base_url: $('adm-base-url').value.trim(), user_quota_mb: +$('adm-quota-mb').value, garmin_daily_request_budget: +$('adm-budget').value}}); if (d._ok) toast('Saved.'); });
        $('adm-event-prefix').addEventListener('change', loadEvents); }
    window.addEventListener('hashchange', route);
}

(async function start() {
    state.settings = await api('/api/settings');
    await initialRange();
    wire(); await loadSports();
    if (!location.hash) { let last = null; try { last = localStorage.getItem('fitmon.view'); } catch (e) { /* ignore */ } if (last && last !== 'detail' && VIEWS[last]) location.hash = '#' + last; }
    route();
})();
})();
