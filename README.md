# Vienna for Subway Builder — build & publish guide

Everything needed to build a custom Vienna map for Subway Builder and publish it
to the Railyard registry. Coverage is all 23 districts inside the Stadtgrenze,
extended south-east to take in Flughafen Wien-Schwechat, with special demand for
the airport, universities, hospitals and major venues.

Vienna would be the **first Austrian map in the registry** — there are 309 maps
published and none for AT. The city code `VIEN` is used here; `VIE` and `WIEN` are
also free.

---

## How the pieces fit together

| Piece | What it is |
|---|---|
| **Subway Builder** | the game. Has a JavaScript mod API that can register custom cities. |
| **Railyard** | the map/mod manager. Installs map ZIPs, generates the mod that registers them with the game, and runs a local tile server during play. |
| **The registry** | a GitHub repo of *metadata only*. You host the ZIP yourself (GitHub Releases); the registry stores the manifest and points at it. |
| **depot** | the official Python map-making library. Turns OSM + Overture data into the tiles, buildings index and roads the game needs. |

A finished map is a ZIP with all files at the archive root:

```
VIEN.pmtiles                 vector tiles the game draws
VIEN_foundations.pmtiles     building + ocean foundation layers
buildings_index.bin.gz       building collision geometry
roads.geojson                street network for in-game routing
runways_taxiways.geojson     Schwechat runways
demand_data.json             who commutes from where to where
config.json                  name, code, bbox, population, camera start
```

`depot` produces the first five. `demand_data.json` is the interesting part and
the one nobody can generate for you automatically outside the US — scripts 2 and
3 here do it for Vienna.

---

## Before you start

**Time:** about 30 minutes of your attention, plus 4–8 hours of the machine
grinding away unattended.

**Disk:** ~25 GB free inside WSL. **RAM:** 16 GB comfortable, 8 GB workable if
you drop `ncores` and `RAM` in `config/vienna.json`.

**Two locations, on purpose:**

| Where | What lives there |
|---|---|
| `C:\Users\<you>\SubwayBuilder\` | this project — scripts, config, the finished ZIP |
| `~/vienna/` inside WSL | OSM downloads and build intermediates (~20 GB, disposable) |

Build in WSL's own filesystem, not `/mnt/c` — disk I/O across the Windows
boundary is several times slower and this pipeline writes a *lot* of
intermediate files.

---

## Step 1 — Set up WSL

If you already have Ubuntu in WSL, skip to step 2.

Open PowerShell **as administrator**:

```powershell
wsl --install -d Ubuntu
```

Reboot when it asks. On first launch Ubuntu asks for a username and password —
this is a Linux account, unrelated to your Windows login. Remember the password;
you'll need it for `sudo`.

Then give WSL enough memory. Create `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
memory=12GB
processors=8
swap=8GB
```

and run `wsl --shutdown` in PowerShell to apply it. Adjust to your machine —
leave at least 4 GB and a couple of cores for Windows.

**Docker Desktop** is needed for the routing step. Install it, then in
Settings → Resources → WSL integration, enable it for your Ubuntu distro.

---

## Step 2 — Install the toolchain

Copy this project into WSL and run the installer:

```bash
mkdir -p ~/vienna ~/vienna-subway-builder
cp -r /mnt/c/Users/user/SubwayBuilder/{scripts,config,docs,README.md} ~/vienna-subway-builder/
cd ~/vienna-subway-builder
ls                     # should list: config docs README.md scripts
bash scripts/00_install_toolchain.sh
```

It installs, skipping anything already present:

- **apt packages** — build tools, `osmium-tool`, `sqlite3`, `jq`, Java 21
- **Node** via nvm, then **mapshaper**
- **tippecanoe** and **tile-join**, built from source (a few minutes)
- **pmtiles** CLI and **planetiler.jar** from their GitHub releases
- **Miniforge** (conda) for the Python environment

It ends with a checklist. Every line must say OK before you continue — `depot`
refuses to run if any of its CLI dependencies are missing.

Then create the Python environment and install depot:

```bash
source ~/.bashrc
cd ~
git clone https://github.com/Subway-Builder-Modded/depot.git
cd depot
conda env create -f environment.yml
conda activate depot
pip install .
```

`environment.yml` pins Python 3.13.9 and the exact package versions depot is
tested against. Don't substitute your own — geopandas/shapely/duckdb version
drift is the most common source of confusing failures here.

---

## Step 3 — Get the demand data

Two downloads, both free, one of them manual.

**a) OSM extract** — handled automatically by script 1, which pulls
`austria-latest.osm.pbf` (~800 MB) from Geofabrik.

**b) Statistik Austria population grid** — one manual download:

```bash
cd ~/vienna/data
wget "https://www.statistik.at/gs-inspire/www/inspire2/download/daten/pd_popreg_100m_7767c33f-302c-11e3-beb4-0000c1ab0db6.zip"
unzip pd_popreg_100m_*.zip
ls
```

This is STATISTIK AUSTRIA's INSPIRE **"Wohnbevölkerung nach 100m ETRS-LAEA
Raster für Österreich"** — register-based resident counts, one measured value per
100 m cell, reference date 1 January 2026. It's free and it's the best resident
data available for Austria: each cell carries its own count, which is the top
tier of the registry's quality rubric.

What you get is a single 320 MB `.gml`. Don't try to open it with `ogrinfo` or
`ogr2ogr` — the whole file is *one* INSPIRE `pd:StatisticalDistribution` feature
with ~589,000 cell records nested inside, so GDAL returns one row and spends ten
minutes doing it. Stream it out instead:

```bash
cd ~/vienna-subway-builder
python scripts/01b_extract_population.py
```

That takes a couple of minutes and writes
`data/statistik_austria_population_grid.csv`, which script 2 picks up
automatically. It ends with sanity checks — expect ~589,000 cells, ~9.2M
residents for Austria and ~2M inside the Vienna box. If those numbers come out
wrong, stop and investigate before building anything on top of them.

> **Not** `data.statistik.gv.at/web/meta.jsp?dataset=OGDEXT_RASTER_1`. That
> dataset is the grid *geometry* only — cell outlines with no counts attached.

**Jobs data: don't go looking, it isn't free.** Statistik Austria's workplace
rasters (`Arbeitsstätten nach ÖNACE 2008`, employment by sector, commuting
matrices) are part of the priced regional data offering — €354 base fee per
order plus the data cost, with only 10 km and coarser grids partly free. So
script 2 falls back to distributing Vienna's ~900k jobs across OSM office,
retail, industrial and institutional features using per-type employment
densities. That caps the listing at `medium-quality`, which is the honest tag.

If you ever do buy the workplace raster, drop it in `~/vienna/data` as
`jobs_grid.gpkg` (or anything matching `*arbeitsstaett*`) and script 2 picks it
up automatically — it's the single biggest quality lever on the map, since
workplace data carries 50% of the rubric's weight.

---

## Step 4 — Build the map geometry

```bash
conda activate depot
cd ~/vienna-subway-builder
python scripts/01_build_map.py
```

This runs depot's five stages: clip Austria to the Vienna bbox, fetch Overture
building footprints and build the collision index, extract roads and the
Schwechat runways, generate the vector tiles, then add labels. **1.5–4 hours.**

Watch for the `check_labels` output partway through. It lists which OSM `place`
values actually occur in the bbox and how often. Vienna's 23 Bezirke are tagged
`suburb`, the Grätzl are `quarter`/`neighbourhood`, and the towns just over the
Lower Austrian border are `town`/`village`. If the label density looks wrong when
you play-test, edit `cities` / `suburbs` / `neighborhoods` in
`config/vienna.json` and re-run only `add_labels`.

---

## Step 5 — Generate the demand

```bash
python scripts/02_base_demand.py     # ~10-20 min
python scripts/03_special_demand.py  # 30-90 min, needs Docker running
```

Script 2 builds the base: residents from the Statistik Austria grid, jobs from
the workplace grid or the OSM dasymetric, and commute flows from a
doubly-constrained gravity model. It aggregates the fine grid up to roughly
5,000 demand points, placing each point at the activity-weighted centroid of its
cells rather than the block centre, so points land on the built-up part of the
cell.

It prints two numbers worth reading:

- **mean modelled commute** — should land around 7–9 km straight-line. Higher
  means `gravity_beta` is too low (people commuting unrealistically far); lower
  means it's too high. Nudge it in `config/vienna.json` and re-run; the script
  takes under a minute once the OSM extract is cached.
- **intra-zone share** — the fraction who work in their own cell. ~8% is
  reasonable for a city at this grain.

Script 3 layers on the special demand from `config/vienna.json`, then starts a
local OSRM server in Docker and computes a real driving time and distance for
every single commute. That routing is what makes the game's "would this person
take the train instead of driving?" decision meaningful, and it's the slowest
part — the first run spends 10–25 minutes just contracting the road graph.

It finishes by writing `config.json` and a `description.md` you can paste
straight into the registry submission.

### Tuning the special demand

`config/vienna.json` is where the map's character lives. Every entry is a
`[longitude, latitude]` plus a daily headcount:

- **`airports`** — Schwechat modelled at 30,000 daily ground-side passengers plus
  20,000 staff. The airport handled about 32 million passengers in 2025; only a
  fraction of daily passengers are a transit demand worth modelling, which is
  why the number is well below 32M/365.
- **`universities`** — 18 campuses split by site rather than by institution,
  because Uni Wien at Universitätsring and the Biologiezentrum in the 3rd generate
  completely different trips. `perc_oncampus` is 0 throughout: Austria has no
  US-style dorm system, students commute.
- **`hospitals`** — AKH dominates at 26,000 daily; the Gemeindespitäler follow.
- **`venues`** — Ernst-Happel, Allianz Stadion, Generali-Arena, Stadthalle,
  Messe, Prater, Schönbrunn, the big shopping centres, Mariahilfer Straße.
- **`outside_connections`** — seven corridors at the map edge (Südbahn,
  Ostbahn, Westbahn, Franz-Josefs, Nordbahn, Pressburger, Südost) carrying the
  ~250,000 people who commute into Vienna daily from Niederösterreich and
  Burgenland. Without these the map badly under-models the Gürtel and the
  radial S-Bahn corridors.

The coordinates are good to roughly a block. Before publishing, spot-check the
big ones on <https://www.openstreetmap.org> and nudge any that sit in the wrong
courtyard — a hospital entrance placed on the wrong side of a building changes
where players want a station.

---

## Step 6 — Package and test locally

```bash
SB_DELIVER_DIR=/mnt/c/Users/user/SubwayBuilder python scripts/04_package.py
```

Produces `VIEN-v1.0.0.zip` and `submission_values.json` with the exact manifest
numbers for the registry issue, and copies both to your Windows folder.

Now play it before you publish. In Railyard, install from the local ZIP, launch
Subway Builder and check:

- The camera opens on Stephansplatz at a sensible zoom.
- Buildings render and you can't tunnel through them.
- Demand hotspots look like Vienna — the Innere Stadt, the Gürtel, Favoriten,
  Donaustadt, the airport, the university cluster in the 9th.
- Build a short U-Bahn line along a real corridor (say Karlsplatz–Stephansplatz–
  Schwedenplatz) and see whether ridership is plausible rather than absurd.

If demand feels flat or wildly concentrated, that's `gravity_beta` — adjust and
re-run steps 2–3. The map geometry from step 1 doesn't need rebuilding.

---

## Step 7 — Publish to Railyard

See **[docs/railyard-submission.md](docs/railyard-submission.md)** for the full
field-by-field walkthrough. In short:

1. Create a public GitHub repo (`<you>/sb-vienna-map`), push this project, and
   attach `VIEN-v1.0.0.zip` to a release tagged `v1.0.0`.
2. Take 3–4 in-game screenshots.
3. Open a **Publish a New Map** issue on
   <https://github.com/Subway-Builder-Modded/registry>, filling it from
   `submission_values.json`.
4. CI validates and opens a PR automatically. You'll then be asked the
   **data-quality questions** — a required step; a maintainer has to confirm your
   answers before it merges. Be straight about the methodology: the rubric
   explicitly rewards honest reporting of grain and dasymetric method, and
   claiming high-quality for an OSM-derived jobs layer will get corrected.

---

## What's in this project

```
config/vienna.json          every tunable: bbox, filters, labels, all special demand
scripts/00_install_toolchain.sh
scripts/01_build_map.py     depot MapGen -> tiles, buildings, roads
scripts/02_base_demand.py   Statistik Austria + gravity model -> demand_data.json
scripts/03_special_demand.py  special demand + OSRM routing + config.json
scripts/04_package.py       ZIP + manifest numbers
docs/railyard-submission.md publishing walkthrough
```

## If something breaks

| Symptom | Cause |
|---|---|
| depot exits immediately complaining about a missing program | one of the CLI tools isn't on PATH or isn't executable. Re-run `00_install_toolchain.sh` and check the final checklist. |
| mapshaper "heap out of memory" | raise `RAM` in `config/vienna.json` (it's the GB handed to Node). |
| tiles exceed the per-tile size limit | raise `building_tile_filter_size` or `building_tile_simplification`. |
| `calculate_routes` hangs or errors | Docker isn't running, or the OSRM container didn't start. `docker ps` should show a container named `VIEN`; `docker start VIEN` if it's stopped. |
| Grid CSV parse error | open the CSV and check the id column really contains ETRS89-LAEA cell ids; the script prints which columns it chose. |

## Sources

- Subway Builder modding docs — <https://www.subwaybuilder.com/docs/guides/custom-cities>
- depot — <https://github.com/Subway-Builder-Modded/depot>
- Railyard — <https://github.com/ByteOfBacon/railyard>
- Registry + data-quality rubric — <https://github.com/Subway-Builder-Modded/registry>
- Statistik Austria open data — <https://data.statistik.gv.at/>
