#!/usr/bin/env python
"""
Step 1 - Build the Vienna map geometry.

Produces, in <outputdir>/<map_code>/:
    buildings_index.json  /  buildings_index.bin(.gz)   building collision data
    roads.geojson                                       street network for routing
    runways_taxiways.geojson                            VIE Schwechat runways
    VIE.pmtiles                                         the vector tiles the game draws
    VIE_foundations.pmtiles                             building/ocean foundation layers

Run inside the `depot` conda environment:
    conda activate depot
    python scripts/01_build_map.py

Expect 1.5-4 hours depending on cores and disk. It is safe to re-run individual
stages: comment out the ones that already finished.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

from depot.maps import MapGen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import depot_patches  # noqa: E402  (local module, must follow the path insert)

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config" / "vienna.json").read_text(encoding="utf-8"))

# Where the heavy build happens. Keep this on the WSL filesystem (NOT /mnt/c),
# disk I/O there is several times faster and this step writes a lot.
BUILD_DIR = Path(os.environ.get("SB_BUILD_DIR", Path.home() / "vienna"))
DATA_DIR = BUILD_DIR / "data"
OUT_DIR = BUILD_DIR / "build"
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

PBF_URL = "https://download.geofabrik.de/europe/austria-latest.osm.pbf"
PBF_PATH = DATA_DIR / "austria-latest.osm.pbf"


def download_pbf() -> None:
    """Fetch the Austria extract from Geofabrik (~800 MB) if we don't have it."""
    if PBF_PATH.exists() and PBF_PATH.stat().st_size > 100_000_000:
        print(f"OSM extract already present: {PBF_PATH} "
              f"({PBF_PATH.stat().st_size / 1e6:.0f} MB)")
        return
    print(f"Downloading {PBF_URL}\n  -> {PBF_PATH}")

    def hook(block, block_size, total):
        if total > 0:
            pct = min(100.0, block * block_size * 100.0 / total)
            sys.stdout.write(f"\r  {pct:5.1f}%")
            sys.stdout.flush()

    urllib.request.urlretrieve(PBF_URL, PBF_PATH, reporthook=hook)
    print("\n  done")


def main() -> None:
    download_pbf()

    # Fix depot's dead Overture STAC lookup and its intolerance of unparseable
    # building geometry. See scripts/depot_patches.py for the details.
    depot_patches.apply(CFG)

    mg = CFG["map_generation"]

    # If a pre-fetched buildings file is configured, hand it to depot and it
    # skips the Overture fetch entirely. This is the supported escape hatch
    # when DuckDB cannot read Overture's parquet over S3.
    prefetched = mg.get("buildings_geojson")
    if prefetched:
        prefetched = str(Path(prefetched).expanduser())
        if not os.path.exists(prefetched):
            raise SystemExit(
                f"buildings_geojson is set to {prefetched} but that file does "
                "not exist.\n\nFetch it with:\n"
                "  pip install overturemaps\n"
                f"  overturemaps download --bbox={','.join(str(v) for v in CFG['bbox'])} "
                "\\\n      -f geojson --type=building -o " + prefetched +
                "\n\nOr set buildings_geojson to null to let depot fetch them."
            )
        size_mb = os.path.getsize(prefetched) / 1e6
        print(f"Using pre-fetched buildings: {prefetched} ({size_mb:,.0f} MB)")

    obj = MapGen(
        city=CFG["map_code"],
        bbox=CFG["bbox"],
        osmpbf=str(PBF_PATH),
        outputdir=str(OUT_DIR),
        buildings_geojson=prefetched or None,

        # Building filtering / simplification
        building_index_filter_size=mg["building_index_filter_size"],
        building_tile_filter_size=mg["building_tile_filter_size"],
        building_index_simplification=mg["building_index_simplification"],
        building_tile_simplification=mg["building_tile_simplification"],
        max_building_tile_size=mg["max_building_tile_size"],

        # Layers
        create_building_foundations=mg["create_building_foundations"],
        create_ocean_foundations=mg["create_ocean_foundations"],
        color_military_like_aerodrome=mg["color_military_like_aerodrome"],
        maxzoom=mg["maxzoom"],

        # Labels
        cities=mg["cities"],
        suburbs=mg["suburbs"],
        neighborhoods=mg["neighborhoods"],
        label_name_language=mg["label_name_language"],
        road_name_preferred_language=mg["road_name_preferred_language"],

        # Machine
        ncores=mg["ncores"],
        RAM=mg["RAM"],
        cleanup_files=False,   # keep intermediates so re-runs are cheap
        verb=True,
    )

    # --- Stage 1: clip Austria down to the Vienna bbox -----------------------
    print("\n### extract_base_data")
    obj.extract_base_data()

    # Useful sanity check: shows which OSM `place` values actually exist in the
    # bbox and how many of each, so you can tune cities/suburbs/neighborhoods
    # in config/vienna.json before committing to a label pass.
    print("\n### check_labels")
    obj.check_labels()

    # --- Stage 2: buildings (fetches Overture footprints, ~10-20 min) --------
    print("\n### process_buildings")
    obj.process_buildings()

    # --- Stage 3: roads + VIE runways ---------------------------------------
    print("\n### process_roads_and_aeroways")
    obj.process_roads_and_aeroways()

    # --- Stage 4: vector tiles (the long one) -------------------------------
    print("\n### generate_pmtiles")
    obj.generate_pmtiles()

    # --- Stage 5: labels ----------------------------------------------------
    print("\n### add_labels")
    obj.add_labels()

    print(f"\nMap geometry complete. Outputs in {OUT_DIR / CFG['map_code']}")


if __name__ == "__main__":
    main()
