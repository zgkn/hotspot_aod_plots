import os
import sys
import datetime
import re
import gc
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

# Suppress intrusive connection pool warning messages
logging.getLogger("urllib3").setLevel(logging.ERROR)

def install_deps():
    try:
        import boto3, xarray, netCDF4, folium, matplotlib, requests
    except ImportError:
        print("[*] Installing missing dependencies...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q",
                               "boto3", "xarray", "netcdf4", "h5netcdf", "folium", "matplotlib", "requests"])
install_deps()

import numpy as np
import xarray as xr
import boto3
import requests
from botocore import UNSIGNED
from botocore.config import Config
import matplotlib.pyplot as plt
import folium
from folium.plugins import HeatMap

# --- TELEGRAM CONFIGURATIONS ---
BOT_TOKEN = "5498510198:AAH20TLQrL6TiXwMu4fMKib5TcxPa22OBkQ"
CHAT_ID = "-1001566412226"

def send_telegram_message(text: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Failed to send message: {e}")
        return False

def send_telegram_file(file_path: str, caption: str = None) -> bool:
    if not os.path.exists(file_path):
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    payload = {"chat_id": CHAT_ID}
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "Markdown"
    try:
        with open(file_path, 'rb') as file:
            files = {'document': file}
            response = requests.post(url, data=payload, files=files, timeout=30)
            response.raise_for_status()
            return True
    except Exception as e:
        print(f"Failed to send file: {e}")
        return False

# --- SHARED DOMAIN CONFIGURATIONS ---
SEA_LAT = [-11.0, 28.0]
SEA_LON = [90.0, 142.0]

SATELLITE_CONFIGS = {
    'NOAA-20': {
        'bucket': 'noaa-nesdis-n20-pds',
        'efire_prefix': 'VIIRS_EFIRE_VIIRSI_EDR',
        'aod_prefix': 'VIIRS-JRR-AOD'
    },
    'NOAA-21': {
        'bucket': 'noaa-nesdis-n21-pds',
        'efire_prefix': 'VIIRS_EFIRE_VIIRSI_EDR',
        'aod_prefix': 'VIIRS-JRR-AOD'
    },
    'S-NPP': {
        'bucket': 'noaa-nesdis-snpp-pds',
        'efire_prefix': 'VIIRS_EFIRE_VIIRSI_EDR',
        'aod_prefix': 'VIIRS-JRR-AOD'
    }
}

s3_client = boto3.client('s3', config=Config(signature_version=UNSIGNED))

# --- STATIC REPLACEMENT FOR TLE ORBITAL PREDICTION ---
def get_typical_overpass_hours(night_only=False, day_only=False):
    if day_only:
        return [3, 4, 5, 6, 7, 8]       # Expanded window (UTC)
    if night_only:
        return [15, 16, 17, 18, 19, 20] # Expanded window (UTC)
    return [3, 4, 5, 6, 7, 8, 15, 16, 17, 18, 19, 20]

# --- S3 ATOMIC TRANSACTIONS ---
def download_worker(bucket, key, local_path):
    try:
        if not os.path.exists(local_path):
            s3_client.download_file(bucket, key, local_path)
        return local_path
    except Exception as e:
        print(f"[!] Fail transfer: {os.path.basename(key)}: {e}")
        return None

# --- ENGINE LOGIC FOR ACTIVE HOTSPOTS ---
def scrape_efire(sat_name, config, date_obj, hours, label):
    prefix = f"{config['efire_prefix']}/{date_obj.strftime('%Y/%m/%d')}/"
    tasks = []
    try:
        res = s3_client.list_objects_v2(Bucket=config['bucket'], Prefix=prefix)
        for obj in res.get('Contents', []):
            fn = os.path.basename(obj['Key'])
            match = re.search(r'_s(\d{14})', fn)
            if match and int(match.group(1)[8:10]) in hours:
                local_fn = f"efire_{label}_{sat_name}_{fn}"
                tasks.append((config['bucket'], obj['Key'], local_fn))
    except Exception as e:
        print(f"[!] Warning: Unable to scrape EFIRE for {sat_name} on {date_obj.date()}: {e}")
    return tasks

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
def rasterize_aod(lats, lons, aods):
    res = 500
    rgba = np.zeros((res, res, 4), dtype=np.uint8)
    norm_y = ((res-1) - ((lats - SEA_LAT[0]) / (SEA_LAT[1] - SEA_LAT[0]) * (res-1))).astype(int)
    norm_x = ((lons - SEA_LON[0]) / (SEA_LON[1] - SEA_LON[0]) * (res-1)).astype(int)
    mask = (norm_x >= 0) & (norm_x < res) & (norm_y >= 0) & (norm_y < res)

    cmap = plt.get_cmap('YlGnBu')
    colors = (cmap(np.clip(aods[mask], 0, 1.0)) * 255).astype(np.uint8)
    colors[:, 3] = 175
    rgba[norm_y[mask], norm_x[mask]] = colors
    return rgba

# --- EXECUTION TARGET PIPELINE ---
def main():
    sgt_tz = datetime.timezone(datetime.timedelta(hours=8))
    local_now = datetime.datetime.now(sgt_tz)

    today_date = local_now.date()
    yesterday_date = today_date - datetime.timedelta(days=1)

    sg_date_str = local_now.strftime('%Y%m%d')
    
    # Create output directory for GitLab Artifacts
    os.makedirs("output", exist_ok=True)
    output_filename = os.path.join("output", f"hotspots_aod_map_{sg_date_str}.html")

    print(f"[*] Combined Core Initiated | SGT Now: {local_now.strftime('%Y-%m-%d %H:%M:%S')}")

    send_telegram_message(f"🚀 *Pipeline Started*\n🕒 SGT: `{local_now.strftime('%Y-%m-%d %H:%M:%S')}`\nTargeting AOD (Today) & EFIRE (Yesterday Night + Today Day).")

    m = folium.Map(location=[8.5, 116.0], zoom_start=5, tiles=None)

    # Basemap 1: Esri Satellite Imagery
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri",
        name="Esri Satellite Imagery"
    ).add_to(m)

    # Basemap 2: OpenStreetMap (Provides detailed area and province labels)
    folium.TileLayer(
        tiles="OpenStreetMap",
        name="OpenStreetMap (Labels & Areas)",
        show=False # Hidden by default, toggleable via layer control
    ).add_to(m)

    # --- NEW: Country Boundaries Overlay ---
    boundary_style = {'color': '#ffffff', 'weight': 1.5, 'fillOpacity': 0}
    folium.GeoJson(
        "https://raw.githubusercontent.com/python-visualization/folium/master/examples/data/world-countries.json",
        name="Country Boundaries",
        style_function=lambda x: boundary_style
    ).add_to(m)
    # ----------------------------------------

    feature_groups = {
        "EFIRE_Yesterday_Night": folium.FeatureGroup(name=f"🌌 EFIRE Hotspots Night ({yesterday_date})", show=True),
        "EFIRE_Today_Day": folium.FeatureGroup(name=f"🔥 EFIRE Hotspots Day ({today_date})", show=True),
        "AOD_Today": folium.FeatureGroup(name=f"💨 Aerosol AOD ({today_date})", show=True),
        "Hotspot_Density_Night": folium.FeatureGroup(name="🧲 Density Heatmap (Night)", show=False),
        "Hotspot_Density_Day": folium.FeatureGroup(name="🧲 Density Heatmap (Day)", show=False)
    }

    download_manifest = []

    y_night_hours = get_typical_overpass_hours(night_only=True)
    t_day_hours = get_typical_overpass_hours(day_only=True)

    for sat, config in SATELLITE_CONFIGS.items():
        download_manifest.extend(scrape_efire(sat, config, yesterday_date, y_night_hours, "YESTERDAY_NIGHT"))
        download_manifest.extend(scrape_efire(sat, config, today_date, t_day_hours, "TODAY_DAY"))
        download_manifest.extend(scrape_aod(sat, config, today_date, "TODAY_AOD"))

    send_telegram_message(f"🔍 *Section 1 Complete: S3 Scanning*\n• Found `{len(download_manifest)}` telemetry files matching current window criteria.")

    downloaded_paths = []
    if download_manifest:
        print(f"[*] Executing concurrent transfer pool for {len(download_manifest)} NetCDF tracks...")
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(download_worker, b, k, lf) for b, k, lf in download_manifest]
            for fut in as_completed(futures):
                res_path = fut.result()
                if res_path:
                    downloaded_paths.append(res_path)

    send_telegram_message(f"📥 *Section 2 Complete: S3 Downloads*\n• Downloaded `{len(downloaded_paths)}/{len(download_manifest)}` target arrays cleanly.")

    print("\n[*] Processing local footprints into vector entities...")
    aod_lats, aod_lons, aod_vals = [], [], []
    heatmap_data_night = []  # Night density array
    heatmap_data_day = []    # Day density array
    hotspots_plotted = 0

    for path in downloaded_paths:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            continue

        engine_choice = 'h5netcdf' if path.endswith('.h5') else 'netcdf4'

        try:
            if "efire" in path:
                sat_name = path.split("_")[3]
                is_night = "YESTERDAY_NIGHT" in path
                target_key = "EFIRE_Yesterday_Night" if is_night else "EFIRE_Today_Day"
                target_fg = feature_groups[target_key]

                with xr.open_dataset(path, engine=engine_choice) as ds:
                    lat_vars = [v for v in ds.variables if 'FP_latitude' in v or 'latitude' in v.lower()]
                    relative_lons = [v for v in ds.variables if 'FP_longitude' in v or 'longitude' in v.lower()]
                    pow_vars = [v for v in ds.variables if 'FP_power' in v or 'FRP' in v or 'power' in v.lower()]

                    if not (lat_vars and relative_lons and pow_vars):
                        continue

                    lats = np.atleast_1d(ds[lat_vars[0]].values)
                    lons = np.atleast_1d(ds[relative_lons[0]].values)
                    frps = np.atleast_1d(ds[pow_vars[0]].values)

                    for i in range(len(lats)):
                        lat, lon, p = lats[i], lons[i], frps[i]
                        if np.isnan(p) or p < 0: continue

                        if (SEA_LAT[0] <= lat <= SEA_LAT[1]) and (SEA_LON[0] <= lon <= SEA_LON[1]):
                            # Route to correct heatmap array
                            if is_night:
                                heatmap_data_night.append([float(lat), float(lon)])
                            else:
                                heatmap_data_day.append([float(lat), float(lon)])

                            color = '#FFD700' if p < 10 else ('#FF8C00' if p < 50 else '#FF4500')
                            popup_html = f"<b>🛰️ {sat_name}</b><br>Lat: {lat:.3f}<br>Lon: {lon:.3f}<br>FRP: <b>{p:.1f} MW</b>"

                            folium.CircleMarker(
                                location=[lat, lon],
                                radius=float(np.clip(p / 6.0, 6, 14)),
                                color=color,
                                weight=2,
                                fill=True,
                                fill_color=color,
                                fill_opacity=0.85,
                                popup=folium.Popup(popup_html, max_width=150)
                            ).add_to(target_fg)
                            hotspots_plotted += 1

            elif "aod" in path:
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
            print(f"[!] Processing structural fault on file {os.path.basename(path)}: {e}.")

    aod_mapped = False
    if aod_vals:
        print(f"    -> Mapping gridded AOD surface overlay: {len(aod_vals)} data arrays.")
        img_layer = rasterize_aod(np.array(aod_lats), np.array(aod_lons), np.array(aod_vals))
        folium.raster_layers.ImageOverlay(
            img_layer,
            bounds=[[SEA_LAT[0], SEA_LON[0]], [SEA_LAT[1], SEA_LON[1]]],
            origin='upper',
            interactive=False
        ).add_to(feature_groups["AOD_Today"])
        aod_mapped = True

    # Generate Night Heatmap
    if heatmap_data_night:
        print(f"    -> Generating Night Density Heatmap from {len(heatmap_data_night)} points.")
        HeatMap(
            heatmap_data_night,
            name="Density Night",
            min_opacity=0.4,
            radius=15,
            blur=10,
            max_zoom=1,
        ).add_to(feature_groups["Hotspot_Density_Night"])

    # Generate Day Heatmap
    if heatmap_data_day:
        print(f"    -> Generating Day Density Heatmap from {len(heatmap_data_day)} points.")
        HeatMap(
            heatmap_data_day,
            name="Density Day",
            min_opacity=0.4,
            radius=15,
            blur=10,
            max_zoom=1,
        ).add_to(feature_groups["Hotspot_Density_Day"])

    # Layer stacking
    feature_groups["AOD_Today"].add_to(m)
    feature_groups["Hotspot_Density_Night"].add_to(m)
    feature_groups["Hotspot_Density_Day"].add_to(m)
    feature_groups["EFIRE_Yesterday_Night"].add_to(m)
    feature_groups["EFIRE_Today_Day"].add_to(m)

    composite_legend = """
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
        <div style="display: inline-block; width: 100%; height: 12px; background: linear-gradient(to right, #FFFFE5, #41B6C4, #081D58); border-radius: 2px; opacity: 0.8;"></div>
        <div style="width: 100%; display: flex; justify-content: space-between; padding-top: 2px; font-size: 9px; color: #aaa;">
            <span>0.0 (Clear)</span>
            <span>1.0+ (Dense Plume)</span>
        </div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(composite_legend))
    folium.LayerControl(collapsed=False).add_to(m)

    m.save(output_filename)
    print(f"\n[!] SUCCESS: Target structural mapping complete. Saved to '{output_filename}'")

    send_telegram_message(f"🎨 *Section 3 Complete: Visualization Rendering*\n• Hotspots Rendered: `{hotspots_plotted}` fires.\n• AOD Raster Generated: `{aod_mapped}`.\n• Map file written: `{output_filename}`")

    send_telegram_file(output_filename, caption=f"📊 *Map Product Ready*\nHere is the compiled Southeast Asia active fire and aerosol overlay map.")

    if downloaded_paths:
        print("[*] Tearing down cached track temporary segments...")
        for path in downloaded_paths:
            if os.path.exists(path):
                os.remove(path)
        gc.collect()
        print("[*] Workspace clean.")

if __name__ == "__main__":
    main()
      
