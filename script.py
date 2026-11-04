import os
import sys
import datetime
import re
import gc
import json
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
import xarray as xr
import boto3
from botocore import UNSIGNED
from botocore.config import Config
import matplotlib.pyplot as plt
import folium

import fire_trend
from trend_panel import trend_panel, PHONE_MAX_WIDTH

# --- SHARED DOMAIN CONFIGURATIONS ---
SEA_LAT = [-11.0, 28.0]
SEA_LON = [90.0, 142.0]

SATELLITE_CONFIGS = {
    'NOAA-20': {
        'bucket': 'noaa-nesdis-n20-pds',
        'aod_prefix': 'VIIRS-JRR-AOD'
    },
    'NOAA-21': {
        'bucket': 'noaa-nesdis-n21-pds',
        'aod_prefix': 'VIIRS-JRR-AOD'
    }
}

# --- HOTSPOT TREND SETTINGS (days and area come from the workflow inputs) ---
CACHE_DIR = os.getenv("HOTSPOT_CACHE_DIR", "cache")     # per-day daytime fire pixels, kept between runs on the `data` branch
DEFAULT_DAYS = 14
DEFAULT_AREA = "all"

s3_client = boto3.client('s3', config=Config(signature_version=UNSIGNED, max_pool_connections=24,
                                             retries={'max_attempts': 8, 'mode': 'standard'}))

# --- S3 ATOMIC TRANSACTIONS ---
def download_worker(bucket, key, local_path):
    try:
        if not os.path.exists(local_path):
            s3_client.download_file(bucket, key, local_path)
        return local_path
    except Exception as e:
        print(f"[!] Fail transfer: {os.path.basename(key)}: {e}")
        return None

# --- ENGINE LOGIC FOR RASTER AOD ---
def scrape_aod(sat_name, config, date_obj, label):
    prefix = f"{config['aod_prefix']}/{date_obj.strftime('%Y/%m/%d')}/"
    tasks = []
    start_win = datetime.datetime(date_obj.year, date_obj.month, date_obj.day, 2, 30, tzinfo=datetime.timezone.utc)
    end_win = datetime.datetime(date_obj.year, date_obj.month, date_obj.day, 9, 30, tzinfo=datetime.timezone.utc)
    try:
        for page in s3_client.get_paginator('list_objects_v2').paginate(Bucket=config['bucket'], Prefix=prefix):
            for obj in page.get('Contents', []):
                match = re.search(r'_s(\d{14})', obj['Key'])
                if match:
                    timestamp = datetime.datetime.strptime(match.group(1), '%Y%m%d%H%M%S').replace(tzinfo=datetime.timezone.utc)
                    if start_win <= timestamp <= end_win:
                        local_fn = f"aod_{label}_{sat_name}_{os.path.basename(obj['Key'])}"
                        tasks.append((config['bucket'], obj['Key'], local_fn))
    except Exception as e:
        print(f"[!] Warning: Unable to scrape AOD for {sat_name} on {date_obj.date()}: {e}")
    return tasks

# --- RASTERIZATION ENGINE ---
def rasterize_aod(lats, lons, aods, max_aod=None):
    if max_aod is None:
        max_aod = np.ceil(np.max(aods))

    res = 500
    rgba = np.zeros((res, res, 4), dtype=np.uint8)
    norm_y = ((res-1) - ((lats - SEA_LAT[0]) / (SEA_LAT[1] - SEA_LAT[0]) * (res-1))).astype(int)
    norm_x = ((lons - SEA_LON[0]) / (SEA_LON[1] - SEA_LON[0]) * (res-1)).astype(int)
    mask = (norm_x >= 0) & (norm_x < res) & (norm_y >= 0) & (norm_y < res)

    cmap = plt.get_cmap('turbo')
    colors = (cmap(np.clip(aods[mask], 0, max_aod) / max_aod) * 255).astype(np.uint8)
    colors[:, 3] = 175
    rgba[norm_y[mask], norm_x[mask]] = colors
    return rgba, max_aod

NIGHT_COLOR = "#8e5cff"

# --- HOTSPOT MARKERS ---
def draw_hotspots(fires, group, night=False):
    """One circle marker per unique fire pixel. Daytime: coloured and sized by FRP. Night: a smaller violet dot with a white rim,
    drawn on top of the daytime circles (most night pixels lie where the day ones are, so the same style would be hidden by them).
    Markers do not pass their clicks on to the map."""
    for lat, lon, p, sat in zip(fires["lat"], fires["lon"], fires["frp"], fires["sat"]):
        color = NIGHT_COLOR if night else ('#FFD700' if p < 10 else ('#FF8C00' if p < 50 else '#FF4500'))
        popup_html = (f"<b>{'🌌 Night pass' if night else '🛰️'} {fire_trend.SAT_ORDER[int(sat)]}</b><br>Lat: {lat:.3f}<br>Lon: {lon:.3f}"
                      f"<br>FRP: <b>{p:.1f} MW</b>")
        folium.CircleMarker(
            location=[float(lat), float(lon)],
            radius=float(np.clip(p / 12.0, 3.5, 8)) if night else float(np.clip(p / 6.0, 6, 14)),
            color="#ffffff" if night else color,
            weight=1.5 if night else 2,
            fill=True,
            fill_color=color,
            fill_opacity=0.95 if night else 0.85,
            bubbling_mouse_events=False,
            popup=folium.Popup(popup_html, max_width=150)
        ).add_to(group)
    return len(fires["lat"])

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

    today_date = local_now.date()
    yesterday_date = today_date - datetime.timedelta(days=1)

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
        "Hotspots_Night": folium.FeatureGroup(name=f"🌌 Night hotspots ({yesterday_date} UTC)", show=True),
        "Hotspots_Day": folium.FeatureGroup(name=f"🔥 Daytime hotspots ({end_day} UTC)", show=True),
        "AOD_Today": folium.FeatureGroup(name=f"💨 Aerosol AOD ({today_date})", show=True),
        "Area": folium.FeatureGroup(name="Trend areas", control=False, show=True),    # always on: not in the layer menu
    }

    # --- Daytime fire pixels: refresh the per-day cache, newest day first ---
    budget_s = float(os.getenv("HOTSPOT_BUDGET_MIN", "60")) * 60
    fetched, left = fire_trend.refresh(CACHE_DIR, dates, s3_client, SATELLITE_CONFIGS, domain, budget_s=budget_s, workers=12)
    fire_trend.prune(CACHE_DIR, end_day)
    print(f"[*] Hotspot cache: {len(fetched)} days fetched, {len(left)} left for a later run (time budget {budget_s / 60:.0f} min)")

    # --- AOD downloads: today's passes, or the latest earlier day that has any (before 03 UTC today's have not happened yet) ---
    download_manifest, aod_date = [], today_date
    for back in range(4):
        aod_date = today_date - datetime.timedelta(days=back)
        for sat, config in SATELLITE_CONFIGS.items():
            download_manifest.extend(scrape_aod(sat, config, aod_date, "TODAY_AOD"))
        if download_manifest:
            break
    feature_groups["AOD_Today"].layer_name = f"💨 Aerosol AOD ({aod_date})"

    print(f"[*] Found {len(download_manifest)} AOD files matching current window criteria (day {aod_date}).")

    downloaded_paths = []
    if download_manifest:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(download_worker, b, k, lf) for b, k, lf in download_manifest]
            for fut in as_completed(futures):
                res_path = fut.result()
                if res_path:
                    downloaded_paths.append(res_path)

    print(f"[*] Downloaded {len(downloaded_paths)}/{len(download_manifest)} target arrays cleanly.")

    print("\n[*] Processing local footprints into vector entities...")
    aod_lats, aod_lons, aod_vals = [], [], []

    for path in downloaded_paths:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            print(f"[!] Skipping empty or missing file: {path}")
            continue

        try:
            if "aod" in path:
                with xr.open_dataset(path, engine="netcdf4") as ds:
                    if 'AOD550' in ds and 'Latitude' in ds and 'Longitude' in ds:
                        a = ds['AOD550'].values[::5, ::5].flatten()
                        l = ds['Latitude'].values[::5, ::5].flatten()
                        ln = ds['Longitude'].values[::5, ::5].flatten()

                        mask = (l >= SEA_LAT[0]) & (l <= SEA_LAT[1]) & \
                               (ln >= SEA_LON[0]) & (ln <= SEA_LON[1]) & (~np.isnan(a))
                        aod_lats.extend(l[mask])
                        aod_lons.extend(ln[mask])
                        aod_vals.extend(a[mask])

        except Exception as e:
            print(f"[!] Processing structural fault on file {os.path.basename(path)}: {e}")

    aod_max_scale = 5.0
    if aod_vals:
        print(f"[*] Mapping gridded AOD surface overlay: {len(aod_vals)} data arrays.")
        img_layer, aod_max_scale = rasterize_aod(np.array(aod_lats), np.array(aod_lons), np.array(aod_vals))
        folium.raster_layers.ImageOverlay(
            img_layer,
            bounds=[[SEA_LAT[0], SEA_LON[0]], [SEA_LAT[1], SEA_LON[1]]],
            origin='upper',
            interactive=False
        ).add_to(feature_groups["AOD_Today"])

    # --- Hotspot markers: the latest day (from the cache) and the night before (fetched now, map only) ---
    latest = fire_trend.load_day(CACHE_DIR, end_day)
    day_drawn = draw_hotspots(latest, feature_groups["Hotspots_Day"]) if latest is not None else 0
    print(f"[*] Daytime hotspot markers drawn for {end_day}: {day_drawn}")
    night_drawn = 0
    try:
        night, _ = fire_trend.fetch_day(s3_client, SATELLITE_CONFIGS, yesterday_date, domain, workers=12,
                                        hours=fire_trend.NIGHT_HOURS, daytime=False)
        night_drawn = draw_hotspots(night, feature_groups["Hotspots_Night"], night=True)
    except Exception as e:
        print(f"[!] Night hotspots for {yesterday_date} unavailable: {e}")
    print(f"[*] Night hotspot markers drawn for {yesterday_date}: {night_drawn}")

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
    layers_js = "{" + ", ".join(f"{json.dumps(i)}: {v}" for i, v in layer_names.items()) + "}"
    places_js = json.dumps([{"id": a.id, "polys": a.polygons()} for a in areas])
    m.get_root().script.add_child(folium.Element(AREA_CLICK_JS % (m.get_name(), places_js, layers_js)))

    # Layer stacking
    feature_groups["AOD_Today"].add_to(m)
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
            <span class="lg-item"><span style="display: inline-block; width: 14px; height: 14px; background: #FF4500; border-radius: 50%; margin-right: 5px;"></span> High (&gt;50 MW)</span><br>
            <span class="lg-item"><span style="display: inline-block; width: 8px; height: 8px; background: #8e5cff; border: 1.5px solid #fff; border-radius: 50%; margin-right: 4px; vertical-align: -2px;"></span> Night pass</span>
        </div>
        <hr style="border: 0; border-top: 1px solid #333; margin: 6px 0;">
        <b class="lg-title" style="font-size: 12px; color: #fff; display: block; margin-bottom: 5px;">💨 Aerosol Loading (AOD Today)</b>
        <div style="display: inline-block; width: 100%; height: 12px; background: linear-gradient(to right, #440154, #3b4cc0, #1fa0d0, #5fd35f, #ffea46, #fd1d24); border-radius: 2px; opacity: 0.8;"></div>
        <div style="width: 100%; display: flex; justify-content: space-between; padding-top: 2px; font-size: 9px; color: #aaa;">
            <span>0.0 (Clear)</span>
            <span>{aod_max_scale:.1f}+ (Dense Plume)</span>
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

    if downloaded_paths:
        print("[*] Tearing down cached track temporary segments...")
        for path in downloaded_paths:
            if os.path.exists(path):
                os.remove(path)
        gc.collect()
        print("[*] Workspace clean.")

if __name__ == "__main__":
    main()
