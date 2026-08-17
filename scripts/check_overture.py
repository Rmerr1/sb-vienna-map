#!/usr/bin/env python
"""
Utility - verify Overture building data is reachable, and work out *how*.

depot's `process_buildings` queries Overture's S3 bucket with DuckDB. On some
DuckDB builds this hits an internal error in the httpfs external file cache:

    INTERNAL Error: Information loss on integer cast: value -404098 outside of
    target range [0, 18446744073709551615]

...with a stack trace through CachingFileHandle::ReadAndCopyInterleaved ->
HTTPFileSystem::ReadInternal. That is a DuckDB bug, not a data problem: a
range read returns a length DuckDB computes as negative and then casts to an
unsigned type.

This script tries a sequence of DuckDB configurations against a tiny bbox and
reports which one works. Whichever succeeds gets written to
config/overture_duckdb.json, and depot_patches.py applies it during the real
build.

Usage:
    conda activate depot
    python scripts/check_overture.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config" / "vienna.json").read_text(encoding="utf-8"))
SETTINGS_OUT = ROOT / "config" / "overture_duckdb.json"

RELEASE = CFG["map_generation"].get("overture_release")
# Innere Stadt - small, dense, unambiguously full of buildings.
TEST_BBOX = [16.365, 48.203, 16.380, 48.213]

# Tried in order; first one that works wins.
STRATEGIES = [
    ("default", []),
    ("no external file cache", [
        "SET enable_external_file_cache=false;",
    ]),
    ("no file cache, no keep-alive", [
        "SET enable_external_file_cache=false;",
        "SET http_keep_alive=false;",
    ]),
    ("no file cache, no keep-alive, single thread", [
        "SET enable_external_file_cache=false;",
        "SET http_keep_alive=false;",
        "SET threads=1;",
    ]),
    ("unsigned https endpoint", [
        "SET enable_external_file_cache=false;",
        "SET http_keep_alive=false;",
        "SET s3_endpoint='s3.us-west-2.amazonaws.com';",
        "SET s3_url_style='path';",
    ]),
]


def try_strategy(name: str, statements: list[str]) -> int | None:
    s3 = (f"s3://overturemaps-us-west-2/release/{RELEASE}"
          f"/theme=buildings/type=building/*")
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial; LOAD spatial;")
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute("SET s3_region='us-west-2';")
        for stmt in statements:
            try:
                con.execute(stmt)
            except Exception as exc:
                print(f"    (setting rejected: {stmt.strip()} - {exc})")
        query = f"""
        SELECT count(*) AS n
        FROM read_parquet('{s3}', hive_partitioning=1)
        WHERE bbox.xmin >= {TEST_BBOX[0]} AND bbox.xmax <= {TEST_BBOX[2]}
          AND bbox.ymin >= {TEST_BBOX[1]} AND bbox.ymax <= {TEST_BBOX[3]}
        """
        return con.query(query).fetchone()[0]
    except Exception as exc:
        first_line = str(exc).strip().splitlines()[0]
        print(f"    failed: {first_line}")
        return None
    finally:
        con.close()


def main() -> None:
    if not RELEASE:
        sys.exit("No overture_release set in config/vienna.json.")

    print(f"duckdb {duckdb.__version__}, Overture release {RELEASE}\n")

    for name, statements in STRATEGIES:
        print(f"  trying: {name}")
        n = try_strategy(name, statements)
        if n is None:
            continue
        if n < 100:
            print(f"    connected but only {n} buildings - suspicious, "
                  "trying the next strategy")
            continue

        print(f"\nSUCCESS with '{name}': {n:,} buildings in the test box.")
        SETTINGS_OUT.write_text(
            json.dumps({"strategy": name, "statements": statements}, indent=2),
            encoding="utf-8")
        print(f"Wrote {SETTINGS_OUT.name} - depot_patches.py will apply these "
              "during the build.")
        print("\nSafe to run 01_build_map.py.")
        return

    print("\nAll strategies failed.\n")
    print("Fallback: fetch the buildings with Overture's own CLI instead of "
          "DuckDB.\n")
    print("  pip install overturemaps")
    print(f"  overturemaps download --bbox={','.join(str(v) for v in CFG['bbox'])} \\")
    print("      -f geojson --type=building -o ~/vienna/data/buildings.geojson\n")
    print("Then set  \"buildings_geojson\": \"~/vienna/data/buildings.geojson\"")
    print("in config/vienna.json and depot will skip its own fetch entirely.")
    sys.exit(1)


if __name__ == "__main__":
    main()
