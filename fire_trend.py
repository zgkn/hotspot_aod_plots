"""Daytime VIIRS active fires, cached per UTC day, and the daily hotspot trend for an area.

Source: VIIRS EFIRE (375 m) of NOAA-20 and NOAA-21 on the NOAA open-data buckets (S-NPP is decommissioned). Only daytime fire pixels are kept
(the afternoon passes over the domain: granules that start 03-08 UTC). A day is a UTC day, which is also the local day there.

One fire is usually seen by several passes (two satellites, overlapping orbits), so pixels are de-duplicated: a pixel is dropped
when an earlier granule already has a detection within DEDUPE_RADIUS_M of it. Pixels of one granule never drop each other, so a
fire front that covers many neighbouring pixels still counts as many.
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

EFIRE_PREFIX = "VIIRS_EFIRE_VIIRSI_EDR"
SAT_ORDER = ("NOAA-21", "NOAA-20")               # priority when de-duplicating (the first one keeps its pixels)
DAY_HOURS = range(3, 9)                          # UTC start hours of the granules that hold the afternoon passes
NIGHT_HOURS = range(15, 21)                      # UTC start hours of the night passes (map overlay only, not cached or charted)
COMPLETE_AFTER_HOUR = 10                         # a day is complete when fetched after this UTC hour (the window is posted by then)
DEDUPE_RADIUS_M = 500.0                          # most repeats of a fire in another pass lie within 600 m; 1 km would merge neighbours
FIRE_MASK_KEEP = (8, 9)                          # nominal and high confidence (FP_Confidence is not filled in these files)
MAX_DAYS = 90
KEEP_DAYS = 100                                  # older cache files are deleted
M_PER_DEG = 111195.0
RULES = (f"v1 hours={DAY_HOURS.start}-{DAY_HOURS.stop - 1} mask={','.join(map(str, FIRE_MASK_KEEP))} "
         f"dedupe={DEDUPE_RADIUS_M:g} order={','.join(SAT_ORDER)}")
# Days cached while S-NPP still flew: its pixels came last in the de-duplication, so dropping them gives exactly what a new fetch
# without S-NPP would (see migrate)
LEGACY_RULES = RULES.replace(f"order={','.join(SAT_ORDER)}", "order=NOAA-21,NOAA-20,S-NPP")
REGIONS_FILE = Path(__file__).resolve().parent / "regions.geojson"
# south, west, north, east; same box as model_forecast (the 2 x 2 grid cells around the island)
BOXES = {"Singapore": (1.125, 103.625, 1.625, 104.125)}

_EMPTY = {"lat": np.empty(0, "float32"), "lon": np.empty(0, "float32"), "frp": np.empty(0, "float32"), "mask": np.empty(0, "int8")}


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def latest_day(now=None):
    """The most recent UTC day whose daytime window has started."""
    now = now or utc_now()
    return now.date() if now.hour >= DAY_HOURS.start else now.date() - datetime.timedelta(days=1)


def day_list(end, days):
    return [end - datetime.timedelta(days=i) for i in range(days - 1, -1, -1)]


# ---------------------------------------------------------------------------------------------------------- reading
def read_granule(path, domain, daytime=True):
    """Day (or, with daytime=False, night), good-quality fire pixels inside the domain of one EFIRE granule, and how many were
    dropped for each reason ('night' counts the pixels dropped by the day/night flag, whichever light was asked for)."""
    import xarray as xr
    south, west, north, east = domain
    stats = {"in_domain": 0, "night": 0, "low_conf": 0, "persistent": 0}
    with xr.open_dataset(path) as ds:
        if ds.sizes.get("nfire", 0) == 0 or not all(k in ds.variables for k in ("FP_Latitude", "FP_Longitude", "FP_FireRadiativePower", "FP_Day")):
            return dict(_EMPTY), stats
        v = {k: np.atleast_1d(ds[k].values) for k in ("FP_Latitude", "FP_Longitude", "FP_FireRadiativePower", "FP_Day",
                                                       "FP_FireMask", "FP_PersistentAnomalyCategory") if k in ds.variables}
    lat, lon, frp = v["FP_Latitude"], v["FP_Longitude"], v["FP_FireRadiativePower"]
    ok = (lat >= south) & (lat <= north) & (lon >= west) & (lon <= east) & np.isfinite(frp) & (frp >= 0)
    stats["in_domain"] = int(ok.sum())
    day = ok & (v["FP_Day"] == (1 if daytime else 0))
    stats["night"] = int(ok.sum() - day.sum())
    ok = day
    mask = v.get("FP_FireMask")
    if mask is not None:
        good = ok & np.isin(mask, FIRE_MASK_KEEP)
        stats["low_conf"] = int(ok.sum() - good.sum())
        ok = good
    pers = v.get("FP_PersistentAnomalyCategory")
    if pers is not None:
        good = ok & ~(pers > 0)                  # NaN means no persistent heat source here
        stats["persistent"] = int(ok.sum() - good.sum())
        ok = good
    return {"lat": lat[ok].astype("float32"), "lon": lon[ok].astype("float32"), "frp": frp[ok].astype("float32"),
            "mask": (mask[ok] if mask is not None else np.zeros(int(ok.sum()))).astype("int8")}, stats


def list_granules(s3, config, date, hours=DAY_HOURS):
    """[(start time HHMMSS+tenth, key)] of the window `hours` (UTC start hours) of one UTC day, in time order."""
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=config["bucket"], Prefix=f"{EFIRE_PREFIX}/{date:%Y/%m/%d}/"):
        for obj in page.get("Contents", []):
            m = re.search(r"_s\d{8}(\d{7})", obj["Key"])
            if m and int(m.group(1)[:2]) in hours:
                out.append((m.group(1), obj["Key"]))
    return sorted(out)


# ------------------------------------------------------------------------------------------------------ de-duplicating
def _metres(lat, lon):
    lat = np.asarray(lat, "float64")
    return np.column_stack([np.asarray(lon, "float64") * M_PER_DEG * np.cos(np.radians(lat)), lat * M_PER_DEG])


def dedupe(parts, radius_m=DEDUPE_RADIUS_M):
    """parts: [(sat index, pixel arrays)] in priority order. Returns the unique pixels as one set of arrays plus a 'sat' array.

    A pixel is dropped when an earlier part has a pixel within radius_m; pixels of the same part never drop each other.
    """
    from scipy.spatial import cKDTree
    kept_xy = np.empty((0, 2))
    out = {"lat": [], "lon": [], "frp": [], "mask": [], "sat": []}
    for sat, a in parts:
        n = len(a["lat"])
        if n == 0:
            continue
        xy = _metres(a["lat"], a["lon"])
        keep = np.ones(n, bool)
        if len(kept_xy):
            dist, _ = cKDTree(kept_xy).query(xy, distance_upper_bound=radius_m)
            keep = ~np.isfinite(dist)
        for k in ("lat", "lon", "frp", "mask"):
            out[k].append(a[k][keep])
        out["sat"].append(np.full(int(keep.sum()), sat, "int8"))
        kept_xy = np.vstack([kept_xy, xy[keep]])
    if not out["lat"]:
        return {**_EMPTY, "sat": np.empty(0, "int8")}
    return {k: np.concatenate(v) for k, v in out.items()}


# ------------------------------------------------------------------------------------------------------------ fetching
def fetch_day(s3, sat_configs, date, domain, workers=8, hours=DAY_HOURS, daytime=True):
    """Download the granules of the window `hours` of one UTC day, read them and de-duplicate. Returns (pixel arrays, meta).

    The cache holds the default (daytime) kind; with hours=NIGHT_HOURS, daytime=False it gives the night pixels for the map overlay."""
    jobs = []
    granules = {}
    for sat in SAT_ORDER:
        items = list_granules(s3, sat_configs[sat], date, hours)
        granules[sat] = len(items)
        jobs += [(SAT_ORDER.index(sat), sat_configs[sat]["bucket"], key) for _, key in items]
    tmp = tempfile.mkdtemp(prefix="efire_")
    read_lock = threading.Lock()                 # the HDF5 library is not thread safe, so only the downloads run in parallel

    def work(i, job):
        _, bucket, key = job
        local = os.path.join(tmp, f"{i}.nc")
        try:
            s3.download_file(bucket, key, local)
            with read_lock:
                return i, read_granule(local, domain, daytime), None
        except Exception as e:
            return i, None, f"{os.path.basename(key)}: {e}"
        finally:
            if os.path.exists(local):
                os.remove(local)

    results, failed = {}, []
    try:
        with ThreadPoolExecutor(workers) as ex:
            for fut in as_completed([ex.submit(work, i, j) for i, j in enumerate(jobs)]):
                i, res, err = fut.result()
                if err:
                    failed.append(err)
                else:
                    results[i] = res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    parts = [(jobs[i][0], results[i][0]) for i in sorted(results)]          # priority order: satellite, then time
    raw = {sat: int(sum(len(a["lat"]) for s, a in parts if s == k)) for k, sat in enumerate(SAT_ORDER)}
    dropped = {k: int(sum(results[i][1][k] for i in results)) for k in ("in_domain", "night", "low_conf", "persistent")}
    fires = dedupe(parts)
    meta = {"date": f"{date:%Y-%m-%d}", "fetched_at": utc_now().isoformat(timespec="seconds"), "rules": RULES,
            "granules": granules, "failed": len(failed), "raw": raw, "unique": int(len(fires["lat"])), "dropped": dropped}
    if failed:
        print(f"[!] {date}: {len(failed)} granules failed, for example {failed[0]}")
    return fires, meta


# ------------------------------------------------------------------------------------------------------------- cache
def cache_file(cache_dir, date):
    return Path(cache_dir) / f"fires_{date:%Y%m%d}.npz"


def save_day(cache_dir, date, fires, meta):
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    final = cache_file(cache_dir, date)
    tmp = final.with_name(final.stem + "_tmp.npz")
    np.savez_compressed(tmp, lat=fires["lat"], lon=fires["lon"], frp=fires["frp"], mask=fires["mask"], sat=fires["sat"],
                        meta=np.array(json.dumps(meta)))
    os.replace(tmp, final)


def load_day(cache_dir, date):
    """The cached day as {lat, lon, frp, mask, sat, meta, complete}, or None when missing or made with other rules."""
    path = cache_file(cache_dir, date)
    if not path.exists():
        return None
    try:
        z = np.load(path)
        meta = json.loads(str(z["meta"]))
        if meta.get("rules") != RULES:
            return None
        day = {k: z[k] for k in ("lat", "lon", "frp", "mask", "sat")}
    except Exception as e:
        print(f"[!] Cache file {path.name} unreadable ({e}); it will be fetched again")
        return None
    ready = datetime.datetime.combine(date, datetime.time(COMPLETE_AFTER_HOUR))
    day["meta"] = meta
    day["complete"] = meta.get("failed", 0) == 0 and datetime.datetime.fromisoformat(meta["fetched_at"]) >= ready
    return day


def migrate(cache_dir):
    """Rewrite the cached days made with S-NPP without it: drop its pixels and its counts, keep everything else."""
    n = 0
    for path in sorted(Path(cache_dir).glob("fires_*.npz")):
        try:
            z = np.load(path)
            meta = json.loads(str(z["meta"]))
            if meta.get("rules") != LEGACY_RULES:
                continue
            keep = z["sat"] < len(SAT_ORDER)
            day = {k: z[k][keep] for k in ("lat", "lon", "frp", "mask", "sat")}
        except Exception as e:
            print(f"[!] Cache file {path.name} unreadable ({e}); it will be fetched again")
            continue
        meta.update(rules=RULES, unique=int(keep.sum()),
                    granules={k: v for k, v in meta.get("granules", {}).items() if k in SAT_ORDER},
                    raw={k: v for k, v in meta.get("raw", {}).items() if k in SAT_ORDER})
        save_day(cache_dir, datetime.datetime.strptime(path.stem[6:], "%Y%m%d").date(), day, meta)
        n += 1
    if n:
        print(f"[*] Hotspot cache: {n} days converted to NOAA-20 / NOAA-21 only (S-NPP is decommissioned)")


def prune(cache_dir, today, keep_days=KEEP_DAYS):
    oldest = today - datetime.timedelta(days=keep_days)
    for p in Path(cache_dir).glob("fires_*.npz"):
        m = re.fullmatch(r"fires_(\d{8})\.npz", p.name)
        if m and datetime.datetime.strptime(m.group(1), "%Y%m%d").date() < oldest:
            p.unlink()


def refresh(cache_dir, dates, s3, sat_configs, domain, budget_s=3600, workers=8):
    """Fetch the days that are missing, incomplete or made with other rules, newest first, until the time budget is used.

    Returns (fetched dates, dates left for a later run).
    """
    start = time.monotonic()
    fetched, left = [], []
    migrate(cache_dir)
    today = latest_day()
    for date in sorted(dates, reverse=True):
        if date > today:
            continue
        have = load_day(cache_dir, date)
        if have is not None and have["complete"]:
            continue
        if time.monotonic() - start > budget_s:
            left.append(date)
            continue
        t0 = time.monotonic()
        fires, meta = fetch_day(s3, sat_configs, date, domain, workers)
        if sum(meta["granules"].values()) == 0:
            print(f"[!] {date}: no EFIRE granules published for the daytime window")
            continue
        save_day(cache_dir, date, fires, meta)
        fetched.append(date)
        print(f"[*] {date}: {meta['unique']} unique daytime hotspots from {sum(meta['raw'].values())} detections "
              f"({meta['granules']} granules, {time.monotonic() - t0:.0f} s)")
    return fetched, sorted(left)


# --------------------------------------------------------------------------------------------------------------- areas
class Area:
    """A named place: whole domain, a box or provinces/countries/districts from regions.geojson."""

    def __init__(self, name, kind, geometry, box, group="Areas"):
        self.name, self.kind, self.geometry, self.box, self.group = name, kind, geometry, box, group     # box: south, west, north, east
        self.id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "area"
        import shapely
        shapely.prepare(geometry)

    def contains(self, lat, lon):
        import shapely
        return shapely.contains_xy(self.geometry, np.asarray(lon, "float64"), np.asarray(lat, "float64"))

    def count(self, lat, lon):
        return int(self.contains(lat, lon).sum()) if len(lat) else 0

    def geojson(self):
        from shapely.geometry import mapping
        return {"type": "Feature", "properties": {"name": self.name}, "geometry": mapping(self.geometry)}

    def polygons(self):
        """[[ring, ...], ...] with ring = [[lon, lat], ...] (the first ring of a polygon is its outline, the others are holes)."""
        def flat(g):
            if g.geom_type == "Polygon":
                return [g]
            return [p for part in getattr(g, "geoms", []) for p in flat(part)]
        return [[[[round(x, 4), round(y, 4)] for x, y in ring.coords] for ring in [p.exterior, *p.interiors]] for p in flat(self.geometry)]


def _box_area(box, name, kind="box"):
    from shapely.geometry import box as sbox
    south, west, north, east = box
    return Area(name, kind, sbox(west, south, east, north), box, "Domain" if kind == "domain" else "Boxes")


def _fmt_deg(v, pos, neg):
    return f"{abs(v):g}°{pos if v >= 0 else neg}"


def regions(path=REGIONS_FILE):
    """{name: (shapely geometry, menu group)} of the places in regions.geojson plus the built-in boxes."""
    import shapely
    from shapely.geometry import shape, box as sbox
    out = {}
    if Path(path).exists():
        for f in json.loads(Path(path).read_text())["features"]:
            # simplified outlines can self-touch
            out[f["properties"]["name"]] = (shapely.make_valid(shape(f["geometry"])), f["properties"].get("group", "Provinces"))
    for name, (s_, w, n, e) in BOXES.items():
        out[name] = (sbox(w, s_, e, n), "Boxes")
    return out


def parse_area(text, domain, path=REGIONS_FILE):
    """One place: 'all', a box 'south,west,north,east', a name from regions.geojson, or names joined with '+' (one combined place)."""
    from shapely.ops import unary_union
    t = (text or "").strip()
    if t.lower() in ("", "all", "domain", "whole domain"):
        return _box_area(domain, "Whole domain", "domain")
    parts = [p for p in re.split(r"[,\s]+", t) if p]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        nums = None
    if nums is not None:
        if len(nums) != 4:
            raise ValueError(f"A box needs 4 numbers 'south,west,north,east', got {len(nums)}: {text!r}")
        south, west, north, east = nums
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            raise ValueError(f"Invalid box {text!r}: need south < north within +-90 and west < east within +-180")
        d_s, d_w, d_n, d_e = domain
        if north < d_s or south > d_n or east < d_w or west > d_e:
            raise ValueError(f"Box {text!r} is outside the domain ({d_s}..{d_n} N, {d_w}..{d_e} E)")
        name = f"Box {_fmt_deg(south, 'N', 'S')} to {_fmt_deg(north, 'N', 'S')}, {_fmt_deg(west, 'E', 'W')} to {_fmt_deg(east, 'E', 'W')}"
        return _box_area((south, west, north, east), name)
    known = regions(path)
    lower = {k.lower(): k for k in known}
    chosen = []
    for want in [w.strip() for w in t.split("+") if w.strip()]:
        hit = lower.get(want.lower())
        if hit is None:
            close = [k for k in known if want.lower() in k.lower()]
            if len(close) == 1:
                hit = close[0]
        if hit is None:
            raise ValueError(f"Unknown area {want!r}. Use 'all', a box 'south,west,north,east' or one of: {', '.join(sorted(known))}")
        chosen.append(hit)
    geom = unary_union([known[k][0] for k in chosen])
    w, s, e, n = geom.bounds
    group = known[chosen[0]][1] if len(chosen) == 1 else "Combined"
    return Area(" + ".join(chosen), "region", geom, (s, w, n, e), group)


def parse_areas(text, domain, path=REGIONS_FILE):
    """Several places separated by '|' (or ';' or a new line); each gets its own series. Returns [Area]."""
    items = [x.strip() for x in re.split(r"[|;\n]+", text or "") if x.strip()] or ["all"]
    areas, seen = [], set()
    for item in items:
        area = parse_area(item, domain, path)
        if area.name not in seen:
            seen.add(area.name)
            areas.append(area)
    return areas


# ------------------------------------------------------------------------------------------------------------ series
def build_series(cache_dir, dates, areas):
    """{area id: rows}; one row per day: date, count (None when the day is not in the cache), complete flag and the granule counts."""
    out = {a.id: [] for a in areas}
    for d in dates:
        day = load_day(cache_dir, d)                         # every day file is read once for all the areas
        for a in areas:
            if day is None:
                out[a.id].append({"date": d, "count": None, "complete": False, "granules": {}, "unique_all": None})
            else:
                out[a.id].append({"date": d, "count": a.count(day["lat"], day["lon"]), "complete": day["complete"],
                                  "granules": day["meta"]["granules"], "unique_all": day["meta"]["unique"]})
    return out
