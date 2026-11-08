"""Daily VIIRS AOD grids, cached per UTC day, for the period selector of the map.

Source: VIIRS-JRR-AOD (AOD550) of NOAA-20 and NOAA-21 on the NOAA open-data buckets, the passes that start 02:30-09:30 UTC (the
daytime passes over the domain). Every granule is sub-sampled (every 5th pixel, as the map always did) and averaged into a
STEP-degree grid; the day's grid is the mean over all its passes. It is cached as one small file per day (aod_YYYYMMDD.npz) next
to the fire pixels, so a day is downloaded once, and exported as a grey-scale PNG that the page reads in the browser: the grey
level is AOD / AOD_Q, NODATA where nothing was retrieved. The page averages the days of the chosen period.
"""
import datetime
import json
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

STEP = 0.1                                       # degrees per cell
AOD_Q = 0.025                                    # AOD per grey level; 254 levels reach 6.35
NODATA = 255
SUBSAMPLE = 5
WINDOW = (datetime.time(2, 30), datetime.time(9, 30))      # UTC start times of the granules that are used
COMPLETE_AFTER_HOUR = 10                         # a day is complete when fetched after this UTC hour (the window is posted by then)
KEEP_DAYS = 40
MIN_SHARE = 0.5                                  # a day with fewer files than this share of the day before is still being posted
RULES = f"v1 step={STEP:g} q={AOD_Q:g} sub={SUBSAMPLE} window={WINDOW[0]:%H:%M}-{WINDOW[1]:%H:%M}"


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def grid_shape(domain):
    south, west, north, east = domain
    return int(round((north - south) / STEP)), int(round((east - west) / STEP))


# ------------------------------------------------------------------------------------------------------------ listing
def list_files(s3, sat_configs, date):
    """[(bucket, key)] of the AOD granules of the window of one UTC day, all satellites, in time order."""
    start = datetime.datetime.combine(date, WINDOW[0])
    end = datetime.datetime.combine(date, WINDOW[1])
    out = []
    for sat, config in sat_configs.items():
        prefix = f"{config['aod_prefix']}/{date:%Y/%m/%d}/"
        try:
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=config["bucket"], Prefix=prefix):
                for obj in page.get("Contents", []):
                    m = re.search(r"_s(\d{14})", obj["Key"])
                    if m and start <= datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S") <= end:
                        out.append((m.group(1), config["bucket"], obj["Key"]))
        except Exception as e:
            print(f"[!] Unable to list the AOD of {sat} on {date}: {e}")
    return [(b, k) for _, b, k in sorted(out)]


# ------------------------------------------------------------------------------------------------------------ fetching
def fetch_day(s3, files, domain, workers=8):
    """Download and average the given granules. Returns (uint8 grid, north row first, meta)."""
    import xarray as xr
    south, west, north, east = domain
    ny, nx = grid_shape(domain)
    sums = np.zeros(ny * nx)
    counts = np.zeros(ny * nx)
    tmp = tempfile.mkdtemp(prefix="aod_")
    lock = threading.Lock()                      # the HDF5 library is not thread safe: only the downloads run in parallel
    failed = []

    def work(i, item):
        bucket, key = item
        local = os.path.join(tmp, f"{i}.nc")
        try:
            s3.download_file(bucket, key, local)
            with lock, xr.open_dataset(local, engine="netcdf4") as ds:
                if not all(v in ds for v in ("AOD550", "Latitude", "Longitude")):
                    return i, None, None
                a = ds["AOD550"].values[::SUBSAMPLE, ::SUBSAMPLE].ravel()
                lat = ds["Latitude"].values[::SUBSAMPLE, ::SUBSAMPLE].ravel()
                lon = ds["Longitude"].values[::SUBSAMPLE, ::SUBSAMPLE].ravel()
            ok = np.isfinite(a) & (lat >= south) & (lat < north) & (lon >= west) & (lon < east)
            iy = np.clip(((north - lat[ok].astype("float64")) / STEP).astype(int), 0, ny - 1)
            ix = np.clip(((lon[ok].astype("float64") - west) / STEP).astype(int), 0, nx - 1)
            return i, iy * nx + ix, a[ok].astype("float64")
        except Exception as e:
            failed.append(f"{os.path.basename(key)}: {e}")
            return i, None, None
        finally:
            if os.path.exists(local):
                os.remove(local)

    try:
        with ThreadPoolExecutor(workers) as ex:
            for fut in as_completed([ex.submit(work, i, f) for i, f in enumerate(files)]):
                _, idx, a = fut.result()
                if idx is not None and len(idx):
                    sums += np.bincount(idx, weights=a, minlength=ny * nx)
                    counts += np.bincount(idx, minlength=ny * nx)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    grid = np.full(ny * nx, NODATA, "uint8")
    has = counts > 0
    grid[has] = np.clip(np.rint(np.clip(sums[has] / counts[has], 0, None) / AOD_Q), 0, NODATA - 1).astype("uint8")
    if failed:
        print(f"[!] {len(failed)} AOD granules failed, for example {failed[0]}")
    meta = {"fetched_at": utc_now().isoformat(timespec="seconds"), "rules": RULES, "n_files": len(files), "failed": len(failed),
            "cells": int(has.sum())}
    return grid.reshape(ny, nx), meta


# ------------------------------------------------------------------------------------------------------------- cache
def cache_file(cache_dir, date):
    return Path(cache_dir) / f"aod_{date:%Y%m%d}.npz"


def save_day(cache_dir, date, grid, meta):
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    final = cache_file(cache_dir, date)
    tmp = final.with_name(final.stem + "_tmp.npz")
    np.savez_compressed(tmp, grid=grid, meta=np.array(json.dumps({**meta, "date": f"{date:%Y-%m-%d}"})))
    os.replace(tmp, final)


def load_day(cache_dir, date):
    """The cached day as {grid, meta, complete}, or None when missing or made with other rules."""
    path = cache_file(cache_dir, date)
    if not path.exists():
        return None
    try:
        z = np.load(path)
        meta = json.loads(str(z["meta"]))
        if meta.get("rules") != RULES:
            return None
        grid = z["grid"]
    except Exception as e:
        print(f"[!] Cache file {path.name} unreadable ({e}); it will be fetched again")
        return None
    ready = datetime.datetime.combine(date, datetime.time(COMPLETE_AFTER_HOUR))
    complete = meta.get("failed", 0) == 0 and datetime.datetime.fromisoformat(meta["fetched_at"]) >= ready
    return {"grid": grid, "meta": meta, "complete": complete}


def prune(cache_dir, today, keep_days=KEEP_DAYS):
    oldest = today - datetime.timedelta(days=keep_days)
    for p in Path(cache_dir).glob("aod_*.npz"):
        m = re.fullmatch(r"aod_(\d{8})\.npz", p.name)
        if m and datetime.datetime.strptime(m.group(1), "%Y%m%d").date() < oldest:
            p.unlink()


def refresh(cache_dir, dates, s3, sat_configs, domain, budget_s=2100, workers=8):
    """Fetch the days that are missing, incomplete or made with other rules, newest first, until the time budget is used.

    A day that was fetched before its passes were all posted is fetched again only when more granules are listed now.
    Returns (fetched dates, dates left for a later run).
    """
    start = time.monotonic()
    fetched, left = [], []
    today = utc_now().date()
    for date in sorted(dates, reverse=True):
        if date > today:
            continue
        have = load_day(cache_dir, date)
        if have is not None and have["complete"]:
            continue
        files = list_files(s3, sat_configs, date)
        if not files:
            continue
        if have is not None and have["meta"].get("n_files") == len(files) and not have["meta"].get("failed"):
            continue                              # nothing new has been posted since it was fetched
        if time.monotonic() - start > budget_s:
            left.append(date)
            continue
        t0 = time.monotonic()
        grid, meta = fetch_day(s3, files, domain, workers)
        save_day(cache_dir, date, grid, meta)
        fetched.append(date)
        print(f"[*] AOD {date}: {meta['cells']} cells from {len(files)} granules ({time.monotonic() - t0:.0f} s)")
    return fetched, sorted(left)


def good_days(cache_dir, dates):
    """The cached days to show: those with at least MIN_SHARE of the previous day's granules (the first passes of a new day are
    too few to draw)."""
    out, prev = [], None
    for date in sorted(dates):
        day = load_day(cache_dir, date)
        if day is None or not day["meta"].get("n_files"):
            continue
        n = day["meta"]["n_files"]
        if prev is None or n >= MIN_SHARE * prev:
            out.append(date)
            prev = n
    return out


# ------------------------------------------------------------------------------------------------------------- export
def export_day(cache_dir, date, out_dir):
    """Write data/aod_YYYYMMDD.png (grey level = AOD / AOD_Q, NODATA where none). Returns True when written."""
    from PIL import Image
    day = load_day(cache_dir, date)
    if day is None:
        return False
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    Image.fromarray(day["grid"]).save(Path(out_dir) / f"aod_{date:%Y%m%d}.png", optimize=True)
    return True
