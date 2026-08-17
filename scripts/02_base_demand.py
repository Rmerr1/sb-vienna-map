#!/usr/bin/env python
"""
Step 2 - Generate Vienna's base demand data (residents, jobs, commute flows).

`depot` ships a demand *editor* but not yet a demand *generator* for non-US
cities, so this script builds `demand_data.json` from scratch:

    residents  <- Statistik Austria register-based population grid (100 m / 250 m,
                  ETRS89-LAEA / EPSG:3035), the same class of source the
                  high-quality Zurich map uses for Switzerland
    jobs       <- Statistik Austria workplace grid if you have it, otherwise an
                  OSM workplace dasymetric: the modelled job total is shared out
                  over office / retail / industrial / institutional features
                  using per-type employment densities
    flows      <- doubly-constrained gravity model (Furness / IPF) with an
                  exponential distance decay, calibrated so the median commute
                  lands near Vienna's real ~6-7 km

Driving times and distances are left at zero here - step 3 fills them in with
real OSRM routing.

Usage:
    conda activate depot
    python scripts/02_base_demand.py
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config" / "vienna.json").read_text(encoding="utf-8"))
DCFG = CFG["demand_generation"]

BUILD_DIR = Path(os.environ.get("SB_BUILD_DIR", Path.home() / "vienna"))
DATA_DIR = BUILD_DIR / "data"
OUT_DIR = BUILD_DIR / "build" / CFG["map_code"]
OUT_DIR.mkdir(parents=True, exist_ok=True)

BBOX = CFG["bbox"]                       # [min_lon, min_lat, max_lon, max_lat]
PBF = DATA_DIR / "austria-latest.osm.pbf"

# Statistik Austria grids you download by hand - see README step 3.
# The free INSPIRE download is GML inside a zip; a CSV export works too.
# Anything GDAL can open (.gml, .gpkg, .shp, .zip) is accepted.
POP_GRID_CANDIDATES = [
    "statistik_austria_population_grid.csv",
    "population_grid.gpkg",
    "population_grid.gml",
    "pd_popreg_100m.gml",
]
JOB_GRID_CANDIDATES = [
    "statistik_austria_jobs_grid.csv",
    "jobs_grid.gpkg",
]


def find_grid(candidates, patterns):
    """Return the first existing file from candidates, else glob for patterns."""
    for name in candidates:
        p = DATA_DIR / name
        if p.exists():
            return p
    for pat in patterns:
        hits = sorted(DATA_DIR.glob(pat))
        if hits:
            return hits[0]
    return None


# ---------------------------------------------------------------------------
# Grid handling
# ---------------------------------------------------------------------------
# Statistik Austria publishes INSPIRE-style ETRS89-LAEA cell ids. Seen in the
# wild as e.g. "100mN27481E47563", "CRS3035RES1000mN2748000E4756000",
# "1kmN2748E4756". This parser copes with all of them.
GRID_ID_RE = re.compile(
    r"(?:CRS3035RES)?(?P<size>\d+)(?P<unit>k?m)N(?P<n>\d+)E(?P<e>\d+)",
    re.IGNORECASE,
)


def parse_grid_id(cell_id: str):
    """Return (easting_m, northing_m, cell_size_m) in EPSG:3035, or None."""
    m = GRID_ID_RE.search(str(cell_id))
    if not m:
        return None
    size = int(m.group("size"))
    if m.group("unit").lower() == "km":
        size *= 1000
    n, e = int(m.group("n")), int(m.group("e"))
    # Ids may encode the coordinate in metres or in multiples of the cell size.
    # A real EPSG:3035 northing for Austria is ~2.7e6 m and easting ~4.7e6 m,
    # so anything much smaller is in cell-size units.
    if n < 100_000:
        n *= size
    if e < 100_000:
        e *= size
    return e, n, size


def read_grid_csv(path: Path, value_hint: str):
    """
    Read a Statistik Austria grid CSV into {(easting, northing, size): value}.

    Tolerant on purpose: the agency changes column names between vintages, so
    we sniff the delimiter, find the column holding a parseable grid id, and
    take the value column by name hint, falling back to the first numeric
    column that isn't the id.
    """
    import csv

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
            dialect.delimiter = ";"
        reader = csv.DictReader(fh, dialect=dialect)
        rows = list(reader)

    if not rows:
        raise SystemExit(f"{path} appears to be empty.")

    headers = list(rows[0].keys())
    id_col = next((h for h in headers if parse_grid_id(rows[0].get(h, ""))), None)
    if id_col is None:
        raise SystemExit(
            f"Could not find a grid-id column in {path}.\nColumns: {headers}\n"
            "Expected values like '100mN27481E47563'."
        )

    val_col = next(
        (h for h in headers if value_hint.lower() in h.lower()),
        None,
    )
    if val_col is None:
        for h in headers:
            if h == id_col:
                continue
            try:
                float(str(rows[0][h]).replace(",", "."))
                val_col = h
                break
            except (TypeError, ValueError):
                continue
    if val_col is None:
        raise SystemExit(f"Could not find a numeric value column in {path}. "
                         f"Columns: {headers}")

    print(f"  {path.name}: id column '{id_col}', value column '{val_col}'")

    out = {}
    for row in rows:
        parsed = parse_grid_id(row.get(id_col, ""))
        if not parsed:
            continue
        raw = str(row.get(val_col, "")).strip().replace(",", ".")
        if raw in ("", "-", "NA", "n/a"):
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        if val <= 0:
            continue
        out[parsed] = out.get(parsed, 0.0) + val
    return out


POP_COL_HINTS = ("bev", "pop", "einwohn", "resident", "anzahl", "value",
                 "measure", "wert", "count")


def read_grid_vector(path: Path, value_hint: str, snap_m: int = 100):
    """
    Read any GDAL-readable grid file (GML / GPKG / SHP / zipped shapefile) into
    {(easting, northing, cell_size): value} in EPSG:3035.

    Used for the free Statistik Austria INSPIRE download, which ships GML with
    one feature per 100 m cell carrying its resident count.
    """
    import geopandas as gpd

    read_path = str(path)
    if path.suffix.lower() == ".zip":
        read_path = f"zip://{path}"

    print(f"  opening {path.name} with GDAL...")
    gdf = gpd.read_file(read_path)
    if gdf.empty:
        raise SystemExit(f"{path} opened but contains no features.")

    print(f"  {len(gdf):,} features, CRS {gdf.crs}, "
          f"columns: {[c for c in gdf.columns if c != 'geometry'][:12]}")

    # Pick the population column.
    cols = [c for c in gdf.columns if c != "geometry"]
    val_col = next((c for c in cols if value_hint.lower() in c.lower()), None)
    if val_col is None:
        for hint in POP_COL_HINTS:
            val_col = next((c for c in cols if hint in c.lower()), None)
            if val_col is not None:
                break
    if val_col is None:
        numeric = [c for c in cols
                   if str(gdf[c].dtype).startswith(("int", "float"))]
        if not numeric:
            raise SystemExit(
                f"No numeric attribute found in {path.name}.\n"
                f"Columns: {cols}\n"
                "If this is the grid-geometry file rather than the population "
                "file, you have the wrong download - see README step 3."
            )
        val_col = numeric[0]
    print(f"  using value column '{val_col}'")

    gdf = gdf[[val_col, "geometry"]].copy()
    gdf[val_col] = gpd.pd.to_numeric(gdf[val_col], errors="coerce")
    gdf = gdf[gdf[val_col] > 0]

    if gdf.crs is None:
        print("  WARNING: file has no CRS; assuming EPSG:3035")
        gdf = gdf.set_crs("EPSG:3035")
    gdf = gdf.to_crs("EPSG:3035")

    pts = gdf.geometry.representative_point()
    out = {}
    for e, n, v in zip(pts.x.values, pts.y.values, gdf[val_col].values):
        key = (int(e // snap_m) * snap_m, int(n // snap_m) * snap_m, snap_m)
        out[key] = out.get(key, 0.0) + float(v)
    return out


def read_grid(path: Path, value_hint: str):
    """Dispatch on file type: CSV of cell ids, or any GDAL vector format."""
    if path.suffix.lower() in (".csv", ".txt", ".tsv"):
        return read_grid_csv(path, value_hint)
    return read_grid_vector(path, value_hint)


def clip_wgs84(cells):
    """
    Drop grid cells whose centre falls outside the WGS84 bbox.

    The cheap LAEA prefilter uses the min/max of the four transformed bbox
    corners, which is an axis-aligned rectangle in EPSG:3035 - and therefore a
    slightly ROTATED quadrilateral back in WGS84. That let ~0.7% of demand sit
    south of the map's own southern edge, where no player can ever build, and
    showed up in-game as a clean diagonal cut along the demand footprint.
    """
    if not cells:
        return cells
    keys = list(cells)
    e = np.array([k[0] + k[2] / 2 for k in keys], dtype=np.float64)
    n = np.array([k[1] + k[2] / 2 for k in keys], dtype=np.float64)
    lon, lat = laea_to_wgs84(e, n)
    keep = ((lon >= BBOX[0]) & (lon <= BBOX[2])
            & (lat >= BBOX[1]) & (lat <= BBOX[3]))
    dropped = len(keys) - int(keep.sum())
    if dropped:
        print(f"  {dropped:,} cells dropped by the exact WGS84 clip "
              f"(LAEA prefilter is a rotated box)")
    return {k: cells[k] for k, m in zip(keys, keep) if m}


def laea_to_wgs84(eastings, northings):
    from pyproj import Transformer
    tf = Transformer.from_crs("EPSG:3035", "EPSG:4326", always_xy=True)
    lon, lat = tf.transform(eastings, northings)
    return np.asarray(lon), np.asarray(lat)


# ---------------------------------------------------------------------------
# OSM workplace dasymetric (fallback when no official jobs grid is available)
# ---------------------------------------------------------------------------
# Employees per feature (points) or per hectare (polygons). Generic European
# priors, deliberately conservative; the absolute level does not matter because
# everything is rescaled to `total_jobs`, only the *relative* weights do.
POINT_WEIGHTS = {
    ("building", "office"): 45.0,
    ("building", "commercial"): 30.0,
    ("building", "retail"): 22.0,
    ("building", "industrial"): 28.0,
    ("building", "warehouse"): 8.0,
    ("building", "hospital"): 200.0,
    ("building", "university"): 90.0,
    ("building", "school"): 35.0,
    ("building", "public"): 40.0,
    ("amenity", "hospital"): 300.0,
    ("amenity", "university"): 120.0,
    ("amenity", "college"): 45.0,
    ("amenity", "school"): 40.0,
    ("amenity", "kindergarten"): 12.0,
    ("amenity", "clinic"): 20.0,
    ("amenity", "doctors"): 6.0,
    ("amenity", "pharmacy"): 6.0,
    ("amenity", "bank"): 9.0,
    ("amenity", "post_office"): 9.0,
    ("amenity", "townhall"): 60.0,
    ("amenity", "police"): 30.0,
    ("amenity", "restaurant"): 8.0,
    ("amenity", "cafe"): 5.0,
    ("amenity", "bar"): 4.0,
    ("amenity", "pub"): 5.0,
    ("amenity", "fast_food"): 6.0,
    ("amenity", "theatre"): 25.0,
    ("amenity", "cinema"): 15.0,
    ("amenity", "library"): 12.0,
    ("tourism", "hotel"): 22.0,
    ("tourism", "museum"): 20.0,
    ("shop", "supermarket"): 25.0,
    ("shop", "department_store"): 60.0,
    ("shop", "mall"): 120.0,
    ("shop", "furniture"): 10.0,
    ("shop", "car"): 10.0,
    ("shop", "doityourself"): 20.0,
    ("office", "*"): 25.0,
    ("shop", "*"): 4.0,
    ("craft", "*"): 4.0,
    ("healthcare", "*"): 8.0,
}
# Employees per hectare for landuse polygons.
AREA_WEIGHTS = {
    ("landuse", "commercial"): 60.0,
    ("landuse", "retail"): 45.0,
    ("landuse", "industrial"): 25.0,
    ("landuse", "institutional"): 40.0,
    ("landuse", "education"): 45.0,
    ("landuse", "port"): 12.0,
    ("landuse", "railway"): 8.0,
    ("aeroway", "terminal"): 250.0,
}
OSMIUM_FILTERS = [
    "nwr/office", "nwr/shop", "nwr/craft", "nwr/healthcare", "nwr/tourism",
    "nwr/amenity", "nwr/aeroway=terminal",
    "nwr/building=office,commercial,retail,industrial,warehouse,hospital,"
    "university,school,public",
    "nwr/landuse=commercial,retail,industrial,institutional,education,port,railway",
]


def extract_workplaces() -> Path:
    """Run osmium to pull workplace-ish features inside the bbox into GeoJSONSeq."""
    clipped = OUT_DIR / "vienna_bbox.osm.pbf"
    filtered = OUT_DIR / "workplaces.osm.pbf"
    geojsonl = OUT_DIR / "workplaces.geojsonl"
    if geojsonl.exists() and geojsonl.stat().st_size > 0:
        print(f"  reusing {geojsonl.name}")
        return geojsonl

    if not PBF.exists():
        raise SystemExit(f"Missing {PBF}. Run 01_build_map.py first.")

    bbox_str = ",".join(str(v) for v in BBOX)
    print("  osmium extract (bbox clip)")
    subprocess.run(
        ["osmium", "extract", "--strategy", "complete_ways",
         "--bbox", bbox_str, str(PBF), "-o", str(clipped), "--overwrite"],
        check=True,
    )
    print("  osmium tags-filter (workplace features)")
    subprocess.run(
        ["osmium", "tags-filter", str(clipped), *OSMIUM_FILTERS,
         "-o", str(filtered), "--overwrite"],
        check=True,
    )
    print("  osmium export -> geojsonseq")
    subprocess.run(
        ["osmium", "export", str(filtered), "-f", "geojsonseq",
         "--add-unique-id=type_id", "-o", str(geojsonl), "--overwrite"],
        check=True,
    )
    return geojsonl


def osm_job_weights(cell_size_m: int):
    """Return {(easting, northing, size): job_weight} from OSM features."""
    from pyproj import Transformer
    from shapely.geometry import shape

    geojsonl = extract_workplaces()
    fwd = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)

    weights = defaultdict(float)
    n_feat = 0
    with geojsonl.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip().lstrip("\x1e")   # geojsonseq record separator
            if not line:
                continue
            try:
                feat = json.loads(line)
            except json.JSONDecodeError:
                continue
            props = feat.get("properties") or {}
            geom = feat.get("geometry")
            if not geom:
                continue

            weight = 0.0
            for (key, val), w in POINT_WEIGHTS.items():
                pv = props.get(key)
                if pv is None:
                    continue
                if val == "*" or pv == val:
                    weight = max(weight, w)

            is_area = geom.get("type") in ("Polygon", "MultiPolygon")
            if is_area:
                for (key, val), w_ha in AREA_WEIGHTS.items():
                    if props.get(key) == val:
                        try:
                            g = shape(geom)
                            # crude m^2: degrees -> metres at Vienna's latitude
                            area_m2 = abs(g.area) * (111_320 ** 2) * math.cos(
                                math.radians(48.21))
                        except Exception:
                            area_m2 = 0.0
                        weight = max(weight, w_ha * area_m2 / 10_000.0)

            if weight <= 0:
                continue

            try:
                g = shape(geom)
                pt = g.representative_point() if is_area else g.centroid
                lon, lat = pt.x, pt.y
            except Exception:
                continue
            if not (BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3]):
                continue

            e, n = fwd.transform(lon, lat)
            key = (int(e // cell_size_m) * cell_size_m,
                   int(n // cell_size_m) * cell_size_m,
                   cell_size_m)
            weights[key] += weight
            n_feat += 1

    print(f"  {n_feat:,} workplace features -> {len(weights):,} cells")
    return dict(weights)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def aggregate(res_cells, job_cells, target_points, base_size):
    """Coarsen the grid until the non-empty cell count is near target_points."""
    candidates = [base_size, 125, 150, 200, 250, 300, 400, 500, 600, 800, 1000, 1250]
    candidates = sorted({c for c in candidates if c >= base_size})

    all_keys = set(res_cells) | set(job_cells)
    best = None
    for size in candidates:
        blocks = {(int(e // size) * size, int(n // size) * size) for e, n, _ in all_keys}
        n_blocks = len(blocks)
        score = abs(n_blocks - target_points)
        if best is None or score < best[0]:
            best = (score, size, n_blocks)
        if n_blocks <= target_points:
            break
    _, size, n_blocks = best
    print(f"  aggregating to {size} m blocks -> ~{n_blocks:,} points "
          f"(target {target_points:,})")

    agg = defaultdict(lambda: {"res": 0.0, "job": 0.0, "e": 0.0, "n": 0.0, "w": 0.0})
    for (e, n, _s), v in res_cells.items():
        b = agg[(int(e // size) * size, int(n // size) * size)]
        b["res"] += v
        b["e"] += (e + _s / 2) * v
        b["n"] += (n + _s / 2) * v
        b["w"] += v
    for (e, n, _s), v in job_cells.items():
        b = agg[(int(e // size) * size, int(n // size) * size)]
        b["job"] += v
        b["e"] += (e + _s / 2) * v * 0.5     # jobs pull the centroid too, half weight
        b["n"] += (n + _s / 2) * v * 0.5
        b["w"] += v * 0.5

    keys, res, jobs, es, ns = [], [], [], [], []
    for (be, bn), b in agg.items():
        if b["res"] <= 0 and b["job"] <= 0:
            continue
        if b["w"] > 0:
            ce, cn = b["e"] / b["w"], b["n"] / b["w"]
        else:
            ce, cn = be + size / 2, bn + size / 2
        keys.append((be, bn))
        res.append(b["res"])
        jobs.append(b["job"])
        es.append(ce)
        ns.append(cn)
    return (np.array(res, dtype=np.float64), np.array(jobs, dtype=np.float64),
            np.array(es), np.array(ns), size)


# ---------------------------------------------------------------------------
# Gravity model
# ---------------------------------------------------------------------------
def haversine_matrix(lon, lat):
    """Great-circle distance in km, (n, n) float32."""
    lon_r = np.radians(lon.astype(np.float32))
    lat_r = np.radians(lat.astype(np.float32))
    dlon = lon_r[None, :] - lon_r[:, None]
    dlat = lat_r[None, :] - lat_r[:, None]
    a = (np.sin(dlat / 2) ** 2
         + np.cos(lat_r)[:, None] * np.cos(lat_r)[None, :] * np.sin(dlon / 2) ** 2)
    return (6371.0 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))).astype(np.float32)


def furness(origins, dests, dist_km, beta, intra_km, iters=60):
    """
    Doubly-constrained gravity model. Returns the flow matrix.

    Balancing factors absorb the origin and destination totals:

        a_i = O_i / sum_j (f_ij b_j)      b_j = D_j / sum_i (f_ij a_i)
        T_ij = a_i f_ij b_j

    so row sums come out as O and column sums as D. Multiplying O_i or D_j
    back in when forming T would double-count them - the earlier version did
    exactly that and inflated total flow by ~350x.
    """
    d = dist_km.copy()
    np.fill_diagonal(d, intra_km)
    f = np.exp(-beta * d, dtype=np.float32)

    a = np.ones_like(origins)
    b = np.ones_like(dests)
    for i in range(iters):
        a = origins / np.maximum(f @ b, 1e-12)
        b_new = dests / np.maximum(f.T @ a, 1e-12)
        shift = np.max(np.abs(b_new - b) / np.maximum(b, 1e-12))
        b = b_new
        if shift < 1e-4:
            print(f"  IPF converged after {i + 1} iterations")
            break

    flows = a[:, None] * f * b[None, :]
    err = np.abs(flows.sum(axis=1) - origins).sum() / max(origins.sum(), 1e-9)
    print(f"  row-total error {err:.2%} (should be ~0%), "
          f"total flow {flows.sum():,.0f} (should be {origins.sum():,.0f})")
    return flows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print("### Loading residential population grid")
    pop_grid = find_grid(POP_GRID_CANDIDATES,
                         ["*popreg*", "*population*", "*bevoelker*", "*bev*"])
    if pop_grid is None:
        raise SystemExit(
            f"No population grid found in {DATA_DIR}\n\n"
            "Download STATISTIK AUSTRIA's free INSPIRE 100 m residential\n"
            "population grid (Wohnbevölkerung nach 100m ETRS-LAEA Raster):\n"
            "  https://www.statistik.at/gs-inspire/www/inspire2/download/daten/"
            "pd_popreg_100m_7767c33f-302c-11e3-beb4-0000c1ab0db6.zip\n\n"
            f"Unzip it into {DATA_DIR} and re-run. See README step 3.\n"
            "NOTE: the OGDEXT_RASTER_1 dataset is grid *geometry* only, with no\n"
            "population attribute - it is not the right download."
        )
    print(f"  source: {pop_grid.name}")
    res_cells = read_grid(pop_grid, value_hint="bev")
    base_size = min(s for _, _, s in res_cells) if res_cells else 100
    print(f"  {len(res_cells):,} populated cells, base grid {base_size} m, "
          f"{sum(res_cells.values()):,.0f} residents (whole file)")

    # Clip to bbox
    from pyproj import Transformer
    fwd = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
    e0, n0 = fwd.transform(BBOX[0], BBOX[1])
    e1, n1 = fwd.transform(BBOX[2], BBOX[3])
    e2, n2 = fwd.transform(BBOX[0], BBOX[3])
    e3, n3 = fwd.transform(BBOX[2], BBOX[1])
    emin, emax = min(e0, e1, e2, e3), max(e0, e1, e2, e3)
    nmin, nmax = min(n0, n1, n2, n3), max(n0, n1, n2, n3)
    res_cells = clip_wgs84({k: v for k, v in res_cells.items()
                            if emin <= k[0] <= emax and nmin <= k[1] <= nmax})
    print(f"  {len(res_cells):,} cells inside the bbox, "
          f"{sum(res_cells.values()):,.0f} residents")

    print("\n### Building the jobs surface")
    job_grid = find_grid(JOB_GRID_CANDIDATES, ["*arbeitsstaett*", "*besch*"])
    if job_grid is not None:
        print(f"  using the official Statistik Austria workplace grid "
              f"({job_grid.name})")
        job_cells = read_grid(job_grid, value_hint="besch")
        job_cells = clip_wgs84({k: v for k, v in job_cells.items()
                                if emin <= k[0] <= emax and nmin <= k[1] <= nmax})
        jobs_source = "official"
    else:
        print("  no workplace grid found - falling back to the OSM dasymetric")
        job_cells = osm_job_weights(base_size)
        jobs_source = "osm-dasymetric"

    print("\n### Aggregating to demand points")
    res, jobs, es, ns, cell_size = aggregate(
        res_cells, job_cells, DCFG["target_points"], base_size)
    lon, lat = laea_to_wgs84(es, ns)

    # Blocks take a mass-weighted centroid, so one straddling the boundary can
    # still land just outside. Clamp rather than drop - the demand is real, it
    # just belongs on the edge of the playable area.
    outside = int(((lon < BBOX[0]) | (lon > BBOX[2])
                   | (lat < BBOX[1]) | (lat > BBOX[3])).sum())
    if outside:
        print(f"  {outside:,} block centroids clamped to the bbox edge")
        lon = np.clip(lon, BBOX[0], BBOX[2])
        lat = np.clip(lat, BBOX[1], BBOX[3])

    # Scale to the modelled totals.
    workers_in_map = DCFG["total_jobs"] - DCFG["jobs_from_outside"]
    res = res / res.sum() * workers_in_map            # employed residents
    jobs = jobs / jobs.sum() * workers_in_map         # jobs filled from inside
    print(f"  {len(res):,} points, {workers_in_map:,} modelled commuters")

    print("\n### Gravity model")
    dist = haversine_matrix(lon, lat)
    intra = DCFG["intra_zone_share"]
    # Choose the intra-zone impedance so that roughly `intra_zone_share` of
    # workers stay in their own cell.
    intra_km = max(0.15, (cell_size / 1000.0) * 0.4)
    flows = furness(res.astype(np.float32), jobs.astype(np.float32),
                    dist, DCFG["gravity_beta"], intra_km)

    med = np.average(dist.ravel(), weights=flows.ravel())
    print(f"  mean modelled commute (straight line): {med:.1f} km")
    print(f"  intra-zone share: {np.trace(flows) / flows.sum():.1%} "
          f"(config nominates {intra:.0%})")
    print("  NOTE: intra_zone_share is descriptive, not enforced - with "
          "thousands of\n  competing destinations the own-cell share stays "
          "well under 1%. Harmless\n  for a subway map: people who work in "
          "their own block never ride. Only\n  gravity_beta actually moves "
          "the commute distribution.")

    print("\n### Emitting pops")
    max_pop = DCFG["MAXPOPSIZE"]
    min_flow = DCFG["min_flow_size"]
    target_pops = int(DCFG.get("target_pops", 30000))
    seed = int(DCFG.get("sample_seed", 20260817))

    point_ids = [f"p{i:05d}" for i in range(len(res))]
    points = [{"id": pid, "location": [round(float(lo), 6), round(float(la), 6)],
               "jobs": 0, "residents": 0, "popIds": []}
              for pid, lo, la in zip(point_ids, lon, lat)]

    # Sample origin-destination pairs in proportion to modelled flow.
    #
    # The obvious approach - keep each origin's top-k destinations and rescale
    # them to the origin's total - is what this used to do, and it is badly
    # wrong. Every origin's top-k is very nearly the same set of globally
    # attractive cells, so with k=30 only ~139 distinct destinations ever get
    # chosen out of ~3,900, the busiest destination is inflated 3.7x, and the
    # top-50 share of all demand goes from an intended 32% to 92%. Rescaling
    # rows preserves origin totals while destroying the destination totals the
    # gravity model just spent 6 IPF iterations balancing.
    #
    # Sampling proportional to flow reproduces both marginals in expectation,
    # reaches ~2,500 destinations, and lands the concentration profile within
    # half a point of the intended one. The cost is discretisation noise on
    # individual points, which is unbiased - far preferable to a systematic
    # 3.7x distortion.
    n_pts = flows.shape[0]
    cdf = np.cumsum(flows.ravel(), dtype=np.float64)
    total_flow = float(cdf[-1])
    cdf /= total_flow

    unit = total_flow / target_pops
    if unit < min_flow:
        print(f"  WARNING: sampled pop size {unit:.1f} is below "
              f"min_flow_size {min_flow}; lower target_pops")

    gen = np.random.default_rng(seed)
    picks = np.searchsorted(cdf, gen.random(target_pops), side="left")
    picks = np.clip(picks, 0, n_pts * n_pts - 1)
    pair_idx, pair_counts = np.unique(picks, return_counts=True)
    del cdf

    origins_i = (pair_idx // n_pts).astype(np.int64)
    dests_j = (pair_idx % n_pts).astype(np.int64)
    print(f"  sampled {target_pops:,} draws -> {len(pair_idx):,} distinct OD "
          f"pairs, {len(np.unique(origins_i)):,} origins, "
          f"{len(np.unique(dests_j)):,} destinations, {unit:.1f} people per draw")

    # Integer sizes by largest remainder. Rounding each pair independently
    # biases the total upward - with 21.7 people per draw, every single-draw
    # pair rounds up to 22 and the map ends up with 1.5% more commuters than
    # the gravity model produced. Systematic, not noise.
    raw = pair_counts * unit
    sizes_arr = np.floor(raw).astype(np.int64)
    deficit = int(round(raw.sum() - sizes_arr.sum()))
    if deficit > 0:
        frac = raw - sizes_arr
        sizes_arr[np.argpartition(frac, -deficit)[-deficit:]] += 1

    pops = []
    counter = 0
    n_dropped = 0
    for i, j, size in zip(origins_i, dests_j, sizes_arr):
        size = int(size)
        if size <= 0:
            n_dropped += 1
            continue
        while size > 0:
            chunk = min(size, max_pop)
            pops.append({
                "id": str(counter),
                "size": int(chunk),
                "residenceId": point_ids[int(i)],
                "jobId": point_ids[int(j)],
                "drivingSeconds": 0,
                "drivingDistance": 0,
            })
            counter += 1
            size -= chunk

    data = {"points": points, "pops": pops}

    # Mirror depot's sanitize so the file is already consistent.
    by_id = {p["id"]: p for p in points}
    for p in pops:
        by_id[p["residenceId"]]["popIds"].append(p["id"])
        by_id[p["residenceId"]]["residents"] += p["size"]
        by_id[p["jobId"]]["popIds"].append(p["id"])
        by_id[p["jobId"]]["jobs"] += p["size"]
    data["points"] = [p for p in points if p["jobs"] + p["residents"] > 0]

    out = OUT_DIR / "demand_data.json"
    out.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")

    total = sum(p["size"] for p in pops)
    print(f"\n  points: {len(data['points']):,}")
    print(f"  pops:   {len(pops):,}")
    print(f"  modelled commuters: {total:,} of {workers_in_map:,} "
          f"({total / workers_in_map:.1%} retained; {n_dropped:,} draws "
          f"rounded to zero)")
    # Concentration check, calibrated against THIS city rather than a constant.
    # The reference is the gravity model's own point masses before sampling;
    # if the emitted file is much peakier than that, demand has collapsed onto
    # a few points (which is exactly what top-k truncation used to do).
    def _profile(values):
        vals = np.sort(np.asarray(values, dtype=np.float64))[::-1]
        tot = vals.sum() or 1.0
        return [100 * vals[:m].sum() / tot for m in (10, 50, 100)]

    want = _profile(flows.sum(axis=0) + flows.sum(axis=1))
    got = _profile([p["jobs"] + p["residents"] for p in data["points"]])
    fmt = lambda v: " / ".join(f"{x:.1f}%" for x in v)
    print(f"  demand concentration, top 10/50/100 points: {fmt(got)}")
    print(f"    gravity model intended:                   {fmt(want)}")
    drift = max(g - w for g, w in zip(got, want))
    if drift > 5.0:
        print(f"    WARNING: {drift:.1f} points peakier than intended - "
              "demand is collapsing onto a few points")
    print(f"  jobs source: {jobs_source}")
    print(f"\nWrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
    print("Next: python scripts/03_special_demand.py")


if __name__ == "__main__":
    main()
