#!/usr/bin/env python
"""
Step 4 - Package the map for Railyard and print the submission numbers.

Builds <map_code>-v<version>.zip with every file at the archive root (that is
what Railyard's patcher expects), then prints the exact manifest values you
need for the "Publish a New Map" issue: population, residents_total,
points_count, population_count and the file_sizes table.

Usage:
    conda activate depot
    python scripts/04_package.py
"""
from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config" / "vienna.json").read_text(encoding="utf-8"))

BUILD_DIR = Path(os.environ.get("SB_BUILD_DIR", Path.home() / "vienna"))
OUT_DIR = BUILD_DIR / "build" / CFG["map_code"]
DIST_DIR = BUILD_DIR / "dist"
DIST_DIR.mkdir(parents=True, exist_ok=True)

CODE = CFG["map_code"]

# Everything Railyard looks for, in the order Zurich/Amsterdam ship them.
# Missing optional entries are skipped with a warning rather than failing.
REQUIRED = [
    f"{CODE}.pmtiles",
    "config.json",
    "demand_data.json",
    "roads.geojson",
]
OPTIONAL = [
    f"{CODE}_foundations.pmtiles",
    "buildings_index.bin.gz",
    "buildings_index.bin",
    "buildings_index.json",
    "runways_taxiways.geojson",
    "ocean_depth_index.json.gz",
    "building_tags.json",
]


def mib(path: Path) -> float:
    return round(path.stat().st_size / (1024 * 1024), 2)


def main() -> None:
    if not OUT_DIR.exists():
        raise SystemExit(f"Missing {OUT_DIR}. Run steps 1-3 first.")

    files: list[Path] = []
    for name in REQUIRED:
        p = OUT_DIR / name
        if not p.exists():
            raise SystemExit(f"Required file missing: {p}")
        files.append(p)

    for name in OPTIONAL:
        p = OUT_DIR / name
        if p.exists():
            files.append(p)
        else:
            print(f"  note: optional file not present, skipping - {name}")

    # buildings_index.bin.gz supersedes the uncompressed pair; ship the smallest
    # complete set to keep the download reasonable.
    names = {f.name for f in files}
    if "buildings_index.bin.gz" in names:
        files = [f for f in files if f.name != "buildings_index.bin"]

    version = CFG["version"].lstrip("v")
    zip_path = DIST_DIR / f"{CODE}-v{version}.zip"

    print(f"\nPackaging {len(files)} files -> {zip_path.name}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in files:
            zf.write(f, arcname=f.name)          # archive ROOT, no subfolders
            print(f"  {f.name:<34} {mib(f):>8.2f} MiB")

    # ------------------------------------------------------------- statistics
    demand = json.loads((OUT_DIR / "demand_data.json").read_text(encoding="utf-8"))
    config = json.loads((OUT_DIR / "config.json").read_text(encoding="utf-8"))

    points_count = len(demand["points"])
    population_count = len(demand["pops"])
    residents_total = sum(p["size"] for p in demand["pops"])

    file_sizes = {f.name: mib(f) for f in files}

    summary = {
        "id": "vienna",
        "name": CFG["map_name"],
        "city_code": CODE,
        "country": CFG["country"],
        "population": residents_total,
        "residents_total": residents_total,
        "points_count": points_count,
        "population_count": population_count,
        "initial_view_state": config["initialViewState"],
        "bbox": CFG["bbox"],
        "file_sizes": file_sizes,
        "zip": str(zip_path),
        "zip_size_mib": mib(zip_path),
    }
    (DIST_DIR / "submission_values.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")

    print("\n=== Values for the Railyard 'Publish a New Map' issue ===")
    print(f"  Map ID              vienna")
    print(f"  City Name           {CFG['map_name']}")
    print(f"  City Code           {CODE}")
    print(f"  Country Code        {CFG['country']}")
    print(f"  population          {residents_total:,}")
    print(f"  residents_total     {residents_total:,}")
    print(f"  points_count        {points_count:,}")
    print(f"  population_count    {population_count:,}")
    print(f"  zip size            {mib(zip_path):.1f} MiB")
    print(f"\n  file_sizes:\n{json.dumps(file_sizes, indent=4)}")

    # ------------------------------------- copy back to the Windows-side folder
    win_dir = os.environ.get("SB_DELIVER_DIR")
    if win_dir:
        dest = Path(win_dir)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(zip_path, dest / zip_path.name)
        shutil.copy2(DIST_DIR / "submission_values.json",
                     dest / "submission_values.json")
        if (OUT_DIR / "description.md").exists():
            shutil.copy2(OUT_DIR / "description.md", dest / "description.md")
        print(f"\nCopied the map and submission files to {dest}")
    else:
        print("\nSet SB_DELIVER_DIR to also copy the zip to your Windows folder, e.g."
              "\n  SB_DELIVER_DIR=/mnt/c/Users/<you>/SubwayBuilder python scripts/04_package.py")

    print(f"\nDone. Install it locally by pointing Railyard at {zip_path}, "
          "play-test it, then publish.")


if __name__ == "__main__":
    main()
