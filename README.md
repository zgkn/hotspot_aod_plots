# hotspot_aod_plots

Map of Southeast Asia (90-142E, 11S-28N) with the **aerosol optical depth** (VIIRS AOD550, NOAA-20 / NOAA-21), the
**daytime fire hotspots** of the latest day, any single date or a date range you choose, and a **trend panel** with the daily number
of daytime hotspots in an area over the past days. Built by `.github/workflows/main.yml` (four times a day and on demand) and
published to GitHub Pages.

## Reading the page

The **Date** control at the top left chooses what the hotspot and AOD layers show: a **Single date** (the latest day when the page opens;
pick any other day with the date box) or a **Date range** (From / To, up to 31 days). For a range the map draws every daytime detection
of its days (a fire seen on two days is drawn twice, smaller circles when there are very many) and the **mean AOD** of its days that
have an AOD grid (the legend scale follows the data on screen); the text under the buttons says how many days each came from. On a phone
the control is a small pill with the choice on it: tap it to open it. Hotspots go back as far as the data (up to 31 days are offered),
AOD for the last 30 days; a date without AOD data shows no AOD. Tapping a hotspot shows its popup, with links to open the spot in
Google Maps (pin or satellite view). The Night layer always shows the newest night.

The **Cloud mask** layer (in the layer menu, off until ticked) is a white veil: the share of the satellite pixels that were cloudy or
probably cloudy (VIIRS JRR cloud mask, NOAA-20 and NOAA-21, the same afternoon passes as the AOD), averaged on a 0.1 degree grid; the
whiter, the cloudier. It follows the date control: the chosen day, or the mean of the days of a range. Cloud grids are kept for the last
30 days and are only downloaded by the page while the layer is on. Use it to see where no fire or AOD could be seen.

## Choosing the days and the places

Run the workflow by hand (Actions > Generate Hotspots Map > Run workflow) and fill in:

| input | meaning |
|---|---|
| `days` | 1-90 days counted back from the latest daytime day (UTC) |
| `area` | the places, separated by `\|`, each with its own chart: `Riau`, `Jambi`, `South Sumatra`, `West Kalimantan`, `Central Kalimantan`, `South Kalimantan`, `Cambodia`, `Myanmar`, `Viet Nam`, `Thailand`, `Lao PDR`, `Kota Tinggi (Johor)`, `Mersing (Johor)`, `Singapore`, `all`, or a box `south,west,north,east` such as `-3,100,2,106`. Names joined with `+` make one combined place (`Riau+Jambi`). |

Example: `Central Kalimantan|West Kalimantan|South Kalimantan|Jambi|South Sumatra|Riau`.

Leave an input blank to keep the last choice (the scheduled runs use it too). The choice is remembered only when the run is on the
default branch. A new place name is one entry in `regions.geojson`; any other place can be given as a box.

## What is counted

* VIIRS 375 m active fire (EFIRE) pixels, **daytime only** (`FP_Day` = 1; the afternoon passes, granules starting 03-08 UTC),
  with fire mask nominal or high (`FP_FireMask` 8 or 9). Pixels flagged as persistent heat sources are removed.
* **Unique**: one fire is usually seen by several passes. A pixel is dropped when an earlier granule (NOAA-21, then NOAA-20, then
  in time order; S-NPP is decommissioned and no longer used) already has a pixel within **500 m**; pixels of the same granule never drop each other, so a fire
  front that covers many pixels still counts as many. The radius is `DEDUPE_RADIUS_M` in `fire_trend.py`.
* The map also has a **Night hotspots** layer (night pixels of the newest 15-20 UTC passes, i.e. 23:00-04:00 Singapore time, with the same
  quality filter and de-duplication; the name says "still being posted" until about 22:00 UTC). It is fetched on every run, is not cached and is not part of the chart.
* A day is a UTC day. The latest day is partial until all its passes are posted (about 10:00 UTC) and is drawn lighter.
* Fire detection is blocked by cloud and thick smoke, so a drop in the count can be cloud rather than fewer fires. A yellow dot
  marks a day on which a satellite had far fewer granules than usual.

## Cache

`data` branch: one small file per UTC day with the daytime fire pixels of the whole domain (`fires_YYYYMMDD.npz`, about 40 KB),
the last 100 days, and one with the day's AOD averaged on a 0.1 degree grid (`aod_YYYYMMDD.npz`), the last 40 days, kept as a single
force-pushed commit. The page reads the days it needs from `data/fires_YYYYMMDD.json` and `data/aod_YYYYMMDD.png` next to it
(written by every run). The AOD days are downloaded newest first within their own time budget (`HOTSPOT_AOD_BUDGET_MIN`, 35 minutes;
about 100 s per day). The cloud-mask grids (`cloud_YYYYMMDD.npz`, the last 40 days) work the same way but are heavier (about 590
granules, 22 GB, 3 minutes a day), so they have their own budget (`HOTSPOT_CLOUD_BUDGET_MIN`, 40 minutes, about 14 days per run). A run only downloads the days that are missing or were fetched before
their passes were all posted (about 30 s per day, so a first run for 90 days takes about 50 minutes). If a run runs out of its
time budget (`HOTSPOT_BUDGET_MIN`) the older days are fetched on the next run and shown as missing until then.

## Files

* `script.py` - builds the map and the day files for the page
* `fire_trend.py` - fire download, filtering, de-duplication, per-day cache, areas, daily series
* `aod_grid.py` - AOD and cloud-mask download, daily 0.1 degree grids, per-day cache
* `period_panel.py` - the Date control and the hotspot / AOD / cloud layers (canvas, JavaScript)
* `trend_panel.py` - the trend panel (inline SVG)
* `regions.geojson` - area outlines from `model_forecast`: Natural Earth (public domain) and geoBoundaries gbOpen ADM2 (CC BY 4.0)
