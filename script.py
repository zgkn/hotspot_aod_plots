import os
import sys
import datetime
import re
import json
import shutil
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

# Suppress intrusive connection pool warning messages
logging.getLogger("urllib3").setLevel(logging.ERROR)

def install_deps():
    try:
        import boto3, xarray, netCDF4, folium, matplotlib, scipy, shapely
    except ImportError:
        print("[*] Installing missing dependencies...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q",
                               "boto3", "xarray", "netcdf4", "h5netcdf", "folium", "matplotlib", "scipy", "shapely"])
install_deps()

import numpy as np
import boto3
from botocore import UNSIGNED
from botocore.config import Config
import matplotlib.pyplot as plt
import folium

import aod_grid
import fire_trend
from period_panel import period_panel, period_script, MAX_DAYS as PERIOD_MAX_DAYS
from trend_panel import trend_panel, PHONE_MAX_WIDTH

# --- SHARED DOMAIN CONFIGURATIONS ---
SEA_LAT = [-11.0, 28.0]
SEA_LON = [90.0, 142.0]

SATELLITE_CONFIGS = {
    'NOAA-20': {
        'bucket': 'noaa-nesdis-n20-pds',
        'aod_prefix': 'VIIRS-JRR-AOD',
        'cloud_prefix': 'VIIRS-JRR-CloudMask'
    },
    'NOAA-21': {
        'bucket': 'noaa-nesdis-n21-pds',
        'aod_prefix': 'VIIRS-JRR-AOD',
        'cloud_prefix': 'VIIRS-JRR-CloudMask'
    }
}

# --- HOTSPOT TREND SETTINGS (days and area come from the workflow inputs) ---
CACHE_DIR = os.getenv("HOTSPOT_CACHE_DIR", "cache")     # per-day daytime fire pixels, kept between runs on the `data` branch
DEFAULT_DAYS = 14
DEFAULT_AREA = "all"

s3_client = boto3.client('s3', config=Config(signature_version=UNSIGNED, max_pool_connections=24,
                                             retries={'max_attempts': 8, 'mode': 'standard'}))

AOD_DAYS = 30                                            # days of AOD kept (the fire pixels are kept for longer)


def cmap_lut():
    """The 256 turbo colours of the AOD scale, for the page."""
    return (plt.get_cmap('turbo')(np.linspace(0, 1, 256))[:, :3] * 255).round().astype(int).tolist()


def encode_pixels(fires):
    """Fire pixels as integer arrays for the page (degrees x 1e4, FRP x 10, satellite index)."""
    return {"n": int(len(fires["lat"])),
            "lat": np.rint(fires["lat"].astype("float64") * 1e4).astype(int).tolist(),
            "lon": np.rint(fires["lon"].astype("float64") * 1e4).astype(int).tolist(),
            "frp": np.rint(fires["frp"].astype("float64") * 10).astype(int).tolist(),
            "sat": fires["sat"].astype(int).tolist()}


# --- OPEN THE TREND CHART WHEN THE AREA IS CLICKED ---
AREA_CLICK_JS = """
window.addEventListener('load', function () {
    var map = %s, places = %s, layers = %s, timer = null;
    var NORMAL = {weight: 1.6, fillOpacity: 0.08}, CHOSEN = {weight: 3, fillOpacity: 0.25};
    function inRing(x, y, ring) {
        var inside = false;
        for (var i = 0, j = ring.length - 1; i < ring.length; j = i++) {
            var xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
            if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) inside = !inside;
        }
        return inside;
    }
    function inPlace(p, lat, lng) {              // even-odd over the outline and its holes
        return p.polys.some(function (rings) {
            return rings.filter(function (r) { return inRing(lng, lat, r); }).length %% 2 === 1;
        });
    }
    function placeAt(latlng) {                   // the point itself first, then a few pixels around it (a phone moves a tap)
        var c = map.latLngToContainerPoint(latlng), tol = 12;
        var spots = [[0, 0], [tol, 0], [-tol, 0], [0, tol], [0, -tol]];
        for (var s = 0; s < spots.length; s++) {
            var ll = map.containerPointToLatLng([c.x + spots[s][0], c.y + spots[s][1]]);
            for (var k = 0; k < places.length; k++) if (inPlace(places[k], ll.lat, ll.lng)) return places[k].id;
        }
        return null;
    }
    window.onTrendPlace = function (chosen) {    // the place shown in the panel is drawn heavier, as in model_forecast
        Object.keys(layers).forEach(function (id) { layers[id].setStyle(id === chosen ? CHOSEN : NORMAL); });
    };
    map.on('click', function (e) {
        if (e.originalEvent && e.originalEvent.__hot) return;        // the click was on a hotspot: its popup, not the chart
        var id = placeAt(e.latlng);
        clearTimeout(timer);
        if (id) timer = setTimeout(function () { showTrend(true, id); }, 250);      // not on the first tap of a double-tap zoom
    });
    map.on('dblclick', function () { clearTimeout(timer); });
    // layer menu on phones: the icon opens and closes it, a tap on the map closes it (the CSS hides the list while it is closed)
    var box = document.querySelector('.leaflet-control-layers');
    if (box) {
        var icon = box.querySelector('.leaflet-control-layers-toggle');
        if (icon) icon.addEventListener('click', function (e) { e.preventDefault(); box.classList.toggle('lc-open'); });
        map.on('click', function () { box.classList.remove('lc-open'); });
    }
});
"""

# --- DAYS AND AREA OF THE TREND ---
def get_selection():
    """Days and area: the workflow inputs when given, else the selection saved by the last run that had inputs, else the defaults."""
    days_in = os.getenv("HOTSPOT_DAYS", "").strip()
    area_in = os.getenv("HOTSPOT_AREA", "").strip()
    saved = {}
    selection_file = os.path.join(CACHE_DIR, "selection.json")
    try:
        with open(selection_file) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        pass
    try:
        days = int(days_in) if days_in else int(saved.get("days", DEFAULT_DAYS))
    except ValueError:
        raise SystemExit(f"[X] days must be a whole number from 1 to {fire_trend.MAX_DAYS}, got {days_in!r}")
    if not 1 <= days <= fire_trend.MAX_DAYS:
        raise SystemExit(f"[X] days must be from 1 to {fire_trend.MAX_DAYS}, got {days}")
    area = area_in or saved.get("area", DEFAULT_AREA)
    if (days_in or area_in) and os.getenv("HOTSPOT_SAVE_SELECTION") == "true":
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(selection_file, "w") as f:
            json.dump({"days": days, "area": area}, f)
    return days, area

# --- EXECUTION TARGET PIPELINE ---
def main():
    sgt_tz = datetime.timezone(datetime.timedelta(hours=8))
    local_now = datetime.datetime.now(sgt_tz)

    night_date = fire_trend.latest_night_day()           # UTC day of the newest night passes (15-20 UTC)
    night_partial = fire_trend.utc_now() < datetime.datetime.combine(night_date, datetime.time(22))     # last passes still being posted

    # IMPORTANT: Create output directory for GitHub Artifacts
    os.makedirs("output", exist_ok=True)

    print(f"[*] Combined Core Initiated | SGT Now: {local_now.strftime('%Y-%m-%d %H:%M:%S')}")

    domain = (SEA_LAT[0], SEA_LON[0], SEA_LAT[1], SEA_LON[1])
    days, area_text = get_selection()
    try:
        areas = fire_trend.parse_areas(area_text, domain)
    except ValueError as e:
        raise SystemExit(f"[X] {e}")
    end_day = fire_trend.latest_day()
    dates = fire_trend.day_list(end_day, days)
    print(f"[*] Hotspot trend: {days} days up to {end_day} (UTC) | areas: {', '.join(a.name for a in areas)}")

    m = folium.Map(location=[8.5, 116.0], zoom_start=5, tiles=None)

    # Basemap 1: Esri Satellite Imagery
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri",
        name="Esri Satellite Imagery"
    ).add_to(m)

    # Basemap 2: OpenStreetMap
    folium.TileLayer(
        tiles="OpenStreetMap",
        name="OpenStreetMap (Labels & Areas)",
        show=False
    ).add_to(m)

    # Country Boundaries Overlay
    boundary_style = {'color': '#ffffff', 'weight': 1.5, 'fillOpacity': 0}
    folium.GeoJson(
        "https://raw.githubusercontent.com/python-visualization/folium/master/examples/data/world-countries.json",
        name="Country Boundaries",
        style_function=lambda x: boundary_style
    ).add_to(m)

    feature_groups = {
        "Hotspots_Night": folium.FeatureGroup(name=f"🌌 Night hotspots ({night_date} UTC{', still being posted' if night_partial else ''})", show=True),
        "Hotspots_Day": folium.FeatureGroup(name="🔥 Daytime hotspots", show=True),
        "AOD_Today": folium.FeatureGroup(name="💨 Aerosol AOD", show=True),
        "Cloud": folium.FeatureGroup(name="☁️ Cloud mask (cloud fraction)", show=False),
        "Area": folium.FeatureGroup(name="Trend areas", control=False, show=True),    # always on: not in the layer menu
    }

    # --- Daytime fire pixels: refresh the per-day cache, newest day first ---
    budget_s = float(os.getenv("HOTSPOT_BUDGET_MIN", "60")) * 60
    fetched, left = fire_trend.refresh(CACHE_DIR, dates, s3_client, SATELLITE_CONFIGS, domain, budget_s=budget_s, workers=12)
    if days > 1 and fire_trend.load_day(CACHE_DIR, end_day) is None:
        # the passes of the newest day are not posted yet (its window opens at 03 UTC): show the day before instead of an empty one
        end_day -= datetime.timedelta(days=1)
        dates = fire_trend.day_list(end_day, days)
        print(f"[*] No EFIRE granules for {end_day + datetime.timedelta(days=1)} yet: the trend ends on {end_day}")
        more, left = fire_trend.refresh(CACHE_DIR, dates, s3_client, SATELLITE_CONFIGS, domain, budget_s=budget_s, workers=12)
        fetched += more
    fire_trend.prune(CACHE_DIR, end_day)
    print(f"[*] Hotspot cache: {len(fetched)} days fetched, {len(left)} left for a later run (time budget {budget_s / 60:.0f} min)")

    # --- AOD: one grid per UTC day in the cache (a day is downloaded once, newest first, within a time budget) ---
    aod_end = fire_trend.utc_now().date()
    aod_dates = fire_trend.day_list(aod_end, AOD_DAYS)
    aod_budget_s = float(os.getenv("HOTSPOT_AOD_BUDGET_MIN", "35")) * 60
    aod_fetched, aod_left = aod_grid.refresh(CACHE_DIR, aod_dates, s3_client, SATELLITE_CONFIGS, domain, budget_s=aod_budget_s, workers=12)
    aod_grid.prune(CACHE_DIR, aod_end)
    aod_days = aod_grid.good_days(CACHE_DIR, aod_dates)
    print(f"[*] AOD cache: {len(aod_fetched)} days fetched, {len(aod_left)} left for a later run (time budget {aod_budget_s / 60:.0f} min); "
          f"{len(aod_days)} days to show, newest {aod_days[-1] if aod_days else 'none'}")

    # --- cloud mask: the same, one grid per UTC day (heavier: about 22 GB of granules a day, so its own time budget; it is optional,
    #     a failure here leaves the layer empty and nothing else) ---
    cloud_days = []
    try:
        cloud_budget_s = float(os.getenv("HOTSPOT_CLOUD_BUDGET_MIN", "40")) * 60
        cloud_fetched, cloud_left = aod_grid.refresh(CACHE_DIR, aod_dates, s3_client, SATELLITE_CONFIGS, domain,
                                                     budget_s=cloud_budget_s, workers=12, kind="cloud")
        aod_grid.prune(CACHE_DIR, aod_end, kind="cloud")
        cloud_days = aod_grid.good_days(CACHE_DIR, aod_dates, kind="cloud")
        print(f"[*] Cloud mask cache: {len(cloud_fetched)} days fetched, {len(cloud_left)} left for a later run "
              f"(time budget {cloud_budget_s / 60:.0f} min); {len(cloud_days)} days to show, newest {cloud_days[-1] if cloud_days else 'none'}")
    except Exception as e:
        print(f"[!] Cloud mask unavailable this run: {e}")

    # --- the day files of the page: fire pixels of the last days of the trend, AOD and cloud grids of the days to show ---
    data_dir = os.path.join("output", "data")
    shutil.rmtree(data_dir, ignore_errors=True)
    fire_days, fire_partial = [], []
    for d in dates[-PERIOD_MAX_DAYS:]:                     # a period is limited to PERIOD_MAX_DAYS days
        info = fire_trend.export_day(CACHE_DIR, d, data_dir)
        if info is not None:
            fire_days.append(d.isoformat())
            if not info["complete"]:
                fire_partial.append(d.isoformat())
    aod_shown = [d.isoformat() for d in aod_days if aod_grid.export_day(CACHE_DIR, d, data_dir)]
    cloud_shown = [d.isoformat() for d in cloud_days if aod_grid.export_day(CACHE_DIR, d, data_dir, kind="cloud")]
    print(f"[*] Day files: {len(fire_days)} days of hotspots, {len(aod_shown)} days of AOD, {len(cloud_shown)} days of cloud mask")

    # --- the newest night is drawn from data in the page (fetched now, not cached) ---
    night_pixels = {"n": 0, "lat": [], "lon": [], "frp": [], "sat": []}
    try:
        night, _ = fire_trend.fetch_day(s3_client, SATELLITE_CONFIGS, night_date, domain, workers=12,
                                        hours=fire_trend.NIGHT_HOURS, daytime=False)
        night_pixels = encode_pixels(night)
    except Exception as e:
        print(f"[!] Night hotspots for {night_date} unavailable: {e}")
    print(f"[*] Night hotspot markers for {night_date}: {night_pixels['n']}")
    day_now = fire_trend.load_day(CACHE_DIR, end_day)
    print(f"[*] Daytime hotspots of {end_day}: {0 if day_now is None else len(day_now['lat'])}")

    # --- Trend of daily unique daytime hotspots in the chosen places ---
    series = fire_trend.build_series(CACHE_DIR, dates, areas)
    print(f"\n[*] Daily unique daytime hotspots (one column per place):")
    print("    date        " + " ".join(f"{a.name[:12]:>12}" for a in areas) + "  state")
    for i, d in enumerate(dates):
        first = series[areas[0].id][i]
        cells = " ".join(f"{'-' if series[a.id][i]['count'] is None else series[a.id][i]['count']:>12}" for a in areas)
        state = "no data" if first["count"] is None else ("complete" if first["complete"] else "partial")
        print(f"    {d}  {cells}  {state}  granules={first['granules']}")
    places = [{"id": a.id, "name": a.name, "group": a.group, "rows": series[a.id]} for a in areas]
    m.get_root().html.add_child(folium.Element(trend_panel(places, left=len(left))))
    # Every place is outlined (a white casing under a dark line, as in model_forecast) and a click or tap inside one opens its chart;
    # the panel is hidden until then. The map itself checks which place the point is in, with a few pixels of tolerance: a phone
    # moves a tap to the nearest line or marker, so listening on the outlines alone misses taps near borders. Markers do not pass
    # their clicks on, so tapping a hotspot only shows its popup.
    layer_names = {}
    for a in areas:
        folium.GeoJson(a.geojson(), style_function=lambda x: {"color": "#ffffff", "weight": 4.5, "opacity": 0.85, "fill": False}
                       ).add_to(feature_groups["Area"])
    for a in areas:
        layer = folium.GeoJson(a.geojson(), style_function=lambda x: {"color": "#1a1a19", "weight": 1.6, "fillColor": "#ffffff", "fillOpacity": 0.08})
        layer.add_to(feature_groups["Area"])
        layer_names[a.id] = layer.get_name()
    period_info = {"domain": list(domain), "nx": aod_grid.grid_shape(domain)[1], "ny": aod_grid.grid_shape(domain)[0],
                   "aodStep": aod_grid.AOD_Q, "cmap": cmap_lut(), "sats": list(fire_trend.SAT_ORDER),
                   "fires": fire_days, "partial": fire_partial, "aod": aod_shown, "cloud": cloud_shown,
                   "cloudLevels": aod_grid.KINDS["cloud"]["levels"],
                   "latestFire": end_day.isoformat(), "latestAod": aod_shown[-1] if aod_shown else None,
                   "night": {"date": night_date.isoformat(), **night_pixels}}
    m.get_root().html.add_child(folium.Element(period_panel(period_info)))
    m.get_root().script.add_child(folium.Element(period_script(
        m.get_name(), feature_groups["Hotspots_Day"].get_name(), feature_groups["Hotspots_Night"].get_name(),
        feature_groups["AOD_Today"].get_name(), feature_groups["Cloud"].get_name())))
    layers_js = "{" + ", ".join(f"{json.dumps(i)}: {v}" for i, v in layer_names.items()) + "}"
    places_js = json.dumps([{"id": a.id, "polys": a.polygons()} for a in areas])
    m.get_root().script.add_child(folium.Element(AREA_CLICK_JS % (m.get_name(), places_js, layers_js)))

    # Layer stacking
    feature_groups["AOD_Today"].add_to(m)
    feature_groups["Cloud"].add_to(m)
    feature_groups["Area"].add_to(m)
    feature_groups["Hotspots_Day"].add_to(m)
    feature_groups["Hotspots_Night"].add_to(m)          # on top of the daytime circles, so the night pixels are not hidden by them

    composite_legend = f"""
    <style>
    #map-legend {{ position: fixed; bottom: 35px; left: 25px; width: 235px; box-sizing: border-box;
                background-color: rgba(20, 20, 20, 0.88); color: #ffffff; z-index: 9999;
                font-size: 11px; font-family: Arial, sans-serif; border: 1px solid #444;
                padding: 12px; border-radius: 6px; box-shadow: 0 0 15px rgba(0,0,0,0.6); }}
    @media (max-width: {PHONE_MAX_WIDTH}px) {{
        #map-legend {{ left: 8px; bottom: max(8px, env(safe-area-inset-bottom)); width: 188px; padding: 7px 9px; font-size: 10px; }}
        #map-legend .lg-title {{ font-size: 11px !important; margin-bottom: 3px !important; }}
        #map-legend .lg-frp {{ line-height: 14px !important; margin-bottom: 4px !important; }}
        #map-legend .lg-frp br {{ display: none; }}
        #map-legend .lg-item {{ display: inline-block; margin-right: 6px; white-space: nowrap; }}
        #map-legend hr {{ margin: 4px 0 !important; }}
        body.trend-open #map-legend {{ display: none; }}
        /* the layer menu is a small icon until it is tapped (it covered a large part of the map) */
        .leaflet-control-layers-expanded {{ max-width: 78vw; max-height: 50vh; overflow: auto; font-size: 12px; }}
        .leaflet-control-layers-expanded .leaflet-control-layers-toggle {{ display: block !important; }}
        .leaflet-control-layers:not(.lc-open) {{ padding: 0 !important; }}
        .leaflet-control-layers:not(.lc-open) .leaflet-control-layers-list {{ display: none !important; }}
    }}
    </style>
    <div id="map-legend">
        <b class="lg-title" style="font-size: 12px; color: #fff; display: block; margin-bottom: 5px;">🔥 Fire Intensity (FRP)</b>
        <div class="lg-frp" style="line-height: 16px; margin-bottom: 8px;">
            <span class="lg-item"><span style="display: inline-block; width: 6px; height: 6px; background: #FFD700; border-radius: 50%; margin-right: 5px;"></span> Low (&lt;10 MW)</span><br>
            <span class="lg-item"><span style="display: inline-block; width: 10px; height: 10px; background: #FF8C00; border-radius: 50%; margin-right: 5px;"></span> Mid (10-50 MW)</span><br>
            <span class="lg-item"><span style="display: inline-block; width: 14px; height: 14px; background: #FF4500; border-radius: 50%; margin-right: 5px;"></span> High (&gt;50 MW)</span>
        </div>
        <hr style="border: 0; border-top: 1px solid #333; margin: 6px 0;">
        <b class="lg-title" style="font-size: 12px; color: #fff; display: block; margin-bottom: 5px;">💨 Aerosol Loading (<span id="aod-title">AOD</span>)</b>
        <div style="display: inline-block; width: 100%; height: 12px; background: linear-gradient(to right, #440154, #3b4cc0, #1fa0d0, #5fd35f, #ffea46, #fd1d24); border-radius: 2px; opacity: 0.8;"></div>
        <div style="width: 100%; display: flex; justify-content: space-between; padding-top: 2px; font-size: 9px; color: #aaa;">
            <span>0.0 (Clear)</span>
            <span id="aod-max">5.0+ (Dense Plume)</span>
        </div>
        <div id="lg-cloud" style="display: none;">
            <hr style="border: 0; border-top: 1px solid #333; margin: 6px 0;">
            <b class="lg-title" style="font-size: 12px; color: #fff; display: block; margin-bottom: 5px;">☁️ Cloud fraction (<span id="cloud-title">cloud</span>)</b>
            <div style="height: 12px; border: 1px solid #666; border-radius: 2px; background: linear-gradient(to right, #202020, rgba(238,243,250,0.85));"></div>
            <div style="width: 100%; display: flex; justify-content: space-between; padding-top: 2px; font-size: 9px; color: #aaa;">
                <span>0 (clear)</span>
                <span>1 (cloudy)</span>
            </div>
        </div>
        <hr style="border: 0; border-top: 1px solid #333; margin: 6px 0;">
        <div style="font-size: 9px; color: #4dd0e1;">Click or tap an outlined area for its hotspot chart</div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(composite_legend))
    folium.LayerControl(collapsed=False).add_to(m)

    # Save an index.html copy for GitHub Pages deployment
    m.save(os.path.join("output", "index.html"))

    print("\n[!] SUCCESS: Target structural mapping complete. Saved to 'output/index.html'")

if __name__ == "__main__":
    main()
