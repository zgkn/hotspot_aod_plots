"""Daily VIIRS grids (AOD and cloud mask), cached per UTC day, for the date control of the map.

Sources (NOAA open-data buckets, NOAA-20 and NOAA-21, the passes that start 02:30-09:30 UTC, the daytime passes over the domain):
* "aod":   VIIRS-JRR-AOD, AOD550;
* "cloud": VIIRS-JRR-CloudMask, CloudMask (0 clear, 1 probably clear, 2 probably cloudy, 3 cloudy). The value averaged is the share of
           pixels that are probably cloudy or cloudy, so a cell holds a cloud fraction from 0 to 1.
Every granule is sub-sampled (every 5th pixel) and averaged into a STEP-degree grid; the day's grid is the mean over all its passes.
It is cached as one small file per day and product (aod_YYYYMMDD.npz, cloud_YYYYMMDD.npz) next to the fire pixels, so a day is
downloaded once, and exported as a grey-scale PNG that the page reads in the browser: the grey level is value x scale, NODATA where
nothing was retrieved. The page averages the days of the chosen period.
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
MIN_SHARE = 0.5                                  # a day with fewer files than this share of the day before's is still being posted

# per product: bucket folder key in the satellite configs, variable, grey levels per unit, file stem
KINDS = {
    "aod": {"prefix_key": "aod_prefix", "var": "AOD550", "levels": 1 / AOD_Q, "stem": "aod"},
    "cloud": {"prefix_key": "cloud_prefix", "var": "CloudMask", "levels": 250.0, "stem": "cloud"},
}


def rules(kind="aod"):
    """The cache rules of a product; the AOD string is the one its first cache files were written with and must not change."""
    window = f"window={WINDOW[0]:%H:%M}-{WINDOW[1]:%H:%M}"
    if kind == "aod":
        return f"v1 step={STEP:g} q={AOD_Q:g} sub={SUBSAMPLE} {window}"
    return f"v1 {kind} step={STEP:g} levels={KINDS[kind]['levels']:g} sub={SUBSAMPLE} {window}"


RULES = rules("aod")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def grid_shape(domain):
    south, west, north, east = domain
    return int(round((north - south) / STEP)), int(round((east - west) / STEP))


# ------------------------------------------------------------------------------------------------------------ listing
def list_files(s3, sat_configs, date, kind="aod"):
    """[(bucket, key)] of the granules of the window of one UTC day, all satellites, in time order."""
    start = datetime.datetime.combine(date, WINDOW[0])
    end = datetime.datetime.combine(date, WINDOW[1])
    key_name = KINDS[kind]["prefix_key"]
    out = []
    for sat, config in sat_configs.items():
        prefix = f"{config[key_name]}/{date:%Y/%m/%d}/"
        try:
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=config["bucket"], Prefix=prefix):
                for obj in page.get("Contents", []):
                    m = re.search(r"_s(\d{14})", obj["Key"])
                    if m and start <= datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S") <= end:
                        out.append((m.group(1), config["bucket"], obj["Key"]))
        except Exception as e:
            print(f"[!] Unable to list the {kind} files of {sat} on {date}: {e}")
    return [(b, k) for _, b, k in sorted(out)]


# ------------------------------------------------------------------------------------------------------------ fetching
def _pixel_values(kind, raw):
    """(values, ok) of the sub-sampled pixels: AOD itself, or 1 for a cloudy and 0 for a clear pixel of the cloud mask."""
    raw = raw.astype("float64")
    if kind == "cloud":
        ok = np.isfinite(raw) & (raw >= 0) & (raw <= 3)
        return (raw >= 2).astype("float64"), ok
    return raw, np.isfinite(raw)


def fetch_day(s3, files, domain, workers=8, kind="aod"):
    """Download and average the given granules. Returns (uint8 grid, north row first, meta)."""
    import xarray as xr
    south, west, north, east = domain
    var = KINDS[kind]["var"]
    ny, nx = grid_shape(domain)
    sums = np.zeros(ny * nx)
    counts = np.zeros(ny * nx)
    tmp = tempfile.mkdtemp(prefix=f"{kind}_")
    lock = threading.Lock()                      # the HDF5 library is not thread safe: only the downloads run in parallel
    failed = []

    def work(i, item):
        bucket, key = item
        local = os.path.join(tmp, f"{i}.nc")
        try:
            s3.download_file(bucket, key, local)
            with lock, xr.open_dataset(local, engine="netcdf4") as ds:
                if not all(v in ds for v in (var, "Latitude", "Longitude")):
                    return i, None, None
                a = ds[var].values[::SUBSAMPLE, ::SUBSAMPLE].ravel()
                lat = ds["Latitude"].values[::SUBSAMPLE, ::SUBSAMPLE].ravel()
                lon = ds["Longitude"].values[::SUBSAMPLE, ::SUBSAMPLE].ravel()
            vals, ok = _pixel_values(kind, a)
            ok &= np.isfinite(lat) & np.isfinite(lon) & (lat >= south) & (lat < north) & (lon >= west) & (lon < east)
            iy = np.clip(((north - lat[ok].astype("float64")) / STEP).astype(int), 0, ny - 1)
            ix = np.clip(((lon[ok].astype("float64") - west) / STEP).astype(int), 0, nx - 1)
            return i, iy * nx + ix, vals[ok]
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
    grid[has] = np.clip(np.rint(np.clip(sums[has] / counts[has], 0, None) * KINDS[kind]["levels"]), 0, NODATA - 1).astype("uint8")
    if failed:
        print(f"[!] {len(failed)} {kind} granules failed, for example {failed[0]}")
    meta = {"fetched_at": utc_now().isoformat(timespec="seconds"), "rules": rules(kind), "n_files": len(files), "failed": len(failed),
            "cells": int(has.sum())}
    return grid.reshape(ny, nx), meta


# ------------------------------------------------------------------------------------------------------------- cache
def cache_file(cache_dir, date, kind="aod"):
    return Path(cache_dir) / f"{KINDS[kind]['stem']}_{date:%Y%m%d}.npz"


def save_day(cache_dir, date, grid, meta, kind="aod"):
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    final = cache_file(cache_dir, date, kind)
    tmp = final.with_name(final.stem + "_tmp.npz")
    np.savez_compressed(tmp, grid=grid, meta=np.array(json.dumps({**meta, "date": f"{date:%Y-%m-%d}"})))
    os.replace(tmp, final)


def load_day(cache_dir, date, kind="aod"):
    """The cached day as {grid, meta, complete}, or None when missing or made with other rules."""
    path = cache_file(cache_dir, date, kind)
    if not path.exists():
        return None
    try:
        z = np.load(path)
        meta = json.loads(str(z["meta"]))
        if meta.get("rules") != rules(kind):
            return None
        grid = z["grid"]
    except Exception as e:
        print(f"[!] Cache file {path.name} unreadable ({e}); it will be fetched again")
        return None
    ready = datetime.datetime.combine(date, datetime.time(COMPLETE_AFTER_HOUR))
    complete = meta.get("failed", 0) == 0 and datetime.datetime.fromisoformat(meta["fetched_at"]) >= ready
    return {"grid": grid, "meta": meta, "complete": complete}


def prune(cache_dir, today, keep_days=KEEP_DAYS, kind="aod"):
    oldest = today - datetime.timedelta(days=keep_days)
    stem = KINDS[kind]["stem"]
    for p in Path(cache_dir).glob(f"{stem}_*.npz"):
        m = re.fullmatch(stem + r"_(\d{8})\.npz", p.name)
        if m and datetime.datetime.strptime(m.group(1), "%Y%m%d").date() < oldest:
            p.unlink()


def refresh(cache_dir, dates, s3, sat_configs, domain, budget_s=2100, workers=8, kind="aod"):
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
        have = load_day(cache_dir, date, kind)
        if have is not None and have["complete"]:
            continue
        files = list_files(s3, sat_configs, date, kind)
        if not files:
            continue
        if have is not None and have["meta"].get("n_files") == len(files) and not have["meta"].get("failed"):
            continue                              # nothing new has been posted since it was fetched
        if time.monotonic() - start > budget_s:
            left.append(date)
            continue
        t0 = time.monotonic()
        grid, meta = fetch_day(s3, files, domain, workers, kind)
        save_day(cache_dir, date, grid, meta, kind)
        fetched.append(date)
        print(f"[*] {kind} {date}: {meta['cells']} cells from {len(files)} granules ({time.monotonic() - t0:.0f} s)")
    return fetched, sorted(left)


def good_days(cache_dir, dates, kind="aod"):
    """The cached days to show: those with at least MIN_SHARE of the previous day's granules (the first passes of a new day are
    too few to draw)."""
    out, prev = [], None
    for date in sorted(dates):
        day = load_day(cache_dir, date, kind)
        if day is None or not day["meta"].get("n_files"):
            continue
        n = day["meta"]["n_files"]
        if prev is None or n >= MIN_SHARE * prev:
            out.append(date)
            prev = n
    return out


# ------------------------------------------------------------------------------------------------------------- export
def export_day(cache_dir, date, out_dir, kind="aod"):
    """Write data/<stem>_YYYYMMDD.png (grey level = value x levels, NODATA where none). Returns True when written."""
    from PIL import Image
    day = load_day(cache_dir, date, kind)
    if day is None:
        return False
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    Image.fromarray(day["grid"]).save(Path(out_dir) / f"{KINDS[kind]['stem']}_{date:%Y%m%d}.png", optimize=True)
    return True
