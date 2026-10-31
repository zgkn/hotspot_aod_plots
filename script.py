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
from trend_panel import trend_panel

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
    },
    'S-NPP': {
        'bucket': 'noaa-nesdis-snpp-pds',
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

    # IMPORTANT: Create output directory for GitHub Artifacts
    os.makedirs("output", exist_ok=True)

    print(f"[*] Combined Core Initiated | SGT Now: {local_now.strftime('%Y-%m-%d %H:%M:%S')}")

    domain = (SEA_LAT[0], SEA_LON[0], SEA_LAT[1], SEA_LON[1])
    days, area_text = get_selection()
    try:
        area = fire_trend.parse_area(area_text, domain)
    except ValueError as e:
        raise SystemExit(f"[X] {e}")
    end_day = fire_trend.latest_day()
    dates = fire_trend.day_list(end_day, days)
    print(f"[*] Hotspot trend: {days} days up to {end_day} (UTC) | area: {area.name}")

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
        "Hotspots_Day": folium.FeatureGroup(name=f"🔥 Daytime hotspots ({end_day} UTC)", show=True),
        "AOD_Today": folium.FeatureGroup(name=f"💨 Aerosol AOD ({today_date})", show=True),
        "Area": folium.FeatureGroup(name=f"📐 Trend area: {area.name}", show=True),
    }

    # --- Daytime fire pixels: refresh the per-day cache, newest day first ---
    budget_s = float(os.getenv("HOTSPOT_BUDGET_MIN", "60")) * 60
    fetched, left = fire_trend.refresh(CACHE_DIR, dates, s3_client, SATELLITE_CONFIGS, domain, budget_s=budget_s, workers=12)
    fire_trend.prune(CACHE_DIR, end_day)
    print(f"[*] Hotspot cache: {len(fetched)} days fetched, {len(left)} left for a later run (time budget {budget_s / 60:.0f} min)")

    # --- AOD downloads (today) ---
    download_manifest = []
    for sat, config in SATELLITE_CONFIGS.items():
        download_manifest.extend(scrape_aod(sat, config, today_date, "TODAY_AOD"))

    print(f"[*] Found {len(download_manifest)} AOD files matching current window criteria.")

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

    # --- Daytime hotspots of the latest day, from the cache (de-duplicated) ---
    hotspots_plotted = 0
    latest = fire_trend.load_day(CACHE_DIR, end_day)
    if latest is not None:
        for lat, lon, p, sat in zip(latest["lat"], latest["lon"], latest["frp"], latest["sat"]):
            color = '#FFD700' if p < 10 else ('#FF8C00' if p < 50 else '#FF4500')
            popup_html = (f"<b>🛰️ {fire_trend.SAT_ORDER[int(sat)]}</b><br>Lat: {lat:.3f}<br>Lon: {lon:.3f}"
                          f"<br>FRP: <b>{p:.1f} MW</b>")
            folium.CircleMarker(
                location=[float(lat), float(lon)],
                radius=float(np.clip(p / 6.0, 6, 14)),
                color=color,
                weight=2,
                fill=True,
                fill_color=color,
                fill_opacity=0.85,
                popup=folium.Popup(popup_html, max_width=150)
            ).add_to(feature_groups["Hotspots_Day"])
            hotspots_plotted += 1
    print(f"[*] Hotspot markers drawn for {end_day}: {hotspots_plotted}")

    # --- Trend of daily unique daytime hotspots in the chosen area ---
    rows = fire_trend.build_series(CACHE_DIR, dates, area)
    print(f"\n[*] Daily unique daytime hotspots in {area.name}:")
    for r in rows:
        state = "no data" if r["count"] is None else ("complete" if r["complete"] else "partial")
        print(f"    {r['date']}  {'-' if r['count'] is None else r['count']:>6}  {state}  granules={r['granules']}")
    m.get_root().html.add_child(folium.Element(trend_panel(rows, area.name, left=len(left))))
    if area.kind != "domain":
        folium.GeoJson(
            area.geojson(),
            style_function=lambda x: {"color": "#4dd0e1", "weight": 2.5, "fillOpacity": 0.04, "dashArray": "6 4"},
        ).add_to(feature_groups["Area"])

    # Layer stacking
    feature_groups["AOD_Today"].add_to(m)
    feature_groups["Area"].add_to(m)
    feature_groups["Hotspots_Day"].add_to(m)

    composite_legend = f"""
    <div style="position: fixed; bottom: 35px; left: 25px; width: 235px; height: 195px;
                background-color: rgba(20, 20, 20, 0.88); color: #ffffff; z-index:9999;
                font-size: 11px; font-family: Arial, sans-serif; border: 1px solid #444;
                padding: 12px; border-radius: 6px; box-shadow: 0 0 15px rgba(0,0,0,0.6);">
        <b style="font-size: 12px; color: #fff; display: block; margin-bottom: 5px;">🔥 Fire Intensity (FRP)</b>
        <div style="line-height: 16px; margin-bottom: 8px;">
            <span style="display: inline-block; width: 6px; height: 6px; background: #FFD700; border-radius: 50%; margin-right: 5px;"></span> Low (&lt;10 MW)<br>
            <span style="display: inline-block; width: 10px; height: 10px; background: #FF8C00; border-radius: 50%; margin-right: 5px;"></span> Mid (10-50 MW)<br>
            <span style="display: inline-block; width: 14px; height: 14px; background: #FF4500; border-radius: 50%; margin-right: 5px;"></span> High (&gt;50 MW)
        </div>
        <hr style="border: 0; border-top: 1px solid #333; margin: 6px 0;">
        <b style="font-size: 12px; color: #fff; display: block; margin-bottom: 5px;">💨 Aerosol Loading (AOD Today)</b>
        <div style="display: inline-block; width: 100%; height: 12px; background: linear-gradient(to right, #440154, #3b4cc0, #1fa0d0, #5fd35f, #ffea46, #fd1d24); border-radius: 2px; opacity: 0.8;"></div>
        <div style="width: 100%; display: flex; justify-content: space-between; padding-top: 2px; font-size: 9px; color: #aaa;">
            <span>0.0 (Clear)</span>
            <span>{aod_max_scale:.1f}+ (Dense Plume)</span>
        </div>
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
