"""The daily hotspot trend as a dark panel with a zoomable, scrollable bar chart per place (drawn in the browser, nothing to load).

Built like the model_forecast time-series panel: hidden until showTrend(true, place) is called (the page does that when an outlined
place is clicked), a place menu in the header switches between the places, x closes it, and on phones it is a bottom sheet.

The chart is drawn by a small script from the numbers embedded in the page, so it can follow the pointer:
  * press and drag along the chart (mouse or finger) moves the chosen day, shown as a line with its date;
  * wheel or pinch zooms in time, Shift+wheel or a two-finger drag scrolls, the 7 d / 14 d / 30 d / All buttons jump to a range;
  * the strip under the chart shows the whole period with the visible part as a window: drag it to scroll, drag its edges to zoom.
The time window and the chosen day are shared by all places, so switching place keeps the same days on screen.
"""
import html
import json
from statistics import median

LOW_COVERAGE = 0.6                  # a satellite with fewer granules than this share of its usual number is flagged
PHONE_MAX_WIDTH = 760               # px, same breakpoint as model_forecast


def _label(d):
    return f"{d.day} {d:%b}"


def _low_coverage(rows):
    """Dates on which some satellite had far fewer granules than usual (its counts are then not comparable)."""
    flagged = set()
    sats = {s for r in rows for s in r["granules"]}
    for s in sats:
        usual = median([r["granules"][s] for r in rows if r["granules"].get(s)] or [0])
        for r in rows:
            if r["granules"] and usual and r["granules"].get(s, 0) < LOW_COVERAGE * usual:
                flagged.add(r["date"])
    return flagged


STYLE = f"""
<style>
#trend-panel {{ display: none; position: fixed; bottom: 35px; right: 12px; width: 460px; max-width: calc(100vw - 24px);
    max-height: calc(100vh - 90px); overflow-y: auto; background-color: rgba(20, 20, 20, 0.92); color: #fff; z-index: 10000;
    font-size: 11px; font-family: Arial, sans-serif; border: 1px solid #444; padding: 10px 12px; border-radius: 6px;
    box-shadow: 0 0 15px rgba(0,0,0,0.6); box-sizing: border-box; }}
#trend-panel.open {{ display: block; }}
#trend-panel .tp-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }}
#trend-close {{ font: inherit; font-size: 20px; line-height: 1; cursor: pointer; padding: 0 7px; border-radius: 6px;
    border: 1px solid #555; background: transparent; color: #fff; }}
#trend-place {{ font: inherit; font-weight: 700; font-size: 13px; color: #fff; background: #2a2a2a; border: 1px solid #555;
    border-radius: 6px; padding: 3px 4px; max-width: 100%; margin-top: 4px; }}
#trend-place option, #trend-place optgroup {{ color: #1a1a19; background: #fff; }}
.tp-place[hidden] {{ display: none; }}
#tp-ctl {{ display: flex; align-items: center; gap: 4px; flex-wrap: wrap; margin: 6px 0 0; }}
#tp-ctl button {{ font: inherit; font-size: 11px; cursor: pointer; padding: 2px 9px; border-radius: 12px; border: 1px solid #555;
    background: transparent; color: #fff; }}
#tp-ctl button.on {{ background: #4dd0e1; border-color: #4dd0e1; color: #08323a; }}
#tp-range {{ margin-left: auto; color: #bbb; font-size: 10px; }}
#trend-read {{ min-height: 2.7em; margin: 4px 0 2px; color: #ddd; line-height: 1.3; }}
#tp-chart {{ touch-action: pan-y; cursor: crosshair; user-select: none; -webkit-user-select: none; outline: none; }}
#tp-chart:focus-visible {{ outline: 1px dashed #4dd0e1; }}
#tp-chart svg, #tp-nav svg {{ display: block; }}
#tp-nav {{ touch-action: none; cursor: grab; margin-top: 4px; user-select: none; -webkit-user-select: none; }}
#tp-nav:active {{ cursor: grabbing; }}
@media (max-width: {PHONE_MAX_WIDTH}px) {{
    #trend-panel {{ left: 8px; right: 8px; width: auto; max-width: none; bottom: max(8px, env(safe-area-inset-bottom));
        max-height: 70vh; padding: 8px 10px; }}
    #trend-read {{ min-height: 2.6em; }}
}}
</style>
"""

SCRIPT = r"""
<script>
(function () {
    var D = JSON.parse(document.getElementById('trend-data').textContent), N = D.labels.length;
    var panel = document.getElementById('trend-panel'), box = document.getElementById('tp-chart'), nav = document.getElementById('tp-nav'),
        read = document.getElementById('trend-read'), pick = document.getElementById('trend-place'), rangeEl = document.getElementById('tp-range'),
        chips = document.querySelectorAll('#tp-ctl button[data-days]'), hint = read.textContent;
    var BAR = '#FF8C00', PARTIAL = '#FFC27A', AVG = '#4dd0e1', GRID = '#444', TXT = '#bbb';
    var PL = 38, PR = 6, PT = 8, PB = 26, MIN_DAYS = Math.min(7, N);
    var view = {a: 0, w: N};                   // first day shown (days may be fractional while zooming) and how many days are shown
    var sel = null, hov = null, place = D.order[0], cw = 0, ch = 176, avgs = {};

    function series() { return D.places[place]; }
    function plotW() { return cw - PL - PR; }
    function clampView() { view.w = Math.max(MIN_DAYS, Math.min(N, view.w)); view.a = Math.max(0, Math.min(N - view.w, view.a)); }
    function niceStep(top) {
        for (var e = 1; ; e *= 10) for (var j = 0, ms = [1, 2, 5]; j < 3; j++) if (Math.ceil(top / (ms[j] * e)) <= 5) return ms[j] * e;
    }
    function movingMean() {                    // trailing 7-day mean of the complete days, per place
        if (avgs[place]) return avgs[place];
        var c = series().c, k = series().k, out = [];
        for (var i = 0; i < N; i++) {
            var s = 0, n = 0;
            for (var j = Math.max(0, i - 6); j <= i; j++) if (c[j] !== null && k[j]) { s += c[j]; n++; }
            out.push(N >= 7 && n >= 4 ? s / n : null);
        }
        return (avgs[place] = out);
    }
    function tip(i) {
        var p = series(), t = D.labels[i] + ' ' + D.years[i] + ': ';
        if (p.c[i] === null) return t + 'no data';
        t += p.c[i] + ' unique daytime hotspots';
        if (!p.k[i]) t += ' (partial: passes of this day are still being posted)';
        if (D.low[i]) t += ' (a satellite had far fewer granules than usual)';
        var g = D.gran[i], ks = Object.keys(g);
        if (ks.length) t += '. Granules: ' + ks.map(function (s) { return s + ' ' + g[s]; }).join(', ');
        return t;
    }

    // ---- the chart ------------------------------------------------------------------------------------------------------
    function draw() {
        cw = box.clientWidth;
        if (!cw) return;
        ch = window.innerWidth <= %PHONE% ? 168 : 176;
        var pw = plotW(), ph = ch - PT - PB, bw = pw / view.w, c = series().c, k = series().k, am = movingMean();
        var lo = Math.max(0, Math.floor(view.a)), hi = Math.min(N - 1, Math.ceil(view.a + view.w) - 1), top = 1, i;
        for (i = lo; i <= hi; i++) if (c[i] !== null && c[i] > top) top = c[i];
        var step = niceStep(top), ymax = step * Math.ceil(top / step);
        function X(d) { return PL + (d - view.a) * bw; }
        function Y(v) { return PT + ph * (1 - v / ymax); }
        var s = '<svg width="' + cw + '" height="' + ch + '" viewBox="0 0 ' + cw + ' ' + ch + '" role="img" aria-label="Daily daytime hotspot counts">' +
                '<defs><clipPath id="tp-clip"><rect x="' + PL + '" y="' + (PT - 1) + '" width="' + pw + '" height="' + (ph + 2) + '"/></clipPath></defs>';
        for (var t = 0; t <= ymax + 1e-9; t += step) {
            s += '<line x1="' + PL + '" x2="' + (cw - PR) + '" y1="' + Y(t).toFixed(1) + '" y2="' + Y(t).toFixed(1) + '" stroke="' + GRID + '" stroke-width="0.6"/>' +
                 '<text x="' + (PL - 4) + '" y="' + (Y(t) + 3).toFixed(1) + '" text-anchor="end" font-size="9" fill="' + TXT + '">' + t + '</text>';
        }
        s += '<g clip-path="url(#tp-clip)">';
        var barw = Math.max(1, bw * 0.78);
        for (i = lo; i <= hi; i++) {
            var x = X(i) + (bw - barw) / 2;
            if (c[i] === null) { s += '<rect x="' + x.toFixed(1) + '" y="' + (PT + ph - 3) + '" width="' + barw.toFixed(1) + '" height="3" fill="#666"/>'; continue; }
            var h = ph * c[i] / ymax;
            s += '<rect x="' + x.toFixed(1) + '" y="' + (PT + ph - h).toFixed(1) + '" width="' + barw.toFixed(1) + '" height="' + Math.max(h, 0.8).toFixed(1) +
                 '" fill="' + (k[i] ? BAR : PARTIAL) + '"' + (k[i] ? '' : ' stroke="#fff" stroke-dasharray="2 2" stroke-width="0.7"') + '/>';
        }
        var pts = [];
        for (i = lo; i <= hi; i++) if (am[i] !== null) pts.push((X(i) + bw / 2).toFixed(1) + ',' + Y(am[i]).toFixed(1));
        if (pts.length > 1) s += '<polyline points="' + pts.join(' ') + '" fill="none" stroke="' + AVG + '" stroke-width="1.6" stroke-linejoin="round"/>';
        s += '<rect id="tp-band" x="0" y="' + PT + '" width="1" height="' + ph + '" fill="#ffffff" fill-opacity="0.13" visibility="hidden" pointer-events="none"/>';
        s += '</g>';
        var every = Math.max(1, Math.ceil(46 / bw));
        for (i = lo; i <= hi; i++) {
            var cx = X(i) + bw / 2;
            if (cx < PL - 2 || cx > cw - 2) continue;
            if (D.low[i] && c[i] !== null) s += '<circle cx="' + cx.toFixed(1) + '" cy="' + (PT + ph + 5) + '" r="2" fill="#ffd60a"/>';
            if (i % every) continue;
            var anchor = cx > cw - 22 ? 'end' : (cx < PL + 14 ? 'start' : 'middle');
            s += '<text x="' + Math.min(cx, cw - 1).toFixed(1) + '" y="' + (ch - 8) + '" text-anchor="' + anchor + '" font-size="9" fill="' + TXT + '">' + D.labels[i] + '</text>';
        }
        s += '<line id="tp-hov" x1="0" x2="0" y1="' + PT + '" y2="' + (PT + ph) + '" stroke="#cccccc" stroke-width="1" stroke-dasharray="3 2" visibility="hidden" pointer-events="none"/>' +
             '<line id="tp-sel" x1="0" x2="0" y1="' + PT + '" y2="' + (PT + ph) + '" stroke="#ffffff" stroke-width="1.6" visibility="hidden" pointer-events="none"/>' +
             '<text id="tp-tag" x="0" y="' + (PT + 9) + '" text-anchor="middle" font-size="10" font-weight="700" fill="#ffffff" stroke="#141414" stroke-width="3" ' +
             'paint-order="stroke" visibility="hidden" pointer-events="none"></text></svg>';
        box.innerHTML = s;
        marks();
    }
    function marks() {                         // the chosen day and the hovered day: only a few attributes change, so this is cheap
        var svg = box.firstChild;
        if (!svg) return;
        var pw = plotW(), bw = pw / view.w;
        function at(i) {                       // pixel x of the middle of day i, or null when it is outside the window
            if (i === null) return null;
            var x = PL + (i - view.a) * bw + bw / 2;
            return x >= PL - 0.5 && x <= PL + pw + 0.5 ? x : null;
        }
        function line(id, i) {
            var el = svg.querySelector('#' + id), x = at(i);
            if (x === null) { el.setAttribute('visibility', 'hidden'); return null; }
            el.setAttribute('x1', x); el.setAttribute('x2', x); el.setAttribute('visibility', 'visible');
            return x;
        }
        line('tp-hov', hov);
        var x = line('tp-sel', sel), band = svg.querySelector('#tp-band'), tag = svg.querySelector('#tp-tag');
        if (x === null) { band.setAttribute('visibility', 'hidden'); tag.setAttribute('visibility', 'hidden'); return; }
        var w = Math.max(bw, 3);
        band.setAttribute('x', x - w / 2); band.setAttribute('width', w); band.setAttribute('visibility', 'visible');
        tag.textContent = D.labels[sel];
        tag.setAttribute('x', Math.min(Math.max(x, PL + 22), cw - PR - 22)); tag.setAttribute('visibility', 'visible');
    }
    function dayAt(clientX) {
        var r = box.getBoundingClientRect();
        return Math.max(0, Math.min(N - 1, Math.floor(view.a + (clientX - r.left - PL) / plotW() * view.w)));
    }
    function setSel(i) { sel = i; read.textContent = tip(i); marks(); }

    // ---- the overview strip (the whole period, the visible part is the window) -----------------------------------------------------
    function drawNav() {
        var w = nav.clientWidth, h = 34;
        if (!w) return;
        var c = series().c, k = series().k, top = 1, i;
        for (i = 0; i < N; i++) if (c[i] !== null && c[i] > top) top = c[i];
        var s = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '"><rect x="0" y="0" width="' + w + '" height="' + h + '" rx="3" fill="#1b1b1b"/>', bw = w / N;
        for (i = 0; i < N; i++) {
            if (c[i] === null) continue;
            var bh = Math.max(0.8, (h - 8) * c[i] / top);
            s += '<rect x="' + (i * bw).toFixed(1) + '" y="' + (h - 3 - bh).toFixed(1) + '" width="' + Math.max(0.8, bw - 0.4).toFixed(1) + '" height="' + bh.toFixed(1) + '" fill="' + (k[i] ? '#9a5a14' : '#6b5a45') + '"/>';
        }
        var x0 = view.a / N * w, x1 = (view.a + view.w) / N * w;
        s += '<rect x="' + x0.toFixed(1) + '" y="1" width="' + (x1 - x0).toFixed(1) + '" height="' + (h - 2) + '" rx="3" fill="#ffffff" fill-opacity="0.16" stroke="#ffffff" stroke-width="1.2"/>' +
             '<rect x="' + (x0 - 2).toFixed(1) + '" y="' + (h / 2 - 8) + '" width="4" height="16" rx="2" fill="#ffffff"/>' +
             '<rect x="' + (x1 - 2).toFixed(1) + '" y="' + (h / 2 - 8) + '" width="4" height="16" rx="2" fill="#ffffff"/></svg>';
        nav.innerHTML = s;
    }
    function rangeText() {
        var ia = Math.max(0, Math.round(view.a)), ib = Math.min(N - 1, Math.round(view.a + view.w) - 1);
        rangeEl.textContent = D.labels[ia] + ' - ' + D.labels[ib] + ' · ' + (ib - ia + 1) + ' days';
        chips.forEach(function (b) { var d = +b.getAttribute('data-days'), want = d ? Math.min(N, d) : N; b.className = Math.abs(view.w - want) < 0.5 && Math.abs(view.a + view.w - N) < 0.5 ? 'on' : ''; });
    }
    function redraw() { clampView(); draw(); drawNav(); rangeText(); }

    var navDrag = null;
    nav.addEventListener('pointerdown', function (e) {
        var r = nav.getBoundingClientRect(), W = r.width, x = e.clientX - r.left, x0 = view.a / N * W, x1 = (view.a + view.w) / N * W, mode;
        if (Math.abs(x - x0) <= 14 && Math.abs(x - x0) <= Math.abs(x - x1)) mode = 'left';
        else if (Math.abs(x - x1) <= 14) mode = 'right';
        else if (x > x0 && x < x1) mode = 'move';
        else { view.a = x / W * N - view.w / 2; mode = 'move'; redraw(); }       // a tap outside the window centres it there
        navDrag = {mode: mode, x: e.clientX, a: view.a, w: view.w, W: W};
        nav.setPointerCapture(e.pointerId);
        e.preventDefault();
    });
    nav.addEventListener('pointermove', function (e) {
        if (!navDrag) return;
        var dd = (e.clientX - navDrag.x) / navDrag.W * N, end = navDrag.a + navDrag.w;
        if (navDrag.mode === 'move') view.a = navDrag.a + dd;
        else if (navDrag.mode === 'left') { var a = Math.max(0, Math.min(end - MIN_DAYS, navDrag.a + dd)); view.a = a; view.w = end - a; }
        else view.w = Math.max(MIN_DAYS, Math.min(N - navDrag.a, navDrag.w + dd));
        redraw();
    });
    function navEnd() { navDrag = null; }
    nav.addEventListener('pointerup', navEnd); nav.addEventListener('pointercancel', navEnd);

    // ---- the chart: drag to move the chosen day, pinch / wheel to zoom, two fingers / Shift+wheel to scroll --------------------------
    var ptrs = {}, pinch = null, scrubbing = false;
    function pair() { var ids = Object.keys(ptrs); return ids.length >= 2 ? [ptrs[ids[0]], ptrs[ids[1]]] : null; }
    box.addEventListener('pointerdown', function (e) {
        if (e.pointerType === 'mouse' && e.button !== 0) return;
        try { box.setPointerCapture(e.pointerId); } catch (err) { /* the pointer is already gone */ }
        ptrs[e.pointerId] = e.clientX;
        var two = pair();
        if (two) {
            var r = box.getBoundingClientRect(), mid = (two[0] + two[1]) / 2 - r.left - PL;
            pinch = {d: Math.max(24, Math.abs(two[0] - two[1])), w: view.w, day: view.a + mid / plotW() * view.w};
            scrubbing = false;
        } else { scrubbing = true; hov = null; setSel(dayAt(e.clientX)); }          // the line moves at once, no waiting for a click
    });
    box.addEventListener('pointermove', function (e) {
        if (ptrs[e.pointerId] !== undefined) ptrs[e.pointerId] = e.clientX;
        var two = pair();
        if (pinch && two) {
            var r = box.getBoundingClientRect(), mid = (two[0] + two[1]) / 2 - r.left - PL, w2 = pinch.w * pinch.d / Math.max(24, Math.abs(two[0] - two[1]));
            view.w = Math.max(MIN_DAYS, Math.min(N, w2)); view.a = pinch.day - mid / plotW() * view.w;
            redraw();
        } else if (scrubbing && ptrs[e.pointerId] !== undefined) setSel(dayAt(e.clientX));      // drag: the line follows the finger or mouse
        else if (e.pointerType === 'mouse') { hov = dayAt(e.clientX); read.textContent = tip(hov); marks(); }
    });
    function release(e) {
        delete ptrs[e.pointerId];
        if (!pair()) pinch = null;
        if (!Object.keys(ptrs).length) scrubbing = false;
    }
    box.addEventListener('pointerup', release);
    box.addEventListener('pointercancel', release);
    box.addEventListener('pointerleave', function (e) {
        if (e.pointerType !== 'mouse' || scrubbing) return;
        hov = null; read.textContent = sel === null ? hint : tip(sel); marks();
    });
    box.addEventListener('wheel', function (e) {
        e.preventDefault();
        var k = e.deltaMode === 1 ? 16 : 1, dx = (Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : (e.shiftKey ? e.deltaY : 0)) * k, pw = plotW();
        var r = box.getBoundingClientRect(), px = e.clientX - r.left - PL, day = view.a + px / pw * view.w;
        if (dx) view.a += dx / pw * view.w;                                       // scroll
        else { view.w *= Math.exp(e.deltaY * k * 0.0015); clampView(); view.a = day - px / pw * view.w; }      // zoom around the pointer
        redraw();
    }, {passive: false});
    box.addEventListener('keydown', function (e) {
        var d = e.key === 'ArrowLeft' ? -1 : (e.key === 'ArrowRight' ? 1 : 0);
        if (!d) return;
        e.preventDefault();
        if (e.shiftKey) { view.a += d * view.w * 0.2; redraw(); return; }
        var i = Math.max(0, Math.min(N - 1, (sel === null ? N : sel) + (sel === null ? -1 : d)));
        if (i < view.a) view.a = i; else if (i >= view.a + view.w) view.a = i - view.w + 1;
        redraw(); setSel(i);
    });
    chips.forEach(function (b) {
        b.addEventListener('click', function () {
            var d = +b.getAttribute('data-days');
            view.w = d ? Math.min(N, d) : N; view.a = N - view.w;
            redraw();
        });
    });

    // ---- showing the panel and switching place ----------------------------------------------------------------------------------------
    window.showTrend = function (open, id) {
        panel.classList.toggle('open', open);
        document.body.classList.toggle('trend-open', open);
        if (id) {
            place = id; pick.value = id;
            document.querySelectorAll('.tp-place').forEach(function (b) { b.hidden = b.getAttribute('data-place') !== id; });
            read.textContent = sel === null ? hint : tip(sel);                    // the same day, now for this place
        }
        if (open) redraw();
        if (window.onTrendPlace) window.onTrendPlace(open ? pick.value : null);     // the map draws that place heavier
    };
    pick.addEventListener('change', function () { window.showTrend(true, pick.value); });
    document.getElementById('trend-close').addEventListener('click', function () { window.showTrend(false); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') window.showTrend(false); });
    window.addEventListener('resize', function () { if (panel.classList.contains('open')) redraw(); });
})();
</script>
""".replace("%PHONE%", str(PHONE_MAX_WIDTH))


def _block(place, left, hidden):
    """The summary and the notes of one place (the chart itself is drawn by the script)."""
    rows = place["rows"]
    n = len(rows)
    have = [r for r in rows if r["count"] is not None]
    done = [r for r in have if r["complete"]]
    low = _low_coverage(done)
    last = rows[-1]
    latest = f"{last['count']}" + ("" if last["complete"] else " so far") if last["count"] is not None else "no data"
    summary = [f"Latest ({_label(last['date'])}): <b>{latest}</b>"]
    if done:
        peak = max(done, key=lambda r: r["count"])
        summary.append(f"mean <b>{sum(r['count'] for r in done) / len(done):.0f}</b>/day")
        summary.append(f"peak <b>{peak['count']}</b> ({_label(peak['date'])})")
    notes = []
    if len(have) < n:
        notes.append(f"Data for {len(have)} of {n} days" + (f"; {left} older days are fetched on the next run" if left else ""))
    if low:
        notes.append('<span style="color:#ffd60a">&#9679;</span> a satellite had far fewer granules that day')
    if n >= 7:
        notes.append('<span style="color:#4dd0e1">&#9472;</span> 7-day mean')
    pid = html.escape(place["id"], quote=True)
    flag = " hidden" if hidden else ""
    return (f'<div class="tp-place" data-place="{pid}"{flag} style="margin: 6px 0 0; color:#ddd;">{" &middot; ".join(summary)}</div>',
            f'<div class="tp-place" data-place="{pid}"{flag} style="color:#aaa; font-size: 9px; margin-top: 2px;">{" &nbsp; ".join(notes)}</div>')


def trend_panel(places, left=0):
    """places: [{id, name, group, rows}], rows = [{date, count (None = not in the cache), complete, granules}] oldest first.

    Returns the panel HTML (hidden at first; the first place is the one shown when it opens without a place)."""
    rows0 = places[0]["rows"]
    n = len(rows0)
    low = _low_coverage([r for r in rows0 if r["count"] is not None and r["complete"]])
    data = {
        "labels": [_label(r["date"]) for r in rows0],
        "years": [r["date"].year for r in rows0],
        "low": [1 if r["date"] in low else 0 for r in rows0],
        "gran": [r["granules"] for r in rows0],
        "order": [p["id"] for p in places],
        "places": {p["id"]: {"c": [r["count"] for r in p["rows"]], "k": [1 if r["complete"] else 0 for r in p["rows"]]} for p in places},
    }
    data_json = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    groups = {}
    for p in places:
        groups.setdefault(p.get("group", "Areas"), []).append(p)
    options = "".join(
        f'<optgroup label="{html.escape(g, quote=True)}">'
        + "".join(f'<option value="{html.escape(p["id"], quote=True)}">{html.escape(p["name"])}</option>' for p in ps)
        + "</optgroup>" for g, ps in groups.items())
    blocks = [_block(p, left, i > 0) for i, p in enumerate(places)]
    return f"""{STYLE}
    <div id="trend-panel" role="dialog" aria-label="Daytime hotspots per day">
        <div class="tp-head">
            <div style="min-width: 0;">
                <b style="font-size: 12px;">&#128293; Daytime hotspots per day, last {n} days</b><br>
                <select id="trend-place" aria-label="Place">{options}</select>
            </div>
            <button id="trend-close" type="button" aria-label="Close the hotspot chart">&times;</button>
        </div>
        {"".join(b[0] for b in blocks)}
        <div id="tp-ctl">
            <button type="button" data-days="7">7 d</button><button type="button" data-days="14">14 d</button><button type="button" data-days="30">30 d</button><button type="button" data-days="0" class="on">All</button>
            <span id="tp-range"></span>
        </div>
        <div id="trend-read">Press and drag on the chart to move along the days; pinch or use the wheel to zoom.</div>
        <div id="tp-chart" tabindex="0" aria-label="Daily counts: arrow keys move the chosen day, Shift and arrows scroll"></div>
        <div id="tp-nav" aria-label="Whole period: drag the window to scroll, drag its edges to zoom"></div>
        {"".join(b[1] for b in blocks)}
        <details style="color:#888; font-size: 9px; margin-top: 4px; line-height: 1.35;">
            <summary style="cursor: pointer;">About this chart</summary>
            Unique VIIRS daytime fire pixels (NOAA-20, NOAA-21, S-NPP; nominal/high confidence; persistent heat sources removed),
            repeats of one fire in other passes merged within 500 m. UTC days. Each place has its own scale, which follows the days
            on screen. Press and drag moves the chosen day; the wheel or a pinch zooms; Shift and wheel, two fingers, or the strip
            under the chart scroll. Cloud or thick smoke hides fires, so a drop can be cloud, not fewer fires.
        </details>
        <div style="color:#888; font-size: 9px; margin-top: 3px;">Outlines: Natural Earth (public domain), geoBoundaries (CC BY 4.0)</div>
    </div>
    <script type="application/json" id="trend-data">{data_json}</script>
    {SCRIPT}
    """
