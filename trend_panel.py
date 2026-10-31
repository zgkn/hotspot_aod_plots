"""The daily hotspot trend as a small dark panel with an inline SVG bar chart (nothing to load, works offline)."""
import html
import math
from statistics import median

BAR, PARTIAL, AVG, GRID, TXT = "#FF8C00", "#FFC27A", "#4dd0e1", "#444", "#bbb"
W, H = 436, 176
PL, PR, PT, PB = 38, 6, 8, 26
AVG_DAYS = 7
LOW_COVERAGE = 0.6                  # a satellite with fewer granules than this share of its usual number is flagged


def _step(top, ticks=5):
    """Smallest tick step of the form 1, 2 or 5 x 10^k that covers `top` with at most `ticks` steps."""
    k = 0
    while True:
        for m in (1, 2, 5):
            if math.ceil(top / (m * 10 ** k)) <= ticks:
                return m * 10 ** k
        k += 1


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


def trend_panel(rows, area_name, left=0):
    """rows: [{date, count (None = not in the cache), complete, granules}] oldest first. Returns the panel HTML."""
    n = len(rows)
    have = [r for r in rows if r["count"] is not None]
    done = [r for r in have if r["complete"]]
    top = max([r["count"] for r in have] + [1])
    step = _step(top)
    ymax = step * math.ceil(top / step)
    pw, ph = W - PL - PR, H - PT - PB
    bw = pw / n
    barw = max(1.0, bw * 0.78)
    low = _low_coverage(done)

    def X(i):
        return PL + i * bw

    def Y(v):
        return PT + ph * (1 - v / ymax)

    svg = [f'<svg viewBox="0 0 {W} {H}" width="100%" style="display:block" role="img" aria-label="Daily daytime hotspot counts">']
    t = 0.0
    while t <= ymax + 1e-9:
        svg.append(f'<line x1="{PL}" x2="{W - PR}" y1="{Y(t):.1f}" y2="{Y(t):.1f}" stroke="{GRID}" stroke-width="0.6"/>'
                   f'<text x="{PL - 4}" y="{Y(t) + 3:.1f}" text-anchor="end" font-size="9" fill="{TXT}">{t:g}</text>')
        t += step
    for i, r in enumerate(rows):
        x = X(i) + (bw - barw) / 2
        tip = f"{_label(r['date'])} {r['date'].year}: "
        if r["count"] is None:
            svg.append(f'<rect x="{x:.1f}" y="{PT + ph - 3:.1f}" width="{barw:.1f}" height="3" fill="#666"/>')
            tip += "no data"
        else:
            tip += f"{r['count']} unique daytime hotspots"
            if not r["complete"]:
                tip += " (partial: passes of this day are still being posted)"
            if r["date"] in low:
                tip += " (a satellite had far fewer granules than usual)"
            if r["granules"]:
                tip += " | granules " + ", ".join(f"{s} {g}" for s, g in r["granules"].items())
            h = ph * r["count"] / ymax
            fill = BAR if r["complete"] else PARTIAL
            extra = "" if r["complete"] else ' stroke="#fff" stroke-dasharray="2 2" stroke-width="0.7"'
            svg.append(f'<rect x="{x:.1f}" y="{PT + ph - h:.1f}" width="{barw:.1f}" height="{max(h, 0.8):.1f}" fill="{fill}"{extra}/>')
        if r["date"] in low and r["count"] is not None:
            svg.append(f'<circle cx="{X(i) + bw / 2:.1f}" cy="{PT + ph + 5}" r="2" fill="#ffd60a"/>')
        svg.append(f'<rect x="{X(i):.1f}" y="{PT}" width="{bw:.1f}" height="{ph}" fill="transparent"><title>{html.escape(tip)}</title></rect>')
    if n >= AVG_DAYS:                                    # trailing 7-day mean of the complete days
        pts = []
        for i in range(n):
            win = [r["count"] for r in rows[max(0, i - AVG_DAYS + 1):i + 1] if r["count"] is not None and r["complete"]]
            if len(win) >= 4:
                pts.append(f"{X(i) + bw / 2:.1f},{Y(sum(win) / len(win)):.1f}")
        if len(pts) > 1:
            svg.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{AVG}" stroke-width="1.6" stroke-linejoin="round"/>')
    every = max(1, math.ceil(46 / bw))
    for i in range(n - 1, -1, -every):                   # labels counted back from the latest day
        cx = X(i) + bw / 2
        anchor = "end" if cx > W - 22 else ("start" if cx < PL + 14 else "middle")      # keep the first and last label inside
        cx = min(cx, W - 1) if anchor == "end" else cx
        svg.append(f'<text x="{cx:.1f}" y="{H - 8}" text-anchor="{anchor}" font-size="9" fill="{TXT}">{_label(rows[i]["date"])}</text>')
    svg.append("</svg>")

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
    if n >= AVG_DAYS:
        notes.append(f'<span style="color:{AVG}">&#9472;</span> 7-day mean')
    return f"""
    <div id="trend-panel" style="position: fixed; bottom: 35px; right: 12px; width: 460px; max-width: calc(100vw - 24px);
                background-color: rgba(20, 20, 20, 0.9); color: #fff; z-index: 9999; font-size: 11px; font-family: Arial, sans-serif;
                border: 1px solid #444; padding: 10px 12px; border-radius: 6px; box-shadow: 0 0 15px rgba(0,0,0,0.6); box-sizing: border-box;">
        <div style="cursor: pointer; display: flex; justify-content: space-between; align-items: baseline;"
             onclick="var b=document.getElementById('trend-body');b.style.display=b.style.display==='none'?'block':'none';">
            <b style="font-size: 12px;">&#128293; Daytime hotspots per day, last {n} days</b><span style="color:#aaa">&#9662;</span>
        </div>
        <div style="color:#ddd; margin-top: 2px;">{html.escape(area_name)}</div>
        <div id="trend-body">
            <div style="margin: 6px 0 4px; color:#ddd;">{" &middot; ".join(summary)}</div>
            {"".join(svg)}
            <div style="color:#aaa; font-size: 9px; margin-top: 2px;">{" &nbsp; ".join(notes)}</div>
            <div style="color:#888; font-size: 9px; margin-top: 4px; line-height: 1.35;">
                Unique VIIRS daytime fire pixels (NOAA-20, NOAA-21, S-NPP; nominal/high confidence; persistent heat sources removed),
                repeats of one fire in other passes merged within 500 m. UTC days. Cloud or thick smoke hides fires, so a drop can be
                cloud, not fewer fires. Outlines: Natural Earth (public domain), geoBoundaries (CC BY 4.0).
            </div>
        </div>
    </div>
    <script>if (window.innerWidth < 800) document.getElementById('trend-body').style.display = 'none';</script>
    """
