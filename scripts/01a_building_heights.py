#!/usr/bin/env python
"""
Step 1a - Give every building a plausible height.

Overture's building heights are sparse: for the Vienna bbox only ~41% of
footprints carry `height` and ~19% carry `num_floors`. depot defaults anything
without a height to 4 m, which would flatten most of the city - including the
Gründerzeit blocks that are actually 16-20 m.

This fills the gap in three tiers, most trustworthy first:

  1. `height`      - use Overture's value as-is
  2. `num_floors`  - floors x metres_per_floor
  3. imputed       - median height of buildings within the same small grid cell
                     that DID have a height, falling back to a coarser cell,
                     then to the city-wide median

Tier 3 is a spatial imputation rather than a constant: a building with no
attributes in Ottakring inherits Ottakring's typical height, not Donaustadt's.
Every output feature gets a `height` and a `height_source` property, so the
provenance stays auditable and you can report it honestly in the map listing.

Usage:
    conda activate depot
    python scripts/01a_building_heights.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config" / "vienna.json").read_text(encoding="utf-8"))
HCFG = CFG["map_generation"].get("building_heights", {})

BUILD_DIR = Path(os.environ.get("SB_BUILD_DIR", Path.home() / "vienna"))
DATA_DIR = BUILD_DIR / "data"
SRC = DATA_DIR / "buildings.geojson"
DST = DATA_DIR / "buildings_with_heights.geojson"

# ~3.2 m per storey suits Vienna's stock: Gründerzeit floors are tall (3.5-4 m),
# post-war and modern are shorter (2.7-3 m).
M_PER_FLOOR = float(HCFG.get("metres_per_floor", 3.2))
# ~0.002 deg is roughly 150 m at Vienna's latitude - a block or two.
FINE = float(HCFG.get("impute_grid_deg", 0.002))
# ~0.01 deg is roughly 750 m - a neighbourhood.
COARSE = float(HCFG.get("fallback_grid_deg", 0.01))
MIN_SAMPLES = int(HCFG.get("min_samples_per_cell", 3))
# Guard rails: Overture occasionally carries nonsense values.
MIN_H, MAX_H = 2.0, 260.0   # Donauturm aside, DC Tower is 220 m


def first_coord(geom):
    """Cheap representative point without building a shapely object."""
    c = geom.get("coordinates")
    try:
        while isinstance(c[0], list):
            c = c[0]
        return float(c[0]), float(c[1])
    except (TypeError, IndexError, ValueError):
        return None


def clean_height(val):
    try:
        h = float(val)
    except (TypeError, ValueError):
        return None
    if MIN_H <= h <= MAX_H:
        return h
    return None


def main() -> None:
    if not SRC.exists():
        sys.exit(f"Missing {SRC}\nFetch it first - see README step 3.")

    total_bytes = SRC.stat().st_size
    print(f"Pass 1/2 - measuring known heights in {SRC.name} "
          f"({total_bytes / 1e6:.0f} MB)")

    fine_cells: dict = defaultdict(list)
    coarse_cells: dict = defaultdict(list)
    all_known: list = []
    n_feat = n_height = n_floors = 0
    read = 0
    next_report = 25_000_000

    with SRC.open("r", encoding="utf-8") as fh:
        for line in fh:
            read += len(line)
            s = line.strip().rstrip(",")
            if not s.startswith('{"id"') and '"Feature"' not in s:
                continue
            try:
                feat = json.loads(s)
            except json.JSONDecodeError:
                continue
            n_feat += 1
            props = feat.get("properties") or {}

            h = clean_height(props.get("height"))
            if h is None:
                floors = props.get("num_floors")
                if floors:
                    try:
                        h = clean_height(float(floors) * M_PER_FLOOR)
                        if h is not None:
                            n_floors += 1
                    except (TypeError, ValueError):
                        h = None
            else:
                n_height += 1

            if h is None:
                continue

            pt = first_coord(feat.get("geometry") or {})
            if pt is None:
                continue
            lon, lat = pt
            fine_cells[(int(lon / FINE), int(lat / FINE))].append(h)
            coarse_cells[(int(lon / COARSE), int(lat / COARSE))].append(h)
            all_known.append(h)

            if read >= next_report:
                sys.stdout.write(f"\r  {read * 100.0 / total_bytes:5.1f}%")
                sys.stdout.flush()
                next_report += 25_000_000

    print(f"\r  100.0%" + " " * 20)

    if not all_known:
        sys.exit("No usable heights at all - check the input file.")

    fine_med = {k: statistics.median(v) for k, v in fine_cells.items()
                if len(v) >= MIN_SAMPLES}
    coarse_med = {k: statistics.median(v) for k, v in coarse_cells.items()
                  if len(v) >= MIN_SAMPLES}
    city_med = statistics.median(all_known)

    print(f"  buildings:              {n_feat:,}")
    print(f"  with Overture height:   {n_height:,} ({n_height / n_feat:.1%})")
    print(f"  derived from floors:    {n_floors:,} ({n_floors / n_feat:.1%})")
    print(f"  fine cells (~150 m):    {len(fine_med):,}")
    print(f"  coarse cells (~750 m):  {len(coarse_med):,}")
    print(f"  city-wide median:       {city_med:.1f} m")

    print(f"\nPass 2/2 - writing {DST.name}")
    counts = defaultdict(int)
    read = 0
    next_report = 25_000_000

    with SRC.open("r", encoding="utf-8") as fh, \
            DST.open("w", encoding="utf-8") as out:
        out.write('{"type": "FeatureCollection", "features": [\n')
        first = True
        for line in fh:
            read += len(line)
            s = line.strip().rstrip(",")
            if not s.startswith('{"id"') and '"Feature"' not in s:
                continue
            try:
                feat = json.loads(s)
            except json.JSONDecodeError:
                continue

            props = feat.setdefault("properties", {})
            source = "overture"
            h = clean_height(props.get("height"))

            if h is None:
                floors = props.get("num_floors")
                if floors:
                    try:
                        h = clean_height(float(floors) * M_PER_FLOOR)
                        source = "floors"
                    except (TypeError, ValueError):
                        h = None

            if h is None:
                pt = first_coord(feat.get("geometry") or {})
                if pt is not None:
                    lon, lat = pt
                    h = fine_med.get((int(lon / FINE), int(lat / FINE)))
                    source = "imputed_local"
                    if h is None:
                        h = coarse_med.get(
                            (int(lon / COARSE), int(lat / COARSE)))
                        source = "imputed_area"
                if h is None:
                    h = city_med
                    source = "imputed_city"

            props["height"] = round(float(h), 1)
            props["height_source"] = source
            counts[source] += 1

            # Drop Overture's bulky provenance block - depot never reads it and
            # it is most of the file size.
            props.pop("sources", None)

            out.write(("" if first else ",\n") + json.dumps(feat,
                                                            separators=(",", ":")))
            first = False

            if read >= next_report:
                sys.stdout.write(f"\r  {read * 100.0 / total_bytes:5.1f}%")
                sys.stdout.flush()
                next_report += 25_000_000

        out.write("\n]}\n")

    print(f"\r  100.0%" + " " * 20)
    written = sum(counts.values())
    print(f"\nWrote {DST} ({DST.stat().st_size / 1e6:.0f} MB, "
          f"{written:,} buildings)")
    print("\n  height provenance:")
    for src in ("overture", "floors", "imputed_local", "imputed_area",
                "imputed_city"):
        if counts[src]:
            print(f"    {src:<16} {counts[src]:>9,}  "
                  f"({counts[src] / written:5.1%})")

    measured = counts["overture"] + counts["floors"]
    print(f"\n  measured or derived: {measured / written:.1%}")
    if counts["imputed_city"] / written > 0.05:
        print("\n  NOTE: more than 5% fell back to the city-wide median, which "
              "means\n  large areas have no height data at all. Consider "
              "lowering\n  min_samples_per_cell in config/vienna.json.")

    print("\nNow set buildings_geojson in config/vienna.json to:")
    print(f"  {DST}")
    print("(already done if you are using the shipped config)")
    print("\nNext: python scripts/01_build_map.py")


if __name__ == "__main__":
    main()
