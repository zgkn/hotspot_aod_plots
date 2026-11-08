"""The period selector of the map and the layers it drives, as HTML + JSON data + JavaScript (no libraries besides Leaflet).

A control at the top left chooses what the daytime-hotspot and AOD layers show: the latest day, the past 14 days, or a date range.
* hotspots: every daytime detection of the days in the period (a fire seen on two days is drawn twice), on one canvas;
* AOD: the mean of the daily AOD grids of the days in the period that have one, drawn as a coloured image;
* the night layer shows the newest night, as before, with the same canvas.
The days come from small files next to the page (data/fires_YYYYMMDD.json, data/aod_YYYYMMDD.png) that are loaded when needed.
Clicking a hotspot opens its popup (with links to Google Maps) and not the trend chart of the area under it.
"""
import json

MAX_DAYS = 31

STYLE = """
<style>
.pp-box { background: rgba(20,20,20,.88); color: #fff; font: 11px Arial, sans-serif; border: 1px solid #444; border-radius: 6px;
          padding: 8px 10px; width: 222px; box-sizing: border-box; box-shadow: 0 0 15px rgba(0,0,0,.6); }
.pp-box .pp-t { display: flex; align-items: center; gap: 6px; width: 100%; margin: 0 0 5px; padding: 0; background: none; border: 0;
                color: #fff; font: 700 12px Arial, sans-serif; text-align: left; cursor: pointer; }
.pp-box .pp-t .pp-car { margin-left: auto; color: #aaa; }
.pp-box.pp-min { width: auto; max-width: 222px; padding: 6px 9px; }
.pp-box.pp-min .pp-t { margin: 0; }
.pp-box.pp-min .pp-body { display: none; }
.pp-btns { display: flex; gap: 4px; margin-bottom: 5px; }
.pp-btns button { flex: 1; background: #2a2a2a; color: #ddd; border: 1px solid #555; border-radius: 4px; padding: 5px 2px;
                  font: 11px Arial, sans-serif; cursor: pointer; line-height: 1.15; }
.pp-btns button.on { background: #0e4a52; border-color: #4dd0e1; color: #fff; }
.pp-range { display: none; grid-template-columns: 34px 1fr; gap: 4px 6px; align-items: center; margin-bottom: 5px; }
.pp-range.show { display: grid; }
.pp-range input { background: #222; color: #fff; border: 1px solid #555; border-radius: 4px; padding: 3px 4px; min-width: 0;
                  font: 11px Arial, sans-serif; color-scheme: dark; }
.pp-st { color: #bbb; line-height: 1.35; }
.pp-st b { color: #fff; }
.aod-img { image-rendering: auto; }
@media (max-width: 760px) {
    .pp-box { width: 198px; padding: 6px 8px; font-size: 10px; }
    .pp-box.pp-min { max-width: 198px; }
    .pp-btns button { font-size: 10px; padding: 5px 1px; }
}
</style>
"""

# __MAP__, __DAYGROUP__, __NIGHTGROUP__, __AODGROUP__ are replaced by the names of the Leaflet objects
SCRIPT = r"""
window.addEventListener('load', function () {
    var map = __MAP__, dayGroup = __DAYGROUP__, nightGroup = __NIGHTGROUP__, aodGroup = __AODGROUP__;
    var D = JSON.parse(document.getElementById('period-data').textContent);
    var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    var S = D.domain[0], W = D.domain[1], N = D.domain[2], E = D.domain[3];
    var BLANK = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
    var firesCache = {}, aodCache = {}, seq = 0;

    function ymd(iso) { return iso.replace(/-/g, ''); }
    function addDays(iso, n) { var d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); }
    function dayCount(a, b) { return Math.round((Date.parse(b + 'T00:00:00Z') - Date.parse(a + 'T00:00:00Z')) / 864e5) + 1; }
    function label(iso) { var p = iso.split('-'); return (+p[2]) + ' ' + MONTHS[+p[1] - 1] + ' ' + p[0]; }
    function span(a, b) { return a === b ? label(a) : label(a) + ' &ndash; ' + label(b); }
    function between(list, a, b) { return list.filter(function (x) { return x >= a && x <= b; }); }

    // ---- the day files --------------------------------------------------------------------------------------------------
    function loadFires(iso) {
        if (!firesCache[iso]) firesCache[iso] = fetch('data/fires_' + ymd(iso) + '.json').then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        });
        return firesCache[iso];
    }
    function loadAod(iso) {
        if (!aodCache[iso]) aodCache[iso] = new Promise(function (res, rej) {
            var im = new Image();
            im.onload = function () {
                var c = document.createElement('canvas');
                c.width = D.nx; c.height = D.ny;
                var x = c.getContext('2d', {willReadFrequently: true});
                x.drawImage(im, 0, 0);
                var px = x.getImageData(0, 0, D.nx, D.ny).data, g = new Uint8Array(D.nx * D.ny);
                for (var i = 0; i < g.length; i++) g[i] = px[4 * i];
                res(g);
            };
            im.onerror = function () { rej(new Error('image')); };
            im.src = 'data/aod_' + ymd(iso) + '.png';
        });
        return aodCache[iso];
    }
    function assemble(parts) {                   // [{iso, doc}] -> one set of arrays; day = index into dates
        var n = 0, o = 0;
        parts.forEach(function (p) { n += p.doc.n; });
        var d = {n: n, lat: new Float64Array(n), lon: new Float64Array(n), frp: new Float32Array(n), sat: new Uint8Array(n),
                 day: new Uint8Array(n), dates: parts.map(function (p) { return p.iso; })};
        parts.forEach(function (p, k) {
            for (var i = 0; i < p.doc.n; i++, o++) {
                d.lat[o] = p.doc.lat[i] / 1e4; d.lon[o] = p.doc.lon[i] / 1e4; d.frp[o] = p.doc.frp[i] / 10; d.sat[o] = p.doc.sat[i]; d.day[o] = k;
            }
        });
        return d;
    }

    // ---- the hotspots: one canvas per layer, drawn from the arrays -----------------------------------------------------
    var HotLayer = L.Layer.extend({
        initialize: function (pane, z, night) { this._pane = pane; this._z = z; this._night = night; this._data = null; this._n = 0; },
        onAdd: function (m) {
            this._map = m;
            if (!m.getPane(this._pane)) { var p = m.createPane(this._pane); p.style.zIndex = this._z; p.style.pointerEvents = 'none'; }
            this._c = L.DomUtil.create('canvas', '', m.getPane(this._pane));
            this._c.style.pointerEvents = 'none';
            m.on('moveend zoomend resize', this._draw, this);
            m.on('zoomstart', this._clear, this);
            this._draw();
        },
        onRemove: function (m) {
            m.off('moveend zoomend resize', this._draw, this);
            m.off('zoomstart', this._clear, this);
            L.DomUtil.remove(this._c);
            this._c = null; this._map = null; this._n = 0;
        },
        setData: function (d) {
            var ord = new Uint32Array(d.n), i;
            for (i = 0; i < d.n; i++) ord[i] = i;
            ord.sort(function (a, b) { return d.frp[a] - d.frp[b]; });      // the strongest fires are drawn last, on top
            this._data = d; this._ord = ord;
            this._draw();
        },
        _clear: function () {
            if (this._c) this._c.getContext('2d').clearRect(0, 0, this._c.width, this._c.height);
            this._n = 0;
        },
        _draw: function () {
            var m = this._map, c = this._c, d = this._data;
            if (!m || !c) return;
            var size = m.getSize(), dpr = window.devicePixelRatio || 1;
            c.width = size.x * dpr; c.height = size.y * dpr; c.style.width = size.x + 'px'; c.style.height = size.y + 'px';
            L.DomUtil.setPosition(c, m.containerPointToLayerPoint([0, 0]));
            this._n = 0;
            if (!d || !d.n) return;
            var g = c.getContext('2d'); g.scale(dpr, dpr);
            var b = m.getBounds().pad(0.05), s = b.getSouth(), n = b.getNorth(), w = b.getWest(), e = b.getEast();
            var k = d.n > 8000 ? Math.max(0.45, Math.sqrt(8000 / d.n)) : 1;      // smaller circles when there are very many
            var sx = this._sx = new Float32Array(d.n), sy = this._sy = new Float32Array(d.n), sr = this._sr = new Float32Array(d.n),
                si = this._si = new Uint32Array(d.n), cnt = 0, col = '';
            g.globalAlpha = 0.85; g.lineWidth = k < 1 ? 1 : 2;
            for (var j = 0; j < d.n; j++) {
                var i = this._ord[j], la = d.lat[i], lo = d.lon[i];
                if (la < s || la > n || lo < w || lo > e) continue;
                var pt = m.latLngToContainerPoint([la, lo]), f = d.frp[i], r = Math.min(14, Math.max(6, f / 6)) * k;
                var cl = f < 10 ? '#FFD700' : (f < 50 ? '#FF8C00' : '#FF4500');
                if (cl !== col) { col = cl; g.fillStyle = cl; g.strokeStyle = cl; }
                g.beginPath(); g.arc(pt.x, pt.y, r, 0, 6.2832); g.fill(); g.stroke();
                sx[cnt] = pt.x; sy[cnt] = pt.y; sr[cnt] = r; si[cnt] = i; cnt++;
            }
            this._n = cnt;
        },
        hit: function (pt) {                     // the hotspot under a point of the map (a few pixels of tolerance), or null
            var best = -1, bd = 1e9;
            for (var j = this._n - 1; j >= 0; j--) {          // the one drawn last is on top
                var dx = this._sx[j] - pt.x, dy = this._sy[j] - pt.y, dd = Math.sqrt(dx * dx + dy * dy);
                if (dd <= this._sr[j] + 3) return this._si[j];
                if (dd < bd && dd <= this._sr[j] + 14) { bd = dd; best = j; }
            }
            return best >= 0 ? this._si[best] : null;
        }
    });
    var dayLayer = new HotLayer('hotDay', 420, false), nightLayer = new HotLayer('hotNight', 430, true);
    window.__periodLayers = {day: dayLayer, night: nightLayer};      // for tests and the console
    dayGroup.addLayer(dayLayer);
    nightGroup.addLayer(nightLayer);
    var aodImg = L.imageOverlay(BLANK, [[S, W], [N, E]], {interactive: false, className: 'aod-img'});
    aodGroup.addLayer(aodImg);

    function popupHtml(layer, i) {
        var d = layer._data, la = d.lat[i], lo = d.lon[i], at = la.toFixed(5) + ',' + lo.toFixed(5);
        return '<b>' + (layer._night ? '&#127756; Night pass ' : '&#128752;&#65039; ') + D.sats[d.sat[i]] + '</b><br>' + label(d.dates[d.day[i]]) +
            '<br>Lat: ' + la.toFixed(3) + '<br>Lon: ' + lo.toFixed(3) + '<br>FRP: <b>' + d.frp[i].toFixed(1) + ' MW</b>' +
            '<br><a href="https://www.google.com/maps/search/?api=1&query=' + at + '" target="_blank" rel="noopener">&#128205; Open in Google Maps</a>' +
            '<br><a href="https://www.google.com/maps/@?api=1&map_action=map&center=' + at + '&zoom=16&basemap=satellite" target="_blank" rel="noopener">' +
            '&#128752;&#65039; Google Maps satellite view</a>';
    }
    map.on('click', function (e) {               // registered before the area handler: a hotspot wins over the area under it
        var layers = [nightLayer, dayLayer];
        for (var k = 0; k < layers.length; k++) {
            var ly = layers[k], i = ly._map ? ly.hit(e.containerPoint) : null;
            if (i !== null) {
                if (e.originalEvent) e.originalEvent.__hot = true;
                var d = ly._data;
                L.popup({maxWidth: 220}).setLatLng([d.lat[i], d.lon[i]]).setContent(popupHtml(ly, i)).openOn(map);
                return;
            }
        }
    });

    // night: the newest night, from the page itself
    if (D.night && D.night.n) nightLayer.setData(assemble([{iso: D.night.date, doc: D.night}]));

    // ---- the AOD of the period: the mean of the daily grids ---------------------------------------------------------------
    function renderAod(grids) {
        var n = D.nx * D.ny, sum = new Float32Array(n), cnt = new Uint8Array(n), i;
        grids.forEach(function (g) { for (i = 0; i < n; i++) { var v = g[i]; if (v !== 255) { sum[i] += v; cnt[i]++; } } });
        var max = 0;
        for (i = 0; i < n; i++) if (cnt[i]) { var m = sum[i] / cnt[i] * D.aodStep; if (m > max) max = m; }
        var top = Math.max(1, Math.ceil(max - 1e-9));
        var c = document.createElement('canvas'); c.width = D.nx; c.height = D.ny;
        var x = c.getContext('2d'), img = x.createImageData(D.nx, D.ny), px = img.data;
        for (i = 0; i < n; i++) {
            if (!cnt[i]) continue;
            var lut = D.cmap[Math.min(255, Math.round(sum[i] / cnt[i] * D.aodStep / top * 255))];
            px[4 * i] = lut[0]; px[4 * i + 1] = lut[1]; px[4 * i + 2] = lut[2]; px[4 * i + 3] = 175;
        }
        x.putImageData(img, 0, 0);
        aodImg.setUrl(grids.length ? c.toDataURL('image/png') : BLANK);
        return grids.length ? top : null;
    }

    // ---- the control ---------------------------------------------------------------------------------------------------
    var latestAny = D.latestAod && D.latestAod > D.latestFire ? D.latestAod : D.latestFire;
    var firstDay = D.fires.length ? D.fires[0] : latestAny;
    var state = {mode: 'day'}, box, st, rangeEl, fromEl, toEl, btns;
    var Ctl = L.Control.extend({
        options: {position: 'topleft'},
        onAdd: function () {
            box = L.DomUtil.create('div', 'pp-box');
            box.innerHTML = '<button type="button" class="pp-t" aria-label="Show or hide the period choices">&#128197; <span class="pp-tt">Period</span><span class="pp-car">&#9662;</span></button>' +
                '<div class="pp-body"><div class="pp-btns"><button type="button" data-m="day">Latest day</button><button type="button" data-m="14">Past 14 days</button>' +
                '<button type="button" data-m="range">Date range</button></div>' +
                '<div class="pp-range"><label for="pp-from">From</label><input type="date" id="pp-from"><label for="pp-to">To</label><input type="date" id="pp-to"></div>' +
                '<div class="pp-st"></div></div>';
            L.DomEvent.disableClickPropagation(box);
            L.DomEvent.disableScrollPropagation(box);
            return box;
        }
    });
    map.addControl(new Ctl());
    var phone = window.matchMedia('(max-width: 760px)').matches, head = box.querySelector('.pp-t');
    function fold(min) {                         // on a phone the control is a small pill until it is tapped
        box.classList.toggle('pp-min', min);
        head.querySelector('.pp-car').innerHTML = min ? '&#9662;' : '&#9652;';
    }
    head.addEventListener('click', function () { fold(!box.classList.contains('pp-min')); });
    fold(phone);
    st = box.querySelector('.pp-st'); rangeEl = box.querySelector('.pp-range');
    fromEl = box.querySelector('#pp-from'); toEl = box.querySelector('#pp-to'); btns = box.querySelectorAll('.pp-btns button');
    fromEl.min = toEl.min = firstDay; fromEl.max = toEl.max = latestAny;

    function shortSpan(a, b) { return a === b ? label(a) : label(a).slice(0, -5) + ' &ndash; ' + label(b).slice(0, -5); }
    function setMode(mode) {
        state.mode = mode;
        if (phone && mode !== 'range' && seq) fold(true);
        Array.prototype.forEach.call(btns, function (b) { b.classList.toggle('on', b.getAttribute('data-m') === mode); });
        rangeEl.classList.toggle('show', mode === 'range');
        if (mode === 'range' && !fromEl.value) { toEl.value = latestAny; fromEl.value = addDays(latestAny, -6); }
        apply();
    }
    Array.prototype.forEach.call(btns, function (b) { b.addEventListener('click', function () { setMode(b.getAttribute('data-m')); }); });
    fromEl.addEventListener('change', function () { if (state.mode === 'range') apply(); });
    toEl.addEventListener('change', function () { if (state.mode === 'range') apply(); });

    function apply() {
        var my = ++seq, from, to, fireDays, aodDays, note = '';
        if (state.mode === 'day') {
            fireDays = [D.latestFire]; aodDays = D.latestAod ? [D.latestAod] : [];
        } else {
            if (state.mode === '14') { to = latestAny; from = addDays(to, -13); }
            else {
                from = fromEl.value; to = toEl.value;
                if (!from || !to) { st.innerHTML = 'Choose the first and the last day.'; return; }
                if (from > to) { var t = from; from = to; to = t; fromEl.value = from; toEl.value = to; }
                if (dayCount(from, to) > D.maxDays) { from = addDays(to, -(D.maxDays - 1)); fromEl.value = from; note = ' (a period is limited to ' + D.maxDays + ' days)'; }
            }
            fireDays = between(D.fires, from, to); aodDays = between(D.aod, from, to);
        }
        map.closePopup();                        // it would point at a hotspot of the old period
        head.querySelector('.pp-tt').innerHTML = state.mode === 'day' ? 'Latest day' : (state.mode === '14' ? 'Past 14 days' : shortSpan(from, to));
        st.innerHTML = 'Loading&hellip;';
        var settle = function (p) { return p.then(function (v) { return {ok: true, v: v}; }, function () { return {ok: false}; }); };
        Promise.all([Promise.all(fireDays.map(function (d) { return settle(loadFires(d)); })),
                     Promise.all(aodDays.map(function (d) { return settle(loadAod(d)); }))]).then(function (res) {
            if (my !== seq) return;              // a newer choice has been made meanwhile
            var fparts = [], grids = [], k;
            for (k = 0; k < fireDays.length; k++) if (res[0][k].ok) fparts.push({iso: fireDays[k], doc: res[0][k].v});
            for (k = 0; k < aodDays.length; k++) if (res[1][k].ok) grids.push(res[1][k].v);
            var fire = assemble(fparts);
            dayLayer.setData(fire);
            var top = renderAod(grids);
            var aodTitle = document.getElementById('aod-title'), aodMax = document.getElementById('aod-max');
            if (aodTitle) aodTitle.textContent = state.mode === 'day' ? (D.latestAod ? 'AOD ' + label(D.latestAod) : 'AOD') : 'mean AOD of ' + grids.length + ' day' + (grids.length === 1 ? '' : 's');
            if (aodMax && top) aodMax.innerHTML = top.toFixed(1) + '+ (Dense Plume)';
            var failed = (fireDays.length - fparts.length) + (aodDays.length - grids.length);
            var txt;
            if (state.mode === 'day') {
                txt = '<b>' + label(D.latestFire) + '</b>: ' + fire.n.toLocaleString() + ' daytime hotspots<br>AOD: ' +
                      (D.latestAod ? label(D.latestAod) : 'none');
            } else {
                var asked = dayCount(from, to);
                txt = '<b>' + span(from, to) + '</b><br>' + fire.n.toLocaleString() + ' daytime hotspots (' + fparts.length + ' of ' + asked + ' days)<br>' +
                      (grids.length ? 'AOD: mean of ' + grids.length + ' of ' + asked + ' days' : 'AOD: no data for these days');
            }
            if (failed) txt += '<br><span style="color:#ffb74d">Could not load ' + failed + ' day file' + (failed === 1 ? '' : 's') +
                               ' (open the page from its web address).</span>';
            st.innerHTML = txt + note;
        });
    }
    setMode('day');
});
"""


def period_panel(info):
    """HTML for the page: the style and the data. info: see script.py."""
    data = json.dumps({**info, "maxDays": MAX_DAYS}, separators=(",", ":"))
    return STYLE + f'<script type="application/json" id="period-data">{data}</script>'


def period_script(map_name, day_group, night_group, aod_group):
    return (SCRIPT.replace("__MAP__", map_name).replace("__DAYGROUP__", day_group)
            .replace("__NIGHTGROUP__", night_group).replace("__AODGROUP__", aod_group))
