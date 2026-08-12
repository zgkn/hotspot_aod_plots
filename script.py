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
# Token is now pulled from the GitHub Secret 'TELEGRAM_BOT_TOKEN'
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = "-1001566412226"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set! Please check your GitHub Repository Secrets.")

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

# --- CONFIGS & ENGINE LOGIC ---
SEA_LAT = [-11.0, 28.0]
SEA_LON = [90.0, 142.0]
SATELLITE_CONFIGS = {
    'NOAA-20': {'bucket': 'noaa-nesdis-n20-pds', 'efire_prefix': 'VIIRS_EFIRE_VIIRSI_EDR', 'aod_prefix': 'VIIRS-JRR-AOD'},
    'NOAA-21': {'bucket': 'noaa-nesdis-n21-pds', 'efire_prefix': 'VIIRS_EFIRE_VIIRSI_EDR', 'aod_prefix': 'VIIRS-JRR-AOD'},
    'S-NPP': {'bucket': 'noaa-nesdis-snpp-pds', 'efire_prefix': 'VIIRS_EFIRE_VIIRSI_EDR', 'aod_prefix': 'VIIRS-JRR-AOD'}
}

s3_client = boto3.client('s3', config=Config(signature_version=UNSIGNED))

def get_typical_overpass_hours(night_only=False, day_only=False):
    if day_only: return [3, 4, 5, 6, 7, 8]
    if night_only: return [15, 16, 17, 18, 19, 20]
    return [3, 4, 5, 6, 7, 8, 15, 16, 17, 18, 19, 20]

def download_worker(bucket, key, local_path):
    try:
        if not os.path.exists(local_path):
            s3_client.download_file(bucket, key, local_path)
        return local_path
    except Exception as e:
        return None

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
        print(f"Warning: {e}")
    return tasks

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
        print(f"Warning: {e}")
    return tasks

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

def main():
    sgt_tz = datetime.timezone(datetime.timedelta(hours=8))
    local_now = datetime.datetime.now(sgt_tz)
    today_date = local_now.date()
    yesterday_date = today_date - datetime.timedelta(days=1)
    
    os.makedirs("output", exist_ok=True)
    output_filename = os.path.join("output", f"hotspots_aod_map_{local_now.strftime('%Y%m%d')}.html")

    send_telegram_message(f"🚀 *Pipeline Started*\n🕒 SGT: `{local_now.strftime('%Y-%m-%d %H:%M:%S')}`")

    m = folium.Map(location=[8.5, 116.0], zoom_start=5, tiles="CartoDB positron")
    
    # [Insert your existing processing loop here]
    # (Skipped for brevity, but ensure you keep your plotting logic intact)
    
    m.save(output_filename)
    send_telegram_file(output_filename, caption="📊 *Map Product Ready*")

if __name__ == "__main__":
    main()
    
